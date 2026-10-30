import os
import re
import json
import base64
import shutil
import tempfile
import urllib.parse

from flask import Flask, request, jsonify, send_file
import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)

MAX_DURATION = int(os.environ.get("MAX_DURATION", "1800"))

# Find FFmpeg using PATH first, then imageio-ffmpeg.
try:
    FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = shutil.which("ffmpeg")


def validate_youtube_url(url):
    if not url:
        raise ValueError("Missing required parameter: url")

    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP/HTTPS URLs are supported")

    if hostname not in allowed_hosts:
        raise ValueError("Only YouTube URLs are supported")

    return url


def create_ydl_options():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }

    if FFMPEG_PATH:
        options["ffmpeg_location"] = FFMPEG_PATH

    # Optional Base64-encoded Netscape cookies supplied through
    # the YOUTUBE_COOKIES_B64 environment variable.
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()

    if cookies_b64:
        cookie_path = None

        try:
            cookie_data = base64.b64decode(
                cookies_b64,
                validate=True
            )

            # Basic format validation; do not log cookie contents.
            cookie_text = cookie_data.decode("utf-8-sig")

            if "Netscape HTTP Cookie File" not in cookie_text[:200]:
                raise ValueError(
                    "Cookies must be in Netscape HTTP Cookie File format"
                )

            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".txt",
                prefix="yt-cookies-",
                dir="/tmp",
                delete=False,
            ) as cookie_file:
                cookie_file.write(cookie_data)
                cookie_path = cookie_file.name

            options["cookiefile"] = cookie_path

        except Exception:
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass
            raise

    return options


def cleanup_cookie(options):
    cookie_path = options.get("cookiefile")

    if cookie_path:
        try:
            os.unlink(cookie_path)
        except OSError:
            pass


def extract_video_info(url):
    options = create_ydl_options()
    options["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        duration = info.get("duration") or 0

        if MAX_DURATION > 0 and duration > MAX_DURATION:
            raise ValueError(
                f"Video exceeds the maximum duration of {MAX_DURATION} seconds"
            )

        return {
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": duration,
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "webpage_url": info.get("webpage_url", url),
            "extractor": info.get("extractor"),
        }

    finally:
        cleanup_cookie(options)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "name": "YouTube Media API",
        "version": "2.0",
        "endpoints": {
            "info": "/api/info?url=YOUTUBE_URL",
            "mp3": "/api?url=YOUTUBE_URL&quality=192",
            "health": "/api/healthz",
        },
    })


@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "status": "ok",
        "ffmpeg_available": bool(FFMPEG_PATH),
    })


@app.route("/api/info", methods=["GET"])
def video_info():
    try:
        url = validate_youtube_url(request.args.get("url", ""))
        info = extract_video_info(url)

        return jsonify({
            "success": True,
            "data": info,
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": "Unable to retrieve video information",
            "details": str(exc)[:500],
        }), 500


@app.route("/api", methods=["GET"])
def download_mp3():
    temp_dir = None
    options = {}

    try:
        url = validate_youtube_url(request.args.get("url", ""))

        quality = request.args.get("quality", "192")

        if quality not in {"64", "128", "192", "256", "320"}:
            return jsonify({
                "success": False,
                "error": "quality must be 64, 128, 192, 256, or 320",
            }), 400

        if not FFMPEG_PATH or not os.path.isfile(FFMPEG_PATH):
            return jsonify({
                "success": False,
                "error": "FFmpeg executable is unavailable",
                "details": (
                    "Check imageio-ffmpeg installation and deployment logs."
                ),
            }), 500

        temp_dir = tempfile.mkdtemp(
            prefix="youtube-audio-",
            dir="/tmp",
        )

        options = create_ydl_options()

        options.update({
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }],
        })

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        video_id = info.get("id", "")
        output_file = os.path.join(temp_dir, f"{video_id}.mp3")

        if not os.path.isfile(output_file):
            mp3_files = [
                os.path.join(temp_dir, filename)
                for filename in os.listdir(temp_dir)
                if filename.lower().endswith(".mp3")
            ]

            if not mp3_files:
                raise RuntimeError(
                    "MP3 conversion did not produce an output file"
                )

            output_file = mp3_files[0]

        title = info.get("title") or "audio"
        safe_title = re.sub(r"[^\w .-]", "", title).strip()[:100]
        safe_title = safe_title or "audio"

        response = send_file(
            output_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{safe_title}.mp3",
            conditional=False,
        )

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        def cleanup():
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

            cleanup_cookie(options)

        response.call_on_close(cleanup)

        return response

    except ValueError as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

        cleanup_cookie(options)

        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

        cleanup_cookie(options)

        return jsonify({
            "success": False,
            "error": "Audio download or conversion failed",
            "details": str(exc)[:500],
        }), 500


# Vercel imports this module and uses the Flask WSGI application.
# Do not start app.run() in a serverless deployment.
