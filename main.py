"""
ASVM Dialogue Extractor — Telegram bot for Render.
Secrets can be set in Render environment variables. Do NOT commit real tokens to GitHub.
YouTube Data API is used for video metadata only; it cannot download arbitrary video/audio.
"""
import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from googleapiclient.discovery import build
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator

# -------------------- CONFIGURATION --------------------
# Prefer Render Environment Variables. Local fallback strings are intentionally blank.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = os.getenv("API_ID", "").strip()          # Not required by Bot API; only for MTProto clients
API_HASH = os.getenv("API_HASH", "").strip()      # Not required by Bot API; only for MTProto clients
YT_API = os.getenv("YT_API", "").strip()          # YouTube Data API key; metadata/search only
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "19"))
ADMIN_IDS = {
    int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}
MAX_TRANSLATE_CHARS = int(os.getenv("MAX_TRANSLATE_CHARS", "3000"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not PUBLIC_BASE_URL:
    raise RuntimeError("Missing PUBLIC_BASE_URL environment variable.")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing WEBHOOK_SECRET environment variable.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("asvm-dialogue-bot")

yt = build("youtube", "v3", developerKey=YT_API, cache_discovery=False) if YT_API else None
telegram_app = Application.builder().token(BOT_TOKEN).build()
whisper_model = None


def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        log.info("Loading faster-whisper model %s", WHISPER_MODEL_NAME)
        whisper_model = WhisperModel(
            WHISPER_MODEL_NAME,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    return whisper_model


def get_video_id(url: str):
    patterns = [
        r"(?:youtube\.com/watch\?.*?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def youtube_metadata(video_id: str):
    if not yt:
        raise RuntimeError("YT_API is not configured.")
    response = yt.videos().list(
        part="snippet,contentDetails,status",
        id=video_id,
        maxResults=1,
    ).execute()
    items = response.get("items", [])
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet", {})
    details = item.get("contentDetails", {})
    status = item.get("status", {})
    return {
        "title": snippet.get("title", "Unknown title"),
        "channel": snippet.get("channelTitle", "Unknown channel"),
        "duration": details.get("duration", "Unknown"),
        "privacy": status.get("privacyStatus", "unknown"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def chunks(text: str, limit: int = MAX_TRANSLATE_CHARS):
    result, current = [], ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit and current:
            result.append(current.strip())
            current = ""
        current += line + "\n"
    if current.strip():
        result.append(current.strip())
    return result or [text]


def translate_english(text: str):
    translator = GoogleTranslator(source="auto", target="en")
    translated = []
    for chunk in chunks(text):
        translated.append(translator.translate(chunk) or "")
    return "\n".join(translated).strip()


def transcribe(path: str):
    model = get_whisper_model()
    segments, info = model.transcribe(
        path,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    rows = []
    for seg in segments:
        value = seg.text.strip()
        if not value:
            continue
        start, end = max(0, int(seg.start)), max(0, int(seg.end))
        rows.append(
            f"[{start // 3600:02}:{(start % 3600) // 60:02}:{start % 60:02} - "
            f"{end // 3600:02}:{(end % 3600) // 60:02}:{end % 60:02}] {value}"
        )
    return "\n".join(rows), getattr(info, "language", "unknown")


async def send_txt(message, filename: str, text: str):
    with tempfile.TemporaryDirectory() as directory:
        file_path = Path(directory) / filename
        file_path.write_text(text, encoding="utf-8")
        with file_path.open("rb") as file_obj:
            await message.reply_document(document=file_obj, filename=filename)


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📖 Help", callback_data="help"),
         InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("🧰 Commands", callback_data="commands")]
    ]
    await update.effective_message.reply_text(
        "🎬 *ASVM Dialogue Extractor*\n\n"
        "Send a YouTube URL to inspect its metadata, or upload a small video to transcribe it.\n\n"
        "✅ Original-language TXT with timestamps\n"
        "✅ English translation TXT\n"
        "✅ YouTube title/channel/duration via YouTube Data API\n\n"
        "⚠️ YouTube Data API does not download video/audio. For YouTube transcription, use an authorized media source or provide a file you have rights to process.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "How to use:\n"
        "1. Send a YouTube URL: bot returns metadata from YouTube Data API.\n"
        "2. Upload a small video: bot transcribes it locally using faster-whisper.\n"
        "3. Receive original transcript TXT and English translation TXT.\n\n"
        "The official YouTube Data API provides metadata, not arbitrary video/audio downloads. "
        "Use only media you own or are authorized to process."
    )


async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — main menu\n"
        "/help — how to use\n"
        "/commands — command list\n"
        "/id — show your Telegram user ID\n"
        "/status — bot and model configuration status\n"
        "/yt <YouTube URL> — get video metadata\n"
        "/cancel — explain how to stop a running upload (send before starting another job)\n"
        "/admin — admin-only service summary"
    )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_message.reply_text(f"Your Telegram user ID: `{user.id}`", parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🟢 Bot process: online\n"
        f"🧠 Whisper model: {WHISPER_MODEL_NAME} (loads on first transcription)\n"
        f"🎞 YouTube Data API configured: {'yes' if yt else 'no'}\n"
        "🔑 OpenAI API: not used\n"
        f"📦 Upload limit: {MAX_UPLOAD_MB} MB\n"
        "ℹ️ Transcription runs on the Render instance; long videos may exceed its resources."
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.effective_message.reply_text("This command is restricted to configured admins.")
        return
    await update.effective_message.reply_text(
        "🛠 Admin summary\n"
        f"Configured admin IDs: {len(ADMIN_IDS) if ADMIN_IDS else 'not restricted'}\n"
        f"YouTube Data API: {'configured' if yt else 'not configured'}\n"
        f"Whisper: {WHISPER_MODEL_NAME} / {WHISPER_DEVICE} / {WHISPER_COMPUTE_TYPE}"
    )


async def handle_youtube_url(message, url: str):
    video_id = get_video_id(url)
    if not video_id:
        await message.reply_text("Please send a valid YouTube video URL.")
        return
    status_msg = await message.reply_text("🔎 Looking up YouTube video details…")
    try:
        meta = await asyncio.to_thread(youtube_metadata, video_id)
        if not meta:
            await status_msg.edit_text("Video metadata was not found or the video is unavailable.")
            return
        await status_msg.edit_text(
            f"🎬 {meta['title']}\n"
            f"📺 Channel: {meta['channel']}\n"
            f"⏱ Duration: {meta['duration']}\n"
            f"🔒 Visibility: {meta['privacy']}\n"
            f"🔗 {meta['url']}\n\n"
            "The YouTube Data API only returns metadata here; it does not provide the video's audio. "
            "Upload a video file you have permission to process to generate transcript TXT files."
        )
    except Exception:
        log.exception("YouTube API lookup failed")
        await status_msg.edit_text("YouTube API lookup failed. Check YT_API, API quota, and whether YouTube Data API v3 is enabled.")


async def process_uploaded_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    video = message.video
    document = message.document
    file_id = video.file_id if video else document.file_id
    file_size = video.file_size if video else document.file_size
    file_name = (document.file_name if document else None) or "upload.mp4"
    mime_type = (document.mime_type if document else "") or ""

    if file_size and file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.reply_text(
            f"File exceeds {MAX_UPLOAD_MB} MB. Telegram's hosted Bot API download limit applies. "
            "Use a smaller clip or a separately hosted authorized media file."
        )
        return
    if document and not (file_name.lower().endswith((".mp4", ".m4v", ".mov", ".webm")) or mime_type.startswith("video/")):
        await message.reply_text("Please upload a supported video file.")
        return

    status_msg = await message.reply_text("📥 Downloading uploaded video…")
    local_path = None
    try:
        telegram_file = await context.bot.get_file(file_id)
        suffix = Path(file_name).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            local_path = tmp.name
        await telegram_file.download_to_drive(custom_path=local_path)
        await status_msg.edit_text("🧠 Transcribing locally with faster-whisper…")
        original, language = await asyncio.to_thread(transcribe, local_path)
        if not original:
            await status_msg.edit_text("No speech detected in this video.")
            return
        await status_msg.edit_text(f"🌐 Translating to English (detected language: {language})…")
        english = await asyncio.to_thread(translate_english, original)
        await send_txt(message, "original_transcript.txt", original)
        await send_txt(message, "english_translation.txt", english)
        await status_msg.edit_text("✅ Finished. TXT files are attached above.")
    except Exception:
        log.exception("Video processing failed")
        await status_msg.edit_text("❌ Processing failed. Check Render logs, server RAM/disk, and translation connectivity.")
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if get_video_id(text):
        await handle_youtube_url(update.effective_message, text)
    else:
        await update.effective_message.reply_text("Send a YouTube URL or upload a small video file. Use /help.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await query.edit_message_text("Send a YouTube URL for metadata or upload a small video for transcription. Use /help for details.")
    elif query.data == "status":
        await query.edit_message_text(
            f"🟢 Online\nWhisper: {WHISPER_MODEL_NAME}\nYouTube API: {'configured' if yt else 'not configured'}\nOpenAI API: not used"
        )
    elif query.data == "commands":
        await query.edit_message_text("/start /help /commands /id /status /yt /admin")


async def yt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args).strip()
    if not url:
        await update.effective_message.reply_text("Usage: /yt https://www.youtube.com/watch?v=VIDEO_ID")
        return
    await handle_youtube_url(update.effective_message, url)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "This starter processes one task per update and does not keep a cancellable job queue. "
        "For long jobs, use a VPS/background worker with a job queue and cancellation support."
    )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_cmd))
telegram_app.add_handler(CommandHandler("commands", commands_cmd))
telegram_app.add_handler(CommandHandler("id", id_cmd))
telegram_app.add_handler(CommandHandler("status", status_cmd))
telegram_app.add_handler(CommandHandler("admin", admin_cmd))
telegram_app.add_handler(CommandHandler("yt", yt_cmd))
telegram_app.add_handler(CommandHandler("cancel", cancel_cmd))
telegram_app.add_handler(CallbackQueryHandler(button_handler))
telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.Document.FileExtension("mp4"), process_uploaded_video))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/telegram/{WEBHOOK_SECRET}",
        allowed_updates=["message", "callback_query"],
    )
    log.info("Webhook registered.")
    yield
    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(title="ASVM Dialogue Extractor", lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "service": "ASVM Dialogue Extractor",
        "status": "ok",
        "openai_api": False,
        "youtube_data_api": bool(yt),
        "youtube_api_scope": "metadata only; no video/audio download",
    }


@app.post("/telegram/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = Update.de_json(await request.json(), telegram_app.bot)
        asyncio.create_task(telegram_app.process_update(update))
    except Exception as exc:
        log.exception("Webhook update failed")
        raise HTTPException(status_code=400, detail="Invalid update") from exc
    return {"ok": True}
