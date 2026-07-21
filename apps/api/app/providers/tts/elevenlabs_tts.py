from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings

from ..base import TTSProvider


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(self) -> None:
        self.api_key = settings.elevenlabs_api_key
        self.default_voice = settings.elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        self.model = settings.elevenlabs_model or "eleven_turbo_v2_5"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        voice_id = voice or self.default_voice
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.content, "audio/mpeg"
