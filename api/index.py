import os
import re
import json
import shutil
import tempfile
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

import yt_dlp
import imageio_ffmpeg


def validate_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
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


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [""])[0]

        if not url:
            return self.send_json(400, {
                "success": False,
                "error": "Missing URL parameter",
                "usage": "/api?url=https://www.youtube.com/watch?v=VIDEO_ID"
            })

        video_id = validate_youtube_url(url)

        if not video_id:
            return self.send_json(400, {
                "success": False,
                "error": "Invalid YouTube URL"
            })

        temp_dir = tempfile.mkdtemp(prefix="ytmp3_")

        try:
            # Locate FFmpeg bundled by imageio-ffmpeg.
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

            output_template = os.path.join(
                temp_dir,
                "audio.%(ext)s"
            )

            options = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 15,
                "retries": 1,
                "ffmpeg_location": ffmpeg_path,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192"
                    }
                ]
            }

            # Download and convert audio.
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])

            mp3_path = os.path.join(temp_dir, "audio.mp3")

            if not os.path.isfile(mp3_path):
                return self.send_json(500, {
                    "success": False,
                    "error": "MP3 file was not generated"
                })

            file_size = os.path.getsize(mp3_path)

            # Conservative response-size limit.
            if file_size > 4 * 1024 * 1024:
                return self.send_json(413, {
                    "success": False,
                    "error": "MP3 exceeds the 4 MiB response limit",
                    "file_size_bytes": file_size,
                    "message": (
                        "Vercel response and execution limits may "
                        "prevent larger downloads."
                    )
                })

            with open(mp3_path, "rb") as audio_file:
                audio_data = audio_file.read()

            # Return the MP3 as a downloadable attachment.
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
            print("YouTube MP3 error:", repr(error))

            self.send_json(500, {
                "success": False,
                "error": "Audio download or conversion failed",
                "details": str(error)[:500]
            })

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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
