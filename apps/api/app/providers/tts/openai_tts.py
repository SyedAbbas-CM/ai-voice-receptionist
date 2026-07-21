from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings

from ..base import TTSProvider


class OpenAITTS(TTSProvider):
    name = "openai"

    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.default_voice = settings.openai_tts_voice or "alloy"
        self.model = settings.openai_tts_model or "gpt-4o-mini-tts"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice or self.default_voice,
            "response_format": "mp3",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.content, "audio/mpeg"
