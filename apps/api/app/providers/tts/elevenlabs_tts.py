from __future__ import annotations

from typing import Optional

import httpx

from app.core.config import settings

from ..base import TTSProvider


# RE-AUDIT FIX 2026-08-02 (CRITICAL-06): map our internal output_format
# codes to ElevenLabs' query-string values.  The Twilio adapter constructs
# ElevenLabsTTS(output_format="ulaw_8000") so calls to the phone path get
# native telephony audio instead of MP3 (which the Twilio converter refuses).
_ELEVEN_FORMATS = {
    "ulaw_8000": ("ulaw_8000", "audio/x-mulaw;rate=8000"),
    "pcm_16000": ("pcm_16000", "audio/pcm;rate=16000"),
    "pcm_24000": ("pcm_24000", "audio/pcm;rate=24000"),
    "mp3_44100_128": ("mp3_44100_128", "audio/mpeg"),
    "mp3": ("mp3_44100_128", "audio/mpeg"),
}


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(self, output_format: str = "mp3_44100_128") -> None:
        self.api_key = settings.elevenlabs_api_key
        self.default_voice = settings.elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        self.model = settings.elevenlabs_model or "eleven_turbo_v2_5"
        if output_format not in _ELEVEN_FORMATS:
            raise ValueError(
                f"unsupported ElevenLabs output_format {output_format!r}; "
                f"choose from {list(_ELEVEN_FORMATS)}"
            )
        self.output_format = output_format
        self._eleven_fmt, self.mime = _ELEVEN_FORMATS[output_format]

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        voice_id = voice or self.default_voice
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            f"?output_format={self._eleven_fmt}"
        )
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
            return resp.content, self.mime

    async def stream_synthesize(self, text: str, voice: Optional[str] = None):
        """2026-08-09 speed sprint: yield audio chunks as ElevenLabs streams
        them, instead of waiting for the whole synthesis.  First chunk
        arrives in ~150-200ms vs ~1000-2000ms for the batch endpoint.

        Uses the /stream suffix on the same endpoint.  Same auth, same
        payload, same output_format — only difference is the response
        arrives as an HTTP chunked stream we iterate.

        Yields (chunk_bytes, mime) tuples.  Caller is responsible for
        pushing each chunk into the outbound audio path immediately."""
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        voice_id = voice or self.default_voice
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            f"?output_format={self._eleven_fmt}"
        )
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST", url,
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size=1600):
                    if chunk:
                        yield chunk, self.mime

    async def synthesize_from_plan(
        self, plan, voice: Optional[str] = None,
    ) -> tuple[bytes, str]:
        """Sprint 9e: send a VPL-compiled request payload.

        `plan` is a packages.voice.vpl.CompiledSpeechPlan whose
        request_payload is already ElevenLabs-shaped by
        packages.voice.vpl.compilers.compile_elevenlabs.  We only need
        to (1) resolve the voice binding, (2) map the plan's output
        format to the URL query param, (3) post it.

        Kept as a separate method — the old synthesize(text) path stays
        untouched for callers that haven't adopted VPL yet (browser
        widget, greeting cache, non-actor path)."""
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        voice_id = voice or self.default_voice
        fmt_code = plan.output_format
        if fmt_code not in _ELEVEN_FORMATS:
            raise ValueError(
                f"CompiledSpeechPlan output_format={fmt_code!r} not supported; "
                f"choose from {list(_ELEVEN_FORMATS)}"
            )
        eleven_fmt, mime = _ELEVEN_FORMATS[fmt_code]
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            f"?output_format={eleven_fmt}"
        )
        payload = dict(plan.request_payload)
        # Fill model if the compiler produced a mismatched one (compilers
        # default to eleven_turbo_v2_5; provider default may differ per tenant).
        payload.setdefault("model_id", self.model)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.content, mime
