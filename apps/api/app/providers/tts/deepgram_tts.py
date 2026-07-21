from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings

from ..base import TTSProvider


class DeepgramTTS(TTSProvider):
    name = "deepgram"

    def __init__(self) -> None:
        self.api_key = settings.deepgram_api_key
        self.default_voice = settings.deepgram_tts_voice or "aura-asteria-en"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")
        params = {"model": voice or self.default_voice}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepgram.com/v1/speak",
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
                params=params,
                json={"text": text},
            )
            resp.raise_for_status()
            return resp.content, "audio/mpeg"
