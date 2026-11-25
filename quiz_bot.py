import asyncio
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pymupdf
from google import genai
from google.genai import types
from telegram import Update, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
QUIZ_TOPIC_ID = os.environ.get("QUIZ_TOPIC_ID")  # Set this in Render Environment

client = genai.Client(api_key=GEMINI_API_KEY)

MODELS_TO_TRY = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite"
]

# Health check web server for Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def is_allowed_topic(message) -> bool:
    if not QUIZ_TOPIC_ID:
        return True  # Works everywhere until QUIZ_TOPIC_ID is set
    return str(message.message_thread_id) == str(QUIZ_TOPIC_ID).strip()

async def generate_with_fallback(contents):
    last_error = None
    for model_name in MODELS_TO_TRY:
        try:
            return await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
        except Exception as api_err:
            last_error = api_err
            await asyncio.sleep(1)
    raise last_error

# Command to get the current topic's ID
async def get_topic_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    topic_id = message.message_thread_id
    await message.reply_text(f"📌 This topic's ID is: `{topic_id}`", parse_mode="Markdown")

# 1. Handle PDF files
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document or message.document.mime_type != "application/pdf":
        return
    if not is_allowed_topic(message):
        return

    chat_id = message.chat_id
    topic_id = message.message_thread_id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="⏳ Reading PDF & scanning diagrams... Generating quiz!"
    )

    file = await context.bot.get_file(message.document.file_id)
    pdf_path = f"temp_{message.document.file_unique_id}.pdf"
    await file.download_to_drive(pdf_path)

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        prompt = """
        Generate 10 multiple-choice quiz questions from this PDF.
        IMPORTANT:
        - Write ALL questions, options, and explanations strictly in English.
        - If the PDF contains diagrams, circuits, or components, include 2-3 visual questions (e.g., "Look at the image above. What is the name of this component?").
        - For visual questions, set "needs_image": true and provide the exact 1-based "page_number". For text-only questions, set "needs_image": false and "page_number": null.

        Return ONLY a JSON array of objects with these exact keys:
        - "question": string (in English, max 250 characters)
        - "options": array of 4 strings (in English, each max 90 characters)
        - "correct_id": integer (0 to 3)
        - "explanation": string (short explanation in English, max 180 characters)
        - "needs_image": boolean
        - "page_number": integer or null
        """

        response = await generate_with_fallback([
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            prompt
        ])

        questions = json.loads(response.text)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"✅ {len(questions)} quiz questions generated! Starting below:"
        )

        with pymupdf.open(pdf_path) as doc:
            for i, q in enumerate(questions, 1):
                page_num = q.get("page_number")
                if q.get("needs_image") and isinstance(page_num, int) and 1 <= page_num <= len(doc):
                    page = doc[page_num - 1]
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5))
                    img_bytes = pix.tobytes("png")
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        message_thread_id=topic_id,
                        photo=img_bytes,
                        caption=f"🖼️ Reference image for Q{i} (Page {page_num}):"
                    )
                    await asyncio.sleep(1)

                await context.bot.send_poll(
                    chat_id=chat_id,
                    message_thread_id=topic_id,
                    question=f"Q{i}: {q['question']}"[:300],
                    options=[opt[:100] for opt in q["options"][:4]],
                    type=Poll.QUIZ,
                    correct_option_id=q["correct_id"],
                    explanation=q.get("explanation", "")[:200],
                    is_anonymous=False
                )
                await asyncio.sleep(1.5)

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ Error generating quiz: {e}"
        )
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

# 2. Handle Direct Photos / Screenshots
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.photo:
        return
    if not is_allowed_topic(message):
        return

    chat_id = message.chat_id
    topic_id = message.message_thread_id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="⏳ Analyzing image... Generating visual quiz!"
    )

    photo_file = await context.bot.get_file(message.photo[-1].file_id)
    img_path = f"temp_{message.photo[-1].file_unique_id}.jpg"
    await photo_file.download_to_drive(img_path)

    try:
        with open(img_path, "rb") as f:
            img_bytes = f.read()

        prompt = """
        Look at this image (likely an electronics/robotics component, circuit, or diagram).
        Generate 2 multiple-choice quiz questions based on this image strictly in English:
        - Question 1: Ask to identify the component, symbol, or main subject shown in the image.
        - Question 2: Ask about its primary function, pinout, or working principle.

        Return ONLY a JSON array of objects with these exact keys:
        - "question": string (in English, max 250 characters)
        - "options": array of 4 strings (in English, each max 90 characters)
        - "correct_id": integer (0 to 3)
        - "explanation": string (short explanation in English, max 180 characters)
        """

        response = await generate_with_fallback([
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            prompt
        ])

        questions = json.loads(response.text)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="✅ Visual quiz ready! Answer below:"
        )

        for i, q in enumerate(questions, 1):
            await context.bot.send_poll(
                chat_id=chat_id,
                message_thread_id=topic_id,
                question=f"Q{i}: {q['question']}"[:300],
                options=[opt[:100] for opt in q["options"][:4]],
                type=Poll.QUIZ,
                correct_option_id=q["correct_id"],
                explanation=q.get("explanation", "")[:200],
                is_anonymous=False
            )
            await asyncio.sleep(1.5)

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ Error analyzing image: {e}"
        )
    finally:
        if os.path.exists(img_path):
            os.remove(img_path)

# 3. Handle Text Messages (Study notes / paragraphs)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    if not is_allowed_topic(message):
        return
    # Ignore very short messages like "hi", "ok", "thanks"
    if len(message.text.strip()) < 30:
        return

    chat_id = message.chat_id
    topic_id = message.message_thread_id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="⏳ Reading text... Generating quiz!"
    )

    try:
        prompt = f"""
        Generate 3 to 5 multiple-choice quiz questions based on the following text.
        IMPORTANT: Write ALL questions, options, and explanations strictly in English.
        Return ONLY a JSON array of objects with these exact keys:
        - "question": string (in English, max 250 characters)
        - "options": array of 4 strings (in English, each max 90 characters)
        - "correct_id": integer (0 to 3)
        - "explanation": string (short explanation in English, max 180 characters)

        Text:
        {message.text}
        """

        response = await generate_with_fallback([prompt])
        questions = json.loads(response.text)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"✅ {len(questions)} quiz questions generated from text:"
        )

        for i, q in enumerate(questions, 1):
            await context.bot.send_poll(
                chat_id=chat_id,
                message_thread_id=topic_id,
                question=f"Q{i}: {q['question']}"[:300],
                options=[opt[:100] for opt in q["options"][:4]],
                type=Poll.QUIZ,
                correct_option_id=q["correct_id"],
                explanation=q.get("explanation", "")[:200],
                is_anonymous=False
            )
            await asyncio.sleep(1.5)

    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ Error generating quiz: {e}"
        )

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("topicid", get_topic_id))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()