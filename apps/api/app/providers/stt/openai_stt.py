from __future__ import annotations

import httpx

from app.core.config import settings

from ..base import STTProvider


class OpenAISTT(STTProvider):
    name = "openai"

    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.model = settings.openai_stt_model or "whisper-1"

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        ext = "wav" if "wav" in mime else "webm" if "webm" in mime else "mp3"
        files = {"file": (f"audio.{ext}", audio_bytes, mime)}
        data = {"model": self.model, "language": "en", "response_format": "json"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            return resp.json().get("text", "")
