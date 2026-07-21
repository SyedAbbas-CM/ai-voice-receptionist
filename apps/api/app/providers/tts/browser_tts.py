from __future__ import annotations

from typing import Optional

from ..base import TTSProvider


class BrowserTTS(TTSProvider):
    """Sentinel TTS provider: returns empty audio and tells the client to
    speak via the browser's built-in SpeechSynthesis API. Lets the demo
    work with zero TTS credentials."""

    name = "browser"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        return b"", "text/x-browser-speak"
