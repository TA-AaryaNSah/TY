# ASVM Dialogue Extractor — Render + YouTube Data API

## What this bot does
- `/start`, `/help`, `/commands`, `/id`, `/status`, `/yt`, `/admin`, `/cancel`
- YouTube Data API v3 lookup for title, channel, duration, and visibility.
- Small video upload transcription using local `faster-whisper`.
- Original transcript TXT with timestamps + English translation TXT.
- No OpenAI API key or OpenAI API is used.

## Critical YouTube API limitation
**YouTube Data API v3 cannot download arbitrary YouTube video/audio.** It provides metadata and other permitted resources, not an MP4/audio download endpoint. This bot uses YT_API only for metadata. To transcribe a YouTube video, use captions you are authorized to access or upload media you own/are permitted to process. It intentionally does not download arbitrary YouTube video/audio.

## Security
The user previously supplied bot/API credentials in chat. Revoke/rotate the Telegram bot token via `@BotFather` and rotate any exposed keys before deploying. Never hardcode active secrets in `main.py` or commit them to GitHub. Set values in Render Environment.

`API_ID` and `API_HASH` are included as environment variables because requested, but this Bot API implementation does not use them. They are for MTProto clients such as Pyrogram/Telethon, not needed for normal Telegram Bot API webhooks.

## Render deployment
1. Rotate exposed credentials first.
2. Upload repository to GitHub.
3. Create a Render Web Service using Dockerfile / `render.yaml`.
4. Set environment variables:
   - `BOT_TOKEN`
   - `API_ID` (optional; unused here)
   - `API_HASH` (optional; unused here)
   - `YT_API` (YouTube Data API v3 key)
   - `PUBLIC_BASE_URL` (your Render HTTPS URL, no trailing slash)
   - `WEBHOOK_SECRET` (long random string; render.yaml can generate it)
   - `ADMIN_IDS` (comma-separated Telegram numeric IDs, optional)
   - `WHISPER_MODEL=small`
   - `WHISPER_DEVICE=cpu`
   - `WHISPER_COMPUTE_TYPE=int8`
   - `MAX_UPLOAD_MB=19`
5. Deploy and message the bot `/start`.

## Performance notes
- Whisper model downloads on first transcription and can take time.
- Render CPU-only instances may process 10–30 minute videos slowly. RAM and disk depend on the Render plan.
- Telegram's hosted Bot API has file-download limits; this starter is for small uploads.
- Translation uses `deep-translator` and may be rate-limited.
- Render filesystem is ephemeral; temporary uploaded files are deleted after processing.
