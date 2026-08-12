from deep_translator import GoogleTranslator


class Translator:
    """Translate text to Chinese using Google Translate (free, no API key)."""

    def __init__(self, target_lang="zh-CN", max_length=4500):
        self.target_lang = target_lang
        self.max_length = max_length
        self.translator = GoogleTranslator(source="auto", target=target_lang)

    def translate(self, text):
        """
        Translate text to the target language.
        Handles long text by chunking.
        Returns translated string, or empty string on failure.
        """
        if not text or not text.strip():
            return ""

        text = text.strip()

        # If text is short enough, translate directly
        if len(text) <= self.max_length:
            return self._safe_translate(text)

        # Chunk long text
        chunks = self._chunk_text(text, self.max_length)
        results = []
        for chunk in chunks:
            translated = self._safe_translate(chunk)
            if translated:
                results.append(translated)
        return " ".join(results)

    def _safe_translate(self, text):
        """Translate with error handling."""
        try:
            result = self.translator.translate(text)
            return result or ""
        except Exception as e:
            print(f"  Translation error: {e}")
            return ""

    @staticmethod
    def _chunk_text(text, max_length):
        """Split text into chunks at sentence boundaries."""
        import re
        sentences = re.split(r'(?<=[.!?。！？])\s+', text)
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
                    # Force split very long sentences
                    for i in range(0, len(sentence), max_length):
                        chunks.append(sentence[i:i + max_length])
                    current = ""
        if current:
            chunks.append(current)
        return chunks
