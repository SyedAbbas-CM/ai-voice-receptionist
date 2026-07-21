from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings

from ..base import TTSProvider


class CartesiaTTS(TTSProvider):
    name = "cartesia"

    def __init__(self) -> None:
        self.api_key = settings.cartesia_api_key
        self.default_voice = settings.cartesia_voice_id or "a0e99841-438c-4a64-b679-ae501e7d6091"
        self.model = settings.cartesia_model or "sonic-2"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("CARTESIA_API_KEY not set")
        payload = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice or self.default_voice},
            "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
            "language": "en",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "X-API-Key": self.api_key,
                    "Cartesia-Version": "2024-11-13",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.content, "audio/mpeg"
