import os
import re
import json
import shutil
import tempfile
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

import yt_dlp
import imageio_ffmpeg


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")


# --------------------------------------------------
# YOUTUBE URL VALIDATION
# --------------------------------------------------

def validate_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if host in (
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        ):
            video_id = parse_qs(parsed.query).get("v", [""])[0]

        elif host == "youtu.be":
            video_id = parsed.path.strip("/")

        else:
            return None

        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return video_id

        return None

    except Exception:
        return None


# --------------------------------------------------
# API HANDLER
# --------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [""])[0].strip()

        # Validate request
        if not url:
            return self.send_json(400, {
                "success": False,
                "error": "Missing URL parameter",
                "usage": "/api?url=YOUTUBE_URL"
            })

        video_id = validate_youtube_url(url)

        if not video_id:
            return self.send_json(400, {
                "success": False,
                "error": "Invalid YouTube URL"
            })

        temp_dir = tempfile.mkdtemp(prefix="ytmp3_")

        try:
            # Locate FFmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

            # Output file
            output_template = os.path.join(
                temp_dir,
                "audio.%(ext)s"
            )

            # yt-dlp options
            options = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 2,
                "ffmpeg_location": ffmpeg_path,

                # Use cookies only if the file exists.
                "cookiefile": (
                    COOKIES_FILE
                    if os.path.isfile(COOKIES_FILE)
                    else None
                ),

                # Convert audio to MP3.
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192"
                    }
                ]
            }

            # Remove the optional setting if no cookies exist.
            if options["cookiefile"] is None:
                options.pop("cookiefile")

            # Download and convert.
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])

            mp3_path = os.path.join(temp_dir, "audio.mp3")

            if not os.path.isfile(mp3_path):
                return self.send_json(500, {
                    "success": False,
                    "error": "MP3 file was not generated"
                })

            file_size = os.path.getsize(mp3_path)

            # Vercel responses have platform-specific limits.
            # This is an application-level safeguard.
            max_size = 4 * 1024 * 1024

            if file_size > max_size:
                return self.send_json(413, {
                    "success": False,
                    "error": "MP3 exceeds the configured response limit",
                    "file_size_bytes": file_size,
                    "message": (
                        "Use a compatible streaming or storage service "
                        "for larger audio files."
                    )
                })

            # Read the converted MP3.
            with open(mp3_path, "rb") as audio_file:
                audio_data = audio_file.read()

            # Return an automatic download response.
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="audio.mp3"'
            )
            self.send_header(
                "Content-Length",
                str(len(audio_data))
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            self.wfile.write(audio_data)

        except Exception as error:
            print("Audio processing error:", repr(error))

            self.send_json(500, {
                "success": False,
                "error": "Audio download or conversion failed",
                "details": str(error)[:500]
            })

        finally:
            # Delete temporary files after processing.
            shutil.rmtree(temp_dir, ignore_errors=True)

    # --------------------------------------------------
    # JSON RESPONSE
    # --------------------------------------------------

    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)
