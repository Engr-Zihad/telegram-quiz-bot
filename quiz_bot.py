import asyncio
import io
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pymupdf
from PIL import Image
from google import genai
from google.genai import types
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
QUIZ_TOPIC_ID = os.environ.get("QUIZ_TOPIC_ID")

client = genai.Client(api_key=GEMINI_API_KEY)

FAST_MODELS = [
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

# Store pending quiz sources per message until count is chosen
PENDING_JOBS = {}


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
        return True
    return str(message.message_thread_id) == str(QUIZ_TOPIC_ID).strip()


async def generate_json(contents, temperature=0.35):
    last_error = None
    for model_name in FAST_MODELS:
        try:
            res = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=temperature,
                ),
            )
            return json.loads(res.text)
        except Exception as err:
            last_error = err
            await asyncio.sleep(0.25)
    raise last_error


def crop_diagram_from_pdf(doc, page_num: int, box_2d):
    """Crop a specific region [ymin, xmin, ymax, xmax] (0-1000) cleanly with padding."""
    if not doc or not isinstance(page_num, int) or not (1 <= page_num <= len(doc)):
        return None
    page = doc[page_num - 1]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0))
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    w, h = img.size

    if isinstance(box_2d, list) and len(box_2d) == 4:
        ymin, xmin, ymax, xmax = box_2d
        pad_x = int(w * 0.02)
        pad_y = int(h * 0.02)
        left = max(0, int((xmin / 1000.0) * w) - pad_x)
        top = max(0, int((ymin / 1000.0) * h) - pad_y)
        right = min(w, int((xmax / 1000.0) * w) + pad_x)
        bottom = min(h, int((ymax / 1000.0) * h) + pad_y)

        if (right - left) > 60 and (bottom - top) > 60 and ((right - left) * (bottom - top)) < (w * h * 0.85):
            cropped = img.crop((left, top, right, bottom))
            out = io.BytesIO()
            cropped.save(out, format="PNG", optimize=True)
            return out.getvalue()

    pix_fb = page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2))
    return pix_fb.tobytes("png")


def build_count_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Auto (Smart)", callback_data=f"qcount:{job_id}:auto"),
            InlineKeyboardButton("5 Qs", callback_data=f"qcount:{job_id}:5"),
            InlineKeyboardButton("10 Qs", callback_data=f"qcount:{job_id}:10"),
            InlineKeyboardButton("15 Qs", callback_data=f"qcount:{job_id}:15"),
        ]
    ])


async def get_topic_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    await message.reply_text(f"📌 Topic ID: `{message.message_thread_id}`", parse_mode="Markdown")


# Step 1: Receive PDF -> Ask how many questions
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document or message.document.mime_type != "application/pdf":
        return
    if not is_allowed_topic(message):
        return

    file = await context.bot.get_file(message.document.file_id)
    pdf_bytes = bytes(await file.download_as_bytearray())

    job_id = f"{message.chat_id}_{message.message_id}"
    PENDING_JOBS[job_id] = {
        "type": "pdf",
        "data": pdf_bytes,
        "chat_id": message.chat_id,
        "topic_id": message.message_thread_id,
        "user_id": message.from_user.id if message.from_user else None,
    }

    await message.reply_text(
        "📄 PDF received! How many quiz questions should I generate?\n"
        "*(Choose **Auto** to let me decide based on importance & length)*",
        reply_markup=build_count_keyboard(job_id),
        parse_mode="Markdown",
    )


# Step 1: Receive Photo -> Ask how many questions
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.photo:
        return
    if not is_allowed_topic(message):
        return

    photo_file = await context.bot.get_file(message.photo[-1].file_id)
    img_bytes = bytes(await photo_file.download_as_bytearray())

    job_id = f"{message.chat_id}_{message.message_id}"
    PENDING_JOBS[job_id] = {
        "type": "photo",
        "data": img_bytes,
        "chat_id": message.chat_id,
        "topic_id": message.message_thread_id,
        "user_id": message.from_user.id if message.from_user else None,
    }

    await message.reply_text(
        "🖼️ Image received! How many quiz questions should I generate?\n"
        "*(Choose **Auto** to let me decide based on detail & importance)*",
        reply_markup=build_count_keyboard(job_id),
        parse_mode="Markdown",
    )


