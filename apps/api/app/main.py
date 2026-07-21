from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable so `packages.*` resolves without an install step
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.db.session import init_db
from app.routes import channels, chat, debug, elevenlabs_compat, outbound, sessions, twilio, vapi, voice


def create_app() -> FastAPI:
    init_db()

    # Wire observability tracer once at startup. NoopTracer is the default
    # (zero overhead) — set TRACER_KIND=print for local dev or =otel for a
    # real OTLP endpoint (Langfuse, Honeycomb, Grafana Tempo).
    try:
        from packages.observability import build_tracer, set_tracer
        kwargs = {"service_name": settings.tracer_service_name} if settings.tracer_kind == "otel" else {}
        set_tracer(build_tracer(settings.tracer_kind or "noop", **kwargs))
    except Exception as e:
        print(f"[startup] tracer init failed, using noop: {e}")

    app = FastAPI(title="voiceops-ai-agent", version="0.1.0")

    origins = [o.strip() for o in settings.cors_origins.split(",")] if settings.cors_origins != "*" else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(chat.router)
    app.include_router(voice.router)
    app.include_router(sessions.router)
    app.include_router(vapi.router)
    app.include_router(elevenlabs_compat.router)
    app.include_router(channels.router)
    app.include_router(twilio.router)
    app.include_router(outbound.router)
    app.include_router(debug.router)

    @app.on_event("startup")
    async def _warm_filler_pool() -> None:
        """Pre-synthesize the filler audio pool so tool-call latency doesn't
        create dead air on the first call. Non-fatal on failure.

        Skipped for Qwen3-TTS on CPU/MPS — RTF ~30 means warming 5 fillers
        takes 3+ minutes, blocking server startup. Fillers are non-critical
        UX polish; safer to just skip them for slow providers."""
        if settings.tts_provider == "qwen3":
            print("[startup] filler warmup skipped (Qwen3-TTS RTF too high)")
            return
        try:
            from app.providers import get_tts
            from packages.voice import warm_default_pool
            n = await warm_default_pool(get_tts())
            print(f"[startup] filler pool warmed: {n} clips")
        except Exception as e:
            print(f"[startup] filler warmup skipped: {e}")

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "llm": settings.llm_provider,
            "stt": settings.stt_provider,
            "tts": settings.tts_provider,
        }

    @app.get("/config")
    def config() -> JSONResponse:
        return JSONResponse({
            "llm": settings.llm_provider,
            "stt": settings.stt_provider,
            "tts": settings.tts_provider,
        })

    simulator_dir = _REPO_ROOT / "apps" / "call-simulator"
    if simulator_dir.exists():
        # Keep the /simulator path for backwards compat, but also mount at root
        # so relative asset paths (./style.css, ./app.js) resolve correctly when
        # the sim is served from /.
        app.mount("/simulator", StaticFiles(directory=str(simulator_dir), html=True), name="simulator")
        app.mount("/", StaticFiles(directory=str(simulator_dir), html=True), name="root")

    return app


app = create_app()
