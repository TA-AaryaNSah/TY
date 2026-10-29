import os
import re
import json
import shutil
import tempfile
import subprocess
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

import yt_dlp


def validate_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif host == "youtu.be":
            video_id = parsed.path.strip("/")
        else:
            return False

        return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id))
    except Exception:
        return False


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [""])[0]

        if not url:
            return self.send_json(400, {
                "success": False,
                "error": "Missing url parameter",
                "usage": "/api?url=YOUR_YOUTUBE_URL"
            })

        if not validate_url(url):
            return self.send_json(400, {
                "success": False,
                "error": "Invalid YouTube URL"
            })

        # Check that FFmpeg is available in the deployment.
        if not shutil.which("ffmpeg"):
            return self.send_json(503, {
                "success": False,
                "error": "FFmpeg is not available in this deployment.",
                "details": (
                    "Vercel must have a compatible FFmpeg binary "
                    "before audio conversion can work."
                )
            })

        temp_dir = tempfile.mkdtemp(prefix="ytmp3_")

        try:
            output_template = os.path.join(temp_dir, "audio.%(ext)s")

            options = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 15,
                "retries": 1,
                "overwrites": True,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }

            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])

            mp3_path = os.path.join(temp_dir, "audio.mp3")

            if not os.path.isfile(mp3_path):
                return self.send_json(500, {
                    "success": False,
                    "error": "MP3 conversion did not produce an output file."
                })

            # Serverless response limits make streaming large files
            # directly from a temporary file unreliable.
            file_size = os.path.getsize(mp3_path)

            if file_size > 4 * 1024 * 1024:
                return self.send_json(413, {
                    "success": False,
                    "error": "MP3 exceeds the configured response-size limit.",
                    "file_size_bytes": file_size,
                    "suggestion": (
                        "Use a compatible audio-processing service "
                        "for larger files."
                    )
                })

            with open(mp3_path, "rb") as audio_file:
                audio_data = audio_file.read()

            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="audio.mp3"'
            )
            self.send_header("Content-Length", str(len(audio_data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio_data)

        except Exception:
            self.send_json(500, {
                "success": False,
                "error": "Audio extraction failed.",
                "suggestion": (
                    "Check FFmpeg availability, deployment logs, "
                    "execution limits, and whether the video is accessible."
                )
            })

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
