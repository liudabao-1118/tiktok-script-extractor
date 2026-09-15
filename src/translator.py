"""Translate TikTok scripts to Chinese with automatic engine failover.

The translation column used to come back blank because the free Google
endpoint rate-limits shared cloud IPs (GitHub runners): firing 100 requests
at once gets every one of them rejected with
"You made too many requests to the server".

Three layers fix that:

  1. Throttling - requests are spaced out (Google allows ~5 req/s per IP).
  2. Backoff    - rate-limit errors are retried with growing delays.
  3. Failover   - if Google keeps refusing, the run switches engine instead
                  of burning minutes on doomed retries:
                    Google (deep_translator) -> Google (gtx endpoint)
                    -> MyMemory (free, no key)

The circuit breaker stops hammering Google once it is clearly throttled, so
the remaining rows go straight to the fallback engine.
"""

import json
import time
import urllib.parse
import urllib.request

from deep_translator import GoogleTranslator


class Translator:
    """Translate text to Chinese, with automatic engine failover."""

    # --- tuning ---------------------------------------------------------
    GOOGLE_MIN_INTERVAL = 0.35      # ~3 req/s, safely under Google's 5 req/s
    GTX_MIN_INTERVAL = 0.35
    MYMEMORY_MIN_INTERVAL = 0.25

    GOOGLE_MAX_ATTEMPTS = 3
    GOOGLE_TRIP_AFTER = 3           # consecutive failures -> stop trying Google

    MYMEMORY_CHUNK = 400            # MyMemory caps a single request at ~500 bytes
    MYMEMORY_MAX_ATTEMPTS = 3

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, target_lang="zh-CN", max_length=4500):
        self.target_lang = target_lang
        self.max_length = max_length

        self._google = GoogleTranslator(source="auto", target=target_lang)
        self._last_google = 0.0
        self._last_gtx = 0.0
        self._last_mymemory = 0.0

        self._google_failures = 0
        self._google_tripped = False
        self._gtx_tripped = False

        # Visibility into which engine actually did the work
        self.engine_stats = {"google": 0, "gtx": 0, "mymemory": 0, "failed": 0}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def translate(self, text):
        """Return the Chinese translation, or "" when every engine failed."""
        if not text or not text.strip():
            return ""

        text = text.strip()

        if len(text) <= self.max_length:
            return self._safe_translate(text)

        chunks = self._chunk_text(text, self.max_length)
        results = []
        for chunk in chunks:
            translated = self._safe_translate(chunk)
            if translated:
                results.append(translated)
        return " ".join(results)

    def stats_line(self):
        """One-line summary of which engines were used this run."""
        s = self.engine_stats
        return (
            f"translation -> google: {s['google']}, google-gtx: {s['gtx']}, "
            f"mymemory: {s['mymemory']}, failed: {s['failed']}"
        )

    # ------------------------------------------------------------------
    # engine orchestration
    # ------------------------------------------------------------------
    def _safe_translate(self, text):
        if not self._google_tripped:
            result = self._via_google(text)
            if result:
                self.engine_stats["google"] += 1
                return result

        if not self._gtx_tripped:
            result = self._via_gtx(text)
            if result:
                self.engine_stats["gtx"] += 1
                return result

        result = self._via_mymemory(text)
        if result:
            self.engine_stats["mymemory"] += 1
            return result

        self.engine_stats["failed"] += 1
        return ""

    # ------------------------------------------------------------------
    # engine 1: Google via deep_translator
    # ------------------------------------------------------------------
    def _via_google(self, text):
        for attempt in range(self.GOOGLE_MAX_ATTEMPTS):
            self._throttle("_last_google", self.GOOGLE_MIN_INTERVAL)
            try:
                result = self._google.translate(text)
                if result and result.strip():
                    self._google_failures = 0
                    return result.strip()
                return ""
            except Exception as e:
                message = str(e)
                if self._is_rate_limited(message):
                    self._google_failures += 1
                    if self._google_failures >= self.GOOGLE_TRIP_AFTER:
                        self._google_tripped = True
                        print(
                            "  Google is throttling this IP - switching to "
                            "fallback engines for the rest of the run"
                        )
                        return ""
                    delay = 1.5 * (attempt + 1)
                    print(f"  Google rate limited, retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                print(f"  Translation error (google): {message[:110]}")
                return ""
        return ""

    # ------------------------------------------------------------------
    # engine 2: Google gtx endpoint (separate quota bucket in practice)
    # ------------------------------------------------------------------
    def _via_gtx(self, text):
        url = "https://translate.googleapis.com/translate_a/single"
        for attempt in range(2):
            self._throttle("_last_gtx", self.GTX_MIN_INTERVAL)
            try:
                params = urllib.parse.urlencode(
                    {
                        "client": "gtx",
                        "sl": "auto",
                        "tl": self.target_lang,
                        "dt": "t",
                        "q": text,
                    }
                )
                req = urllib.request.Request(
                    f"{url}?{params}", headers={"User-Agent": self.USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                segments = data[0] if data else []
                out = "".join(seg[0] for seg in segments if seg and seg[0])
                return out.strip()
            except Exception as e:
                message = str(e)
                if self._is_rate_limited(message):
                    if attempt == 0:
                        time.sleep(2.0)
                        continue
                    self._gtx_tripped = True
                    print("  Google gtx endpoint throttled too - using MyMemory")
                    return ""
                return ""
        return ""

    # ------------------------------------------------------------------
    # engine 3: MyMemory (free, no API key) - guaranteed fallback
    # ------------------------------------------------------------------
    def _via_mymemory(self, text):
        pieces = [
            text[i : i + self.MYMEMORY_CHUNK]
            for i in range(0, len(text), self.MYMEMORY_CHUNK)
        ]
        translated_pieces = []

        for piece in pieces:
            piece_result = ""
            for attempt in range(self.MYMEMORY_MAX_ATTEMPTS):
                self._throttle("_last_mymemory", self.MYMEMORY_MIN_INTERVAL)
                try:
                    params = urllib.parse.urlencode(
                        {"q": piece, "langpair": f"Autodetect|{self.target_lang}"}
                    )
                    req = urllib.request.Request(
                        f"https://api.mymemory.translated.net/get?{params}",
                        headers={"User-Agent": self.USER_AGENT},
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = json.loads(resp.read().decode("utf-8", "ignore"))

                    candidate = (data.get("responseData") or {}).get("translatedText") or ""
                    candidate = candidate.strip()

                    # MyMemory reports quota/limit problems inside the payload
                    if candidate.upper().startswith("MYMEMORY WARNING") or not candidate:
                        if attempt < self.MYMEMORY_MAX_ATTEMPTS - 1:
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        print(f"  MyMemory quota/limit: {candidate[:90]}")
                        break

                    piece_result = candidate
                    break
                except Exception as e:
                    if attempt == self.MYMEMORY_MAX_ATTEMPTS - 1:
                        print(f"  Translation error (mymemory): {str(e)[:110]}")
                    else:
                        time.sleep(1.5 * (attempt + 1))

            if piece_result:
                translated_pieces.append(piece_result)

        return " ".join(translated_pieces)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _throttle(self, attr, interval):
        """Space consecutive calls so we stay under the endpoint's rate limit."""
        now = time.time()
        elapsed = now - getattr(self, attr)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        setattr(self, attr, time.time())

    @staticmethod
    def _is_rate_limited(message):
        lower = message.lower()
        return (
            "too many requests" in lower
            or "429" in lower
            or "rate limit" in lower
            or "quota" in lower
        )

    @staticmethod
    def _chunk_text(text, max_length):
        """Split text into chunks at sentence boundaries."""
        import re

        sentences = re.split(r"(?<=[.!?。！？])\s+", text)
        chunks = []
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_length:
                current = (current + " " + sentence).strip() if current else sentence
            else:
                if current:
                    chunks.append(current)
                if len(sentence) <= max_length:
                    current = sentence
                else:
                    for i in range(0, len(sentence), max_length):
                        chunks.append(sentence[i : i + max_length])
                    current = ""
        if current:
            chunks.append(current)
        return chunks
