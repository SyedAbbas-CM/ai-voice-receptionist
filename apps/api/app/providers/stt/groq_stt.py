from __future__ import annotations

import httpx

from app.core.config import settings

from ..base import STTProvider


class GroqSTT(STTProvider):
    """Groq Whisper. Very fast and currently free up to a generous per-file limit."""

    name = "groq"

    def __init__(self) -> None:
        self.api_key = settings.groq_api_key
        self.model = settings.groq_stt_model or "whisper-large-v3-turbo"

    # Whisper models hallucinate on very short / silent audio. The most common
    # hallucinations are literal Whisper training-set filler like "Oh my god",
    # "Thank you.", "you", "Bye.". So we reject too-short clips before wasting
    # an API call and returning garbage to the brain.
    _MIN_AUDIO_BYTES = 4000  # ~0.25s at 16kHz mono, very generous lower bound

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        if not audio_bytes or len(audio_bytes) < self._MIN_AUDIO_BYTES:
            return ""

        # Groq's transcription endpoint chokes on codec suffixes like
        # "audio/webm;codecs=opus" — strip to a clean top-level mime.
        clean_mime = mime.split(";")[0].strip().lower() or "audio/wav"
        if "wav" in clean_mime:
            ext, clean_mime = "wav", "audio/wav"
        elif "webm" in clean_mime:
            ext, clean_mime = "webm", "audio/webm"
        elif "ogg" in clean_mime:
            ext, clean_mime = "ogg", "audio/ogg"
        elif "mp3" in clean_mime or "mpeg" in clean_mime:
            ext, clean_mime = "mp3", "audio/mpeg"
        elif "mp4" in clean_mime or "m4a" in clean_mime:
            ext, clean_mime = "m4a", "audio/mp4"
        else:
            # Unknown — try wav as a last resort; better than crashing
            ext, clean_mime = "wav", "audio/wav"

        files = {"file": (f"audio.{ext}", audio_bytes, clean_mime)}
        data = {
            "model": self.model,
            "language": "en",
            "response_format": "json",
            "temperature": "0",  # deterministic, less hallucination
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            text = (resp.json().get("text") or "").strip()

        # Filter known Whisper hallucinations on near-silent input
        if text.lower() in _WHISPER_HALLUCINATIONS:
            return ""
        return text


# Common Whisper "confident hallucinations" on silent/short clips. If the
# model returns exactly one of these, treat as no-speech-detected.
_WHISPER_HALLUCINATIONS = frozenset({
    "you", "you.", "bye", "bye.", "thanks.", "thank you.", "thank you",
    "thanks for watching!", "thanks for watching.",
    "please subscribe.", "subscribe.",
    "oh my god", "oh my god.", "oh, my god", "oh my god!",
    ".", "!", "?", "-",
})