# Step 1: Receive Text -> Ask how many questions
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    if not is_allowed_topic(message):
        return
    if len(message.text.strip()) < 30:
        return

    job_id = f"{message.chat_id}_{message.message_id}"
    PENDING_JOBS[job_id] = {
        "type": "text",
        "data": message.text.strip(),
        "chat_id": message.chat_id,
        "topic_id": message.message_thread_id,
        "user_id": message.from_user.id if message.from_user else None,
    }

    await message.reply_text(
        "📝 Notes received! How many quiz questions should I generate?\n"
        "*(Choose **Auto** to let me decide based on key concepts)*",
        reply_markup=build_count_keyboard(job_id),
        parse_mode="Markdown",
    )


# Step 2: Handle Button Click -> Generate & Send Fast
async def handle_count_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "qcount":
        return

    _, job_id, count_choice = parts
    job = PENDING_JOBS.pop(job_id, None)
    if not job:
        await query.answer("Session expired. Please send the file/text again.", show_alert=True)
        return

    await query.answer()
    chat_id = job["chat_id"]
    topic_id = job["topic_id"]

    count_rule = (
        "Decide the ideal number of questions (between 4 and 12) automatically based on how much important/technical information is in the source."
        if count_choice == "auto"
        else f"Generate exact {count_choice} multiple-choice quiz questions."
    )

    await query.edit_message_text(f"⚡ Generating quiz ({count_choice.upper()})...")

    doc = None
    try:
        if job["type"] == "pdf":
            pdf_bytes = job["data"]
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            prompt = f"""
            {count_rule}
            Rules:
            - Write ALL questions, options, and explanations strictly in English.
            - Focus on important concepts, components, pinouts, ratings, or practical workings.
            - If a question refers to a specific diagram/component on a page, set "crop_image": true, set 1-based "page_number", and set "box_2d": [ymin, xmin, ymax, xmax] (0-1000 integers tightly framing ONLY the diagram/component, NOT full page text). Otherwise "crop_image": false, "page_number": null, "box_2d": null.
            - All 4 options must be clear, plausible, and similar in length.

            Return ONLY a JSON array of objects with keys:
            - "question": string (max 250 chars)
            - "options": array of 4 strings (each max 90 chars)
            - "correct_id": integer (0 to 3)
            - "explanation": string (max 180 chars)
            - "crop_image": boolean
            - "page_number": integer or null
            - "box_2d": [ymin, xmin, ymax, xmax] or null
            """
            questions = await generate_json([
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt,
            ])

        elif job["type"] == "photo":
            prompt = f"""
            {count_rule}
            Analyze this image (electronics, robotics, circuit, or diagram) and write ALL questions, options, and explanations strictly in English.
            Include component/diagram identification, working principle/pins, and practical usage/troubleshooting.
            All 4 options must be clear and plausible.

            Return ONLY a JSON array of objects with keys:
            - "question": string (max 250 chars)
            - "options": array of 4 strings (each max 90 chars)
            - "correct_id": integer (0 to 3)
            - "explanation": string (max 180 chars)
            """
            questions = await generate_json([
                types.Part.from_bytes(data=job["data"], mime_type="image/jpeg"),
                prompt,
            ])

        else:  # text
            prompt = f"""
            {count_rule}
            Write ALL questions, options, and explanations strictly in English based on the text below.
            Focus on key concepts and practical application. All 4 options must be clear and plausible.

            Return ONLY a JSON array of objects with keys:
            - "question": string (max 250 chars)
            - "options": array of 4 strings (each max 90 chars)
            - "correct_id": integer (0 to 3)
            - "explanation": string (max 180 chars)

            Text:
            {job["data"]}
            """
            questions = await generate_json([prompt])

        await query.edit_message_text(f"✅ {len(questions)} questions ready! Sending:")

        for i, q in enumerate(questions, 1):
            if doc and q.get("crop_image"):
                cropped_bytes = crop_diagram_from_pdf(doc, q.get("page_number"), q.get("box_2d"))
                if cropped_bytes:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        message_thread_id=topic_id,
                        photo=cropped_bytes,
                        caption=f"🔍 Q{i} Target",
                    )
                    await asyncio.sleep(0.2)

            await context.bot.send_poll(
                chat_id=chat_id,
                message_thread_id=topic_id,
                question=f"Q{i}: {q['question']}"[:300],
                options=[opt[:100] for opt in q["options"][:4]],
                type=Poll.QUIZ,
                correct_option_id=int(q["correct_id"]),
                explanation=q.get("explanation", "")[:200],
                is_anonymous=False,
            )
            await asyncio.sleep(0.25)

    except Exception as err:
        await query.edit_message_text(f"❌ Error: {err}")
    finally:
        if doc:
            doc.close()


def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("topicid", get_topic_id))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_count_choice, pattern=r"^qcount:"))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()