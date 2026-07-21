"""ElevenLabs-compatible TTS endpoints.

Speaks 11L's HTTP shape, but routes to whatever TTS_PROVIDER is configured
(Kokoro, Qwen3, ElevenLabs itself, OpenAI, etc). Clients can point their
existing 11L SDK at our server without touching their code — just change
the base URL.

Endpoints:
    GET  /v1/voices                             — list available voices
    POST /v1/text-to-speech/{voice_id}          — synthesize (non-streaming)
    POST /v1/text-to-speech/{voice_id}/stream   — synthesize (streaming, MP3/WAV chunks)

The `voice_id` in the URL is passed as the `voice` arg to the backing provider,
so 11L voice IDs work when the provider IS ElevenLabs and provider-native
voice names work when the provider is Qwen3/Kokoro/etc.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.providers import get_tts


router = APIRouter(prefix="/v1", tags=["elevenlabs-compat"])


class VoiceSettings(BaseModel):
    stability: float | None = 0.5
    similarity_boost: float | None = 0.75
    style: float | None = 0.0
    use_speaker_boost: bool | None = True


class TTSRequest(BaseModel):
    text: str
    model_id: str | None = None
    voice_settings: VoiceSettings | None = None
    output_format: str | None = None


def _check_key(xi_api_key: str | None) -> None:
    """Optional gate: if COMPAT_API_KEY is set in env, require it. Otherwise open."""
    required = settings.compat_api_key
    if required and xi_api_key != required:
        raise HTTPException(status_code=401, detail="invalid xi-api-key")


@router.get("/voices")
async def list_voices(xi_api_key: str | None = Header(default=None, alias="xi-api-key")) -> dict:
    _check_key(xi_api_key)
    tts = get_tts()
    voices = _voices_for_provider(tts.name)
    return {"voices": voices}


@router.post("/text-to-speech/{voice_id}")
async def text_to_speech(
    voice_id: str,
    body: TTSRequest,
    xi_api_key: str | None = Header(default=None, alias="xi-api-key"),
) -> Response:
    _check_key(xi_api_key)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    tts = get_tts()
    audio_bytes, mime = await tts.synthesize(text, voice=voice_id)
    if mime == "text/x-browser-speak":
        raise HTTPException(status_code=400, detail="configured TTS_PROVIDER is `browser`; compat endpoint needs a real audio backend")
    return Response(content=audio_bytes, media_type=mime, headers={"X-TTS-Provider": tts.name})


@router.post("/text-to-speech/{voice_id}/stream")
async def text_to_speech_stream(
    voice_id: str,
    body: TTSRequest,
    xi_api_key: str | None = Header(default=None, alias="xi-api-key"),
) -> StreamingResponse:
    _check_key(xi_api_key)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    tts = get_tts()
    audio_bytes, mime = await tts.synthesize(text, voice=voice_id)
    if mime == "text/x-browser-speak":
        raise HTTPException(status_code=400, detail="configured TTS_PROVIDER is `browser`; compat endpoint needs a real audio backend")

    async def _chunks():
        chunk_size = 16 * 1024
        for i in range(0, len(audio_bytes), chunk_size):
            yield audio_bytes[i:i + chunk_size]

    return StreamingResponse(_chunks(), media_type=mime, headers={"X-TTS-Provider": tts.name})


def _voices_for_provider(provider: str) -> list[dict]:
    """Return a list of voice entries in 11L's shape. For non-11L providers,
    surface a curated set that maps to the provider's native voice ids."""
    if provider == "elevenlabs":
        return [{
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "name": "Rachel (default)",
            "category": "premade",
            "labels": {"provider": "elevenlabs"},
        }]
    if provider == "qwen3":
        speakers = ["Vivian", "Adam", "Rina", "Ryan", "Cherry", "Ethan"]
        return [{
            "voice_id": s,
            "name": s,
            "category": "premade",
            "labels": {"provider": "qwen3", "language": settings.qwen3_tts_default_language or "English"},
        } for s in speakers]
    if provider == "openai":
        return [{
            "voice_id": v,
            "name": v.capitalize(),
            "category": "premade",
            "labels": {"provider": "openai"},
        } for v in ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]]
    if provider == "deepgram":
        return [{
            "voice_id": "aura-asteria-en",
            "name": "Asteria",
            "category": "premade",
            "labels": {"provider": "deepgram"},
        }]
    return [{
        "voice_id": "default",
        "name": f"{provider} default",
        "category": "premade",
        "labels": {"provider": provider},
    }]
