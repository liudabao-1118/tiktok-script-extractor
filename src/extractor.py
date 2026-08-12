import os
import subprocess
import tempfile
import requests
import json
import time


class TikTokExtractor:
    """Extract transcripts from TikTok videos using tikwm.com API + Whisper fallback."""

    TIKWM_API = "https://www.tikwm.com/api/"

    def __init__(self, whisper_model="tiny", language=None):
        self.whisper_model = whisper_model
        self.language = language
        self._whisper_model = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def get_video_info(self, url, retries=3):
        """Fetch video metadata from tikwm.com API (with rate-limit retry)."""
        for attempt in range(retries):
            try:
                resp = self.session.get(
                    self.TIKWM_API,
                    params={"url": url},
                    timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0:
                    return data.get("data", {})
                msg = data.get("msg", "Unknown API error")
                # Rate limit (free tier: 1 req/s) -> back off and retry
                if "limit" in msg.lower() or "rate" in msg.lower():
                    time.sleep(2.5)
                    continue
                return {"status": "error", "error_message": msg}
            except requests.RequestException as e:
                time.sleep(2.5)
                if attempt == retries - 1:
                    return {"status": "error", "error_message": f"tikwm API failed: {str(e)}"}
            except (json.JSONDecodeError, KeyError) as e:
                return {"status": "error", "error_message": f"Parse error: {str(e)}"}
        return {"status": "error", "error_message": "tikwm retry exhausted"}

    def download_audio(self, video_url, output_path):
        """Download video and extract audio using ffmpeg."""
        try:
            resp = self.session.get(video_url, timeout=60, stream=True)
            resp.raise_for_status()
            video_file = os.path.join(output_path, "video.mp4")
            with open(video_file, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            audio_file = os.path.join(output_path, "audio.mp3")
            result = subprocess.run(
                ["ffmpeg", "-i", video_file, "-vn", "-acodec", "libmp3lame",
                 "-q:a", "4", audio_file, "-y"],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                return None, f"ffmpeg failed: {result.stderr.decode()[:200]}"

            os.remove(video_file)
            return audio_file, None
        except Exception as e:
            return None, str(e)

    def _get_whisper_model(self):
        """Lazily load the Whisper model."""
        if self._whisper_model is None:
            from faster_whisper import WhisperModel
            self._whisper_model = WhisperModel(
                self.whisper_model,
                device="cpu",
                compute_type="int8"
            )
        return self._whisper_model

    def transcribe_audio(self, audio_path):
        """Transcribe audio file using faster-whisper."""
        try:
            model = self._get_whisper_model()
            segments, info = model.transcribe(
                audio_path,
                language=self.language,
                vad_filter=True,
                beam_size=5
            )
            transcript = " ".join([seg.text.strip() for seg in segments])
            return {
                "transcript": transcript.strip(),
                "language": info.language if info else "unknown",
                "error": None
            }
        except Exception as e:
            return {
                "transcript": "",
                "language": "unknown",
                "error": str(e)
            }

    def extract(self, url):
        """
        Main extraction method.
        Returns dict with: video_id, author, description, original_text,
                          language, duration, status, error_message
        """
        import re
        from utils import extract_video_id, extract_author

        video_id = extract_video_id(url)
        author = extract_author(url)

        result = {
            "video_id": video_id or "",
            "url": url,
            "author": author,
            "description": "",
            "original_text": "",
            "translated_text": "",
            "language": "",
            "duration": "",
            "status": "pending",
            "error_message": ""
        }

        # Step 1: Get video info from tikwm.com (also resolves short URLs)
        # Strip tracking query params (?lang= / &is_copy_url= ...) which break tikwm parsing
        clean_url = url.split("?")[0].split("#")[0]
        info = self.get_video_info(clean_url)
        if info.get("status") == "error":
            result["status"] = "error"
            result["error_message"] = info.get("error_message", "Unknown error")
            return result

        # Use API's video ID as authoritative source
        if not video_id and info.get("id"):
            video_id = str(info["id"])
            result["video_id"] = video_id

        result["description"] = info.get("title", "")
        result["duration"] = str(info.get("duration", "")) + "s"

        author_info = info.get("author", {})
        if isinstance(author_info, dict):
            if not result["author"]:
                result["author"] = "@" + author_info.get("unique_id", author_info.get("nickname", ""))

        # Step 2: Try to get subtitles from tikwm.com (if available)
        subtitle_url = info.get("subtitle") or info.get("subtitle_url")
        if subtitle_url:
            try:
                sub_resp = self.session.get(subtitle_url, timeout=15)
                sub_resp.raise_for_status()
                subtitle_text = self._parse_subtitle(sub_resp.text)
                if subtitle_text:
                    result["original_text"] = subtitle_text
                    result["language"] = "auto"
                    result["status"] = "success"
                    return result
            except Exception:
                pass

        # Step 3: Resolve a downloadable video URL.
        # Prefer tikwm play URL; if unavailable (e.g. anker_uk returns parse error),
        # fall back to yt-dlp which handles most TikTok URLs directly.
        play_url = info.get("play") or info.get("play_no_watermark") or info.get("wmplay")
        if not play_url and info.get("status") != "error":
            play_url = self._get_ytdlp_url(clean_url)
        if not play_url:
            # tikwm also failed -> try yt-dlp on the cleaned URL
            play_url = self._get_ytdlp_url(clean_url)

        if not play_url:
            # If no play URL but we have description, use that as text
            if result["description"]:
                result["original_text"] = result["description"]
                result["status"] = "success_desc_only"
                return result
            result["status"] = "error"
            result["error_message"] = "No video download URL available (tikwm + yt-dlp both failed)"
            return result

        # Step 4: Transcribe with Whisper (unless skipped via env SKIP_WHISPER)
        if os.environ.get("SKIP_WHISPER"):
            result["status"] = "need_whisper"
            result["error_message"] = "Whisper skipped (SKIP_WHISPER set)"
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file, err = self.download_audio(play_url, tmpdir)
            if err:
                result["status"] = "error"
                result["error_message"] = f"Download/ffmpeg error: {err}"
                return result

            whisper_result = self.transcribe_audio(audio_file)
            if whisper_result["error"]:
                result["status"] = "error"
                result["error_message"] = f"Whisper error: {whisper_result['error']}"
                return result

            result["original_text"] = whisper_result["transcript"]
            result["language"] = whisper_result["language"]
            result["status"] = "success" if whisper_result["transcript"] else "no_audio_detected"

        return result

    def _get_ytdlp_url(self, url):
        """Resolve a direct video URL via yt-dlp when tikwm cannot."""
        try:
            import yt_dlp
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "format": "best[ext=mp4]/best",
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("url") or None
        except Exception:
            return None

    @staticmethod
    def _parse_subtitle(content):
        """Parse WebVTT or SRT subtitle content into plain text."""
        lines = content.split('\n')
        text_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.isdigit():
                continue
            if '-->' in line:
                continue
            if line.startswith('WEBVTT'):
                continue
            if line.startswith('NOTE'):
                continue
            text_lines.append(line)
        return ' '.join(text_lines)
