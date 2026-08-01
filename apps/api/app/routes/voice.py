from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, JSONResponse

from app.providers import get_stt, get_tts
from packages.observability import (
    estimate_stt_cost,
    estimate_tts_cost,
    get_tracer,
)


router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(...),
    mime: str = Form("audio/webm"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    stt = get_stt()
    tracer = get_tracer()
    with tracer.span(
        "voice.stt",
        **{
            "stt.provider": stt.name,
            "stt.model": getattr(stt, "model_name", None) or getattr(stt, "model", ""),
            "audio.bytes": len(audio_bytes),
            "audio.mime": mime,
        },
    ) as span:
        transcript = await stt.transcribe(audio_bytes, mime=mime)
        span.set_attribute("transcript.length", len(transcript or ""))
        # Rough seconds estimate: assume WebM/Opus ~4KB/sec, WAV ~32KB/sec
        est_seconds = (len(audio_bytes) / 32000) if "wav" in mime else (len(audio_bytes) / 4000)
        span.set_attribute("audio.seconds_est", round(est_seconds, 2))
        span.set_attribute("cost_usd_est", round(estimate_stt_cost(stt.name, est_seconds), 6))
    return {"transcript": transcript, "provider": stt.name}


@router.post("/tts")
async def text_to_speech(payload: dict):
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice")
    if not text:
        raise HTTPException(status_code=400, detail="missing text")
    tts = get_tts()
    tracer = get_tracer()
    with tracer.span(
        "voice.tts",
        **{"tts.provider": tts.name, "text.length": len(text)},
    ) as span:
        audio_bytes, mime = await tts.synthesize(text, voice=voice)
        span.set_attribute("audio.bytes", len(audio_bytes or b""))
        span.set_attribute("audio.mime", mime)
        span.set_attribute("cost_usd_est", round(estimate_tts_cost(tts.name, len(text)), 6))
    if mime == "text/x-browser-speak":
        return JSONResponse({"provider": "browser", "speak": text})
    return Response(
        content=audio_bytes,
        media_type=mime,
        headers={"X-TTS-Provider": tts.name},
    )


@router.post("/tts-base64")
async def text_to_speech_b64(payload: dict):
    """Same as /tts but returns base64 JSON. Easier to consume from the
    browser call simulator without juggling Blob URLs."""
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice")
    if not text:
        raise HTTPException(status_code=400, detail="missing text")
    tts = get_tts()
    tracer = get_tracer()
    with tracer.span(
        "voice.tts",
        **{"tts.provider": tts.name, "text.length": len(text)},
    ) as span:
        audio_bytes, mime = await tts.synthesize(text, voice=voice)
        span.set_attribute("audio.bytes", len(audio_bytes or b""))
        span.set_attribute("audio.mime", mime)
        span.set_attribute("cost_usd_est", round(estimate_tts_cost(tts.name, len(text)), 6))
    if mime == "text/x-browser-speak":
        return {"provider": "browser", "speak": text, "audio_b64": None, "mime": mime}
    return {
        "provider": tts.name,
        "speak": None,
        "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
        "mime": mime,
    }


@router.post("/tts-stream")
async def text_to_speech_stream(payload: dict):
    """Streaming TTS. Splits text into sentence-sized chunks, synthesizes each,
    and yields NDJSON lines as each chunk becomes ready. The browser plays
    chunk N while chunk N+1 is still being synthed.

    Wire format (one JSON object per line, `\\n`-delimited):
        {"seq": 0, "text": "...", "audio_b64": "...", "mime": "audio/wav"}
        {"seq": 1, "text": "...", "audio_b64": "...", "mime": "audio/wav"}
        {"seq": -1, "done": true, "n_chunks": 2}      // sentinel, end of stream

    On error mid-stream, we emit `{"seq": -1, "error": "...", "n_chunks": N}`.
    """
    import json as _json
    from fastapi.responses import StreamingResponse

    text = (payload.get("text") or "").strip()
    voice = payload.get("voice")
    if not text:
        raise HTTPException(status_code=400, detail="missing text")

    tts = get_tts()
    tracer = get_tracer()

    # Sprint 4a: pre-cached greeting fast-path. If the exact text matches
    # a warmed greeting, skip TTS entirely and send cached bytes as one
    # chunk. Saves 2-3s cold-synth on turn 1 of every call.
    from packages.voice import get_cached_greeting
    cached = get_cached_greeting(text)

    async def _generate():
        seq = 0
        total_bytes = 0
        with tracer.span(
            "voice.tts_stream",
            **{
                "tts.provider": tts.name,
                "tts.streaming_native": bool(getattr(tts, "supports_streaming", False)),
                "text.length": len(text),
                "cache_hit": cached is not None,
            },
        ) as span:
            if cached is not None:
                cached_bytes, cached_mime = cached
                line = _json.dumps({
                    "seq": 0,
                    "audio_b64": base64.b64encode(cached_bytes).decode("ascii"),
                    "mime": cached_mime,
                })
                yield line + "\n"
                yield _json.dumps({"seq": -1, "done": True, "n_chunks": 1, "cache_hit": True}) + "\n"
                span.set_attribute("chunks.emitted", 1)
                span.set_attribute("audio.bytes_total", len(cached_bytes))
                span.set_attribute("cost_usd_est", 0.0)  # cache hit = free
                return

            try:
                async for audio_bytes, mime in tts.stream_sentences(text, voice=voice):
                    # Browser fallback (no local audio) — emit text-only chunk
                    if mime == "text/x-browser-speak":
                        line = _json.dumps({
                            "seq": seq, "provider": "browser",
                            "speak": text, "audio_b64": None, "mime": mime,
                        })
                    else:
                        line = _json.dumps({
                            "seq": seq,
                            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
                            "mime": mime,
                        })
                    seq += 1
                    total_bytes += len(audio_bytes or b"")
                    yield line + "\n"
                span.set_attribute("chunks.emitted", seq)
                span.set_attribute("audio.bytes_total", total_bytes)
                span.set_attribute("cost_usd_est", round(estimate_tts_cost(tts.name, len(text)), 6))
                # Sentinel
                yield _json.dumps({"seq": -1, "done": True, "n_chunks": seq}) + "\n"
            except Exception as e:
                span.set_attribute("error", str(e))
                yield _json.dumps({"seq": -1, "error": str(e), "n_chunks": seq}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
