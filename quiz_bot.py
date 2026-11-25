import asyncio
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from google import genai
from google.genai import types
from telegram import Update, Poll
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

# Render-এর ফ্রি সার্ভার চালু রাখার জন্য ছোট্ট ওয়েব সার্ভার
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document:
        return

    if message.document.mime_type != "application/pdf":
        return

    chat_id = message.chat_id
    topic_id = message.message_thread_id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="⏳ PDF পড়া হচ্ছে, একটু অপেক্ষা করো... কুইজ তৈরি হচ্ছে!"
    )

    file = await context.bot.get_file(message.document.file_id)
    pdf_path = f"temp_{message.document.file_unique_id}.pdf"
    await file.download_to_drive(pdf_path)

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        prompt = """
        Generate 10 multiple-choice quiz questions from this PDF.
        Return ONLY a JSON array of objects with these exact keys:
        - "question": string (max 250 characters)
        - "options": array of 4 strings (each max 90 characters)
        - "correct_id": integer (0 to 3, index of the correct option)
        - "explanation": string (short explanation, max 180 characters)
        """

        models_to_try = [
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash"
        ]

        response = None
        last_error = None
        for model_name in models_to_try:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                break
            except Exception as api_err:
                last_error = api_err
                await asyncio.sleep(2)

        if response is None:
            raise last_error

        questions = json.loads(response.text)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"✅ {len(questions)}টি কুইজ তৈরি হয়ে গেছে! নিচে পরীক্ষা শুরু হচ্ছে:"
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
            text=f"❌ এরর হয়েছে: {e}"
        )
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()