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
from app.routes import admin, channels, chat, debug, elevenlabs_compat, outbound, sessions, twilio, vapi, voice


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

    # AUDIT FIX 2026-08-01 (SEC-012): baseline security headers
    from starlette.middleware.base import BaseHTTPMiddleware as _BHM
    class SecurityHeadersMiddleware(_BHM):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            return response
    app.add_middleware(SecurityHeadersMiddleware)

    # AUDIT FIX 2026-08-01 (SEC-001..SEC-004): API-key auth + tenant scoping
    from app.middleware.auth import AuthTenantMiddleware
    app.add_middleware(AuthTenantMiddleware)

    # Sprint 6c: cross-tenant leak guard.  Importing installs the SQLAlchemy
    # before_execute listener that rejects any query on tenant-scoped tables
    # missing a tenant_id filter.  Defense-in-depth on top of the handler-
    # side tenant scoping in routes/sessions.py.
    from app.db import tenant_guard as _tenant_guard
    _tenant_guard.install()

    # AUDIT FIX 2026-08-01 (SEC-009): tighter CORS default.  Wildcard only in
    # explicit dev mode; production requires a comma-separated allowlist in
    # CORS_ORIGINS env.
    if settings.cors_origins == "*":
        origins = ["*"]
        import os
        if os.environ.get("ENVIRONMENT", "").lower() == "production":
            raise RuntimeError(
                "CORS_ORIGINS=* is forbidden in production; set an explicit allowlist"
            )
    else:
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
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
    app.include_router(admin.router)

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

    @app.on_event("startup")
    async def _warm_greeting_cache() -> None:
        """Pre-synthesize the greeting for the currently-loaded business
        profile. Kills the 2-3s TTS cold-start on turn 1 of every call.

        Non-fatal on failure — if TTS is down at startup, the first caller
        just eats the cold-synth cost as before."""
        if settings.tts_provider == "qwen3":
            print("[startup] greeting cache skipped (Qwen3-TTS too slow)")
            return
        try:
            from app.providers import get_tts
            from app.core.session_manager import load_business
            from packages.voice import warm_greeting_cache
            business = load_business()
            # Build the same greeting the brain would build. Keep in sync
            # with ReceptionistBrain.greet() — TODO: extract a shared helper.
            override = getattr(business, "greeting_override", None)
            if override:
                text = override
            else:
                include_disclosure = getattr(business, "ai_disclosure_enabled", True)
                include_recording = getattr(business, "recording_notice_enabled", True)
                parts = [f"Hi, thanks for calling {business.name}."]
                if include_disclosure:
                    parts.append("I'm an AI assistant here to help.")
                if include_recording:
                    parts.append("This call may be recorded for quality.")
                parts.append("How can I help you today?")
                text = " ".join(parts)
            ok = await warm_greeting_cache(text, get_tts())
            print(f"[startup] greeting cache: {'warmed' if ok else 'FAILED (will cold-synth first call)'}")
        except Exception as e:
            print(f"[startup] greeting cache skipped: {e}")

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

    # Mount /graph (live n8n-style agent visualization) BEFORE /simulator so
    # its assets don't collide with the root mount.
    graph_dir = _REPO_ROOT / "apps" / "graph"
    if graph_dir.exists():
        app.mount("/graph", StaticFiles(directory=str(graph_dir), html=True), name="graph")

    # Mount /call — the customer-facing widget (clean UX, no dev panels).
    # This is what a restaurant/clinic embeds on their site. Same backend as
    # /simulator; different presentation.
    widget_dir = _REPO_ROOT / "apps" / "call-widget"
    if widget_dir.exists():
        app.mount("/call", StaticFiles(directory=str(widget_dir), html=True), name="call")

    simulator_dir = _REPO_ROOT / "apps" / "call-simulator"
    if simulator_dir.exists():
        # Keep the /simulator path for backwards compat, but also mount at root
        # so relative asset paths (./style.css, ./app.js) resolve correctly when
        # the sim is served from /.
        app.mount("/simulator", StaticFiles(directory=str(simulator_dir), html=True), name="simulator")
        app.mount("/", StaticFiles(directory=str(simulator_dir), html=True), name="root")

    return app


app = create_app()
