import asyncio
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pymupdf
from google import genai
from google.genai import types
from telegram import Update, Poll
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

MODELS_TO_TRY = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash"
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

async def generate_with_fallback(contents):
    last_error = None
    for model_name in MODELS_TO_TRY:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
        except Exception as api_err:
            last_error = api_err
            await asyncio.sleep(2)
    raise last_error

# 1. Handle PDF files (Text + Visual/Screenshot Questions)
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document or message.document.mime_type != "application/pdf":
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
        - If the PDF contains diagrams, circuits, or components, include at least 2-3 visual questions (e.g., "Look at the image above. What is the name of this component?" or "Based on the diagram above...").
        - For visual questions, set "needs_image": true and provide the exact 1-based "page_number" where that diagram/component appears. For text-only questions, set "needs_image": false and "page_number": null.

        Return ONLY a JSON array of objects with these exact keys:
        - "question": string (in English, max 250 characters)
        - "options": array of 4 strings (in English, each max 90 characters)
        - "correct_id": integer (0 to 3)
        - "explanation": string (short explanation in English, max 180 characters)
        - "needs_image": boolean
        - "page_number": integer or null (1-based page number)
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
                await asyncio.sleep(2)

    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=f"❌ An error occurred: {e}"
        )
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

# 2. Handle Direct Photos / Screenshots (Component Identification Quiz)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.photo:
        return

    chat_id = message.chat_id
    topic_id = message.message_thread_id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="⏳ Analyzing image... Generating visual quiz!"
    )

    # Get highest resolution photo
    photo_file = await context.bot.get_file(message.photo[-1].file_id)
    img_path = f"temp_{message.photo[-1].file_unique_id}.jpg"
    await photo_file.download_to_drive(img_path)

    try:
        with open(img_path, "rb") as f:
            img_bytes = f.read()

        prompt = """
        Look at this image (likely an electronics/robotics component, circuit, or diagram).
        Generate 2 multiple-choice quiz questions based on this image strictly in English:
        - Question 1: Ask to identify the component, symbol, or main subject shown in the image (e.g., "What is the name of the component shown in the image above?").
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
            await asyncio.sleep(2)

    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=f"❌ An error occurred: {e}"
        )
    finally:
        if os.path.exists(img_path):
            os.remove(img_path)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()