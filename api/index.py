import os
import re
import json
import shutil
import tempfile
import urllib.parse

from flask import Flask, request, jsonify, send_file
import yt_dlp

app = Flask(__name__)

MAX_DURATION = int(os.environ.get("MAX_DURATION", "1800"))
FFMPEG_PATH = shutil.which("ffmpeg")


def validate_youtube_url(url):
    if not url:
        raise ValueError("Missing required parameter: url")

    parsed = urllib.parse.urlparse(url)

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

    if (parsed.hostname or "").lower() not in allowed_hosts:
        raise ValueError("Only YouTube URLs are supported")

    return url


def get_ydl_options():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }

    # Optional: a Netscape-format cookie file supplied as
    # Base64 through the YOUTUBE_COOKIES_B64 environment variable.
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()

    if cookies_b64:
        import base64

        cookie_data = base64.b64decode(cookies_b64, validate=True)

        # Use a temporary writable file instead of /var/task.
        cookie_file = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".txt",
            prefix="yt-cookies-",
            dir="/tmp",
            delete=False,
        )

        try:
            cookie_file.write(cookie_data)
            cookie_file.close()
            options["cookiefile"] = cookie_file.name
        except Exception:
            cookie_file.close()
            try:
                os.unlink(cookie_file.name)
            except OSError:
                pass
            raise

    if FFMPEG_PATH:
        options["ffmpeg_location"] = FFMPEG_PATH

    return options


def extract_info(url):
    options = get_ydl_options()
    options["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        duration = info.get("duration") or 0

        if MAX_DURATION and duration > MAX_DURATION:
            raise ValueError(
                f"Video is too long. Maximum duration: {MAX_DURATION} seconds"
            )

        return {
            "title": info.get("title", "YouTube audio"),
            "duration": duration,
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url", url),
        }

    finally:
        cookie_path = options.get("cookiefile")
        if cookie_path:
            try:
                os.unlink(cookie_path)
            except OSError:
                pass


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "name": "YouTube Media API",
        "endpoints": {
            "info": "/api/info?url=YOUTUBE_URL",
            "mp3": "/api?url=YOUTUBE_URL&quality=192",
            "health": "/api/healthz",
        },
    })


@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({"success": True, "status": "ok"})


@app.route("/api/info", methods=["GET"])
def video_info():
    try:
        url = validate_youtube_url(request.args.get("url", ""))
        info = extract_info(url)

        return jsonify({
            "success": True,
            "data": info,
        })

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


@app.route("/api", methods=["GET"])
def download_mp3():
    temp_dir = None
    options = None

    try:
        url = validate_youtube_url(request.args.get("url", ""))

        quality = request.args.get("quality", "192")

        if quality not in {"64", "128", "192", "320"}:
            return jsonify({
                "success": False,
                "error": "quality must be 64, 128, 192, or 320",
            }), 400

        if not FFMPEG_PATH:
            return jsonify({
                "success": False,
                "error": "FFmpeg is not available in this deployment",
            }), 500

        temp_dir = tempfile.mkdtemp(prefix="youtube-", dir="/tmp")

        options = get_ydl_options()
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
            candidates = [
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if name.lower().endswith(".mp3")
            ]

            if not candidates:
                raise RuntimeError("MP3 conversion did not produce an output file")

            output_file = candidates[0]

        title = info.get("title", "audio")
        safe_title = re.sub(r'[^\w .-]', "", title).strip()[:100]
        safe_title = safe_title or "audio"

        response = send_file(
            output_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{safe_title}.mp3",
            conditional=False,
        )

        # Cleanup happens when the response is closed.
        @response.call_on_close
        def cleanup():
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

            if options:
                cookie_path = options.get("cookiefile")
                if cookie_path:
                    try:
                        os.unlink(cookie_path)
                    except OSError:
                        pass

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        return response

    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if options:
            cookie_path = options.get("cookiefile")
            if cookie_path:
                try:
                    os.unlink(cookie_path)
                except OSError:
                    pass

        return jsonify({
            "success": False,
            "error": "Audio download or conversion failed",
            "details": str(exc)[:500],
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
