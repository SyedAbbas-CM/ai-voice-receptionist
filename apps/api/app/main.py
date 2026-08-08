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
from app.routes import admin, channels, chat, debug, elevenlabs_compat, outbound, plivo, sessions, signalwire, telnyx, twilio, vapi, voice
from packages.observability.structured_log import maybe_install as maybe_install_json_logs


def create_app() -> FastAPI:
    # Force INFO log level for app loggers so debug traces show in
    # /tmp/uvicorn.log — uvicorn's --log-level flag only sets its own
    # loggers, not ours.
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # Sprint 10 obs: install JSON log formatter if STRUCTURED_LOGS=true.
    # Called BEFORE init_db so init logs go through the new formatter.
    maybe_install_json_logs()
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
    app.include_router(signalwire.router)
    app.include_router(telnyx.router)
    app.include_router(plivo.router)
    app.include_router(outbound.router)
    app.include_router(debug.router)
    app.include_router(admin.router)

    # Sprint 9b: /metrics scrape endpoint for turn-latency histograms,
    # barge-in counters, provider fallback counts, ledger heard/generated
    # ratio.  Gated by METRICS_ENABLED (default true).  OTel tracing
    # activates separately when OTEL_EXPORTER_OTLP_ENDPOINT is set.
    from packages.runtime.telemetry import mount_metrics
    mount_metrics(app)

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
            # 2026-08-08 FIX v2: greeting_cache uses ONE shared in-memory
            # dict keyed by sha256(text) — so calling warm_greeting_cache
            # TWICE with different providers is a no-op the second time.
            # And the Twilio path uses a SEPARATE µ-law singleton that had
            # no disk cache wrapper — so every real phone call cold-synthed
            # the greeting (~800-1000ms of dead air).
            # Proper fix: wrap the telephony singleton in the shared disk
            # cache, then call its synthesize() directly to populate that
            # disk cache with the ulaw bytes.  Skips the greeting_cache
            # module entirely for the phone path since it's not the layer
            # the actor's _stream_tts reads from.
            ok_browser = await warm_greeting_cache(text, get_tts())
            ok_phone = False
            try:
                from app.routes.twilio import _get_telephony_tts
                from packages.tts_cache import TTSCacheWrapper
                from packages.tts_cache.cache import get_shared_cache
                telephony = _get_telephony_tts()
                if not isinstance(telephony, TTSCacheWrapper):
                    telephony = TTSCacheWrapper(telephony, cache=get_shared_cache())
                    import app.routes.twilio as _tw
                    _tw._telephony_tts_singleton = telephony
                # Direct synthesize call — bypasses greeting_cache dedup.
                # This populates the disk-backed TTSCacheWrapper cache in
                # ulaw_8000 format so the actor's cache lookup HITS.
                import time as _t
                _tstart = _t.perf_counter()
                _audio, _mime = await telephony.synthesize(text)
                _took_ms = (_t.perf_counter() - _tstart) * 1000
                ok_phone = True
                print(f"[startup] greeting cache: browser={ok_browser} phone=True (ulaw {len(_audio)}B in {_took_ms:.0f}ms)")
            except Exception as _e:
                print(f"[startup] greeting cache: browser={ok_browser} phone-warm FAILED: {_e}")
        except Exception as e:
            print(f"[startup] greeting cache skipped: {e}")

    @app.on_event("startup")
    async def _warm_smart_turn() -> None:
        """S13-A: pre-warm the smart-turn ONNX model + prime the
        inference cache.  Cold first-call is ~450ms which was
        blocking the async event loop on turn 1 of each call,
        starving the Deepgram audio consumer (observed 2026-08-07:
        1000+ frames dropped, greeting delayed 31s, Deepgram closed
        with 1011 no-audio timeout).  Warming here shifts that cost
        to boot."""
        if not getattr(settings, "smart_turn_enabled", False):
            return
        try:
            import numpy as np
            from packages.runtime.smart_turn import SmartTurnDetector
            det = SmartTurnDetector.get()
            # 1 sec of silence — fastest possible warm inference
            silence = np.zeros(16000, dtype=np.int16).tobytes()
            _ = det.predict(silence)
            print("[startup] smart-turn-v3: warmed")
        except Exception as e:
            print(f"[startup] smart-turn warmup skipped: {e}")

    @app.on_event("startup")
    async def _warm_tts_cache() -> None:
        """Task A: pre-cache common backchannels/fillers so runtime
        cache-hit rate starts at ~15% instead of 0%.  Non-fatal."""
        if not getattr(settings, "tts_cache_enabled", False):
            return
        if not getattr(settings, "tts_cache_warm_on_boot", True):
            return
        try:
            from app.providers import get_tts
            from packages.tts_cache import warm_common_utterances, TTSCacheWrapper
            tts = get_tts()
            if not isinstance(tts, TTSCacheWrapper):
                print("[startup] tts_cache warmup skipped (provider not wrapped)")
                return
            results = await warm_common_utterances(tts)
            warmed = sum(1 for v in results.values() if v == "warmed")
            cached = sum(1 for v in results.values() if v == "cached")
            errors = sum(1 for v in results.values() if v.startswith("error"))
            print(
                f"[startup] tts_cache warmup: {cached} already cached, "
                f"{warmed} newly warmed, {errors} errors"
            )
        except Exception as e:
            print(f"[startup] tts_cache warmup skipped: {e}")

    @app.on_event("startup")
    async def _warm_llm_router() -> None:
        """2026-08-08: pre-warm the LLM router's HTTP clients + do a
        3-token echo to hot the TLS session on the primary provider.
        Kills first-call TCP + TLS + Mistral first-byte lag (~1-2s)
        on turn 1 of every call.  Per docs/rnd-2026-08/51-latency-deep-dive.md."""
        try:
            from app.providers.llm.router_llm import RouterLLM
            router = RouterLLM()
            # Hot the primary provider only — one cheap call.
            resp = await router.complete(
                messages=[
                    {"role": "system", "content": "Reply with only the word ok."},
                    {"role": "user", "content": "hi"},
                ],
                temperature=0.0,
                max_tokens=5,
            )
            print(f"[startup] llm router warmed: primary served, reply={resp.text[:20]!r}")
        except Exception as e:
            print(f"[startup] llm router warmup skipped: {e}")

    @app.on_event("startup")
    async def _warm_deepgram_dns() -> None:
        """2026-08-08: do a lightweight HEAD to Deepgram's API to warm
        DNS + TCP + TLS to the region.  When the first real call comes
        in, websockets.connect() then skips DNS lookup (~100-500ms
        savings on cold start).  Per latency deep-dive research."""
        try:
            import httpx
            key = getattr(settings, "deepgram_api_key", None)
            if not key:
                return
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(
                    "https://api.deepgram.com/v1/projects",
                    headers={"Authorization": f"Token {key}"},
                )
            print(f"[startup] deepgram DNS+TLS warmed: HTTP {r.status_code}")
        except Exception as e:
            print(f"[startup] deepgram warmup skipped: {e}")

    @app.get("/debug/tts-cache")
    def tts_cache_stats() -> dict:
        """Task A: observability for the TTS synth cache."""
        try:
            from packages.tts_cache.cache import get_shared_cache
            c = get_shared_cache()
            entries = list(c._index.values())
            return {
                "enabled": bool(getattr(settings, "tts_cache_enabled", False)),
                "entries": len(entries),
                "total_bytes": c._total_bytes,
                "max_bytes": c._max_bytes,
                "utilization_pct": round(100 * c._total_bytes / max(c._max_bytes, 1), 1),
                "cache_dir": c._base,
                "sample_keys": [e.key for e in entries[:20]],
            }
        except Exception as e:
            return {"error": str(e)}

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

    # Mount /call-stream — dev widget that mimics Twilio's Media Streams
    # protocol against /twilio/stream, plus a live debug-event side-panel.
    # Refuses to mount in production unless explicitly allowed, since it
    # exposes an unauth'd view of every call event.
    stream_widget_dir = _REPO_ROOT / "apps" / "call-stream"
    if stream_widget_dir.exists():
        import os as _os_mount
        _env = _os_mount.environ.get("ENVIRONMENT", "development").lower()
        _allow = _os_mount.environ.get("ALLOW_DEBUG_WIDGETS", "false").lower() in ("1", "true", "yes")
        if _env != "production" or _allow:
            app.mount(
                "/call-stream",
                StaticFiles(directory=str(stream_widget_dir), html=True),
                name="call_stream",
            )

    simulator_dir = _REPO_ROOT / "apps" / "call-simulator"
    if simulator_dir.exists():
        # Keep the /simulator path for backwards compat, but also mount at root
        # so relative asset paths (./style.css, ./app.js) resolve correctly when
        # the sim is served from /.
        app.mount("/simulator", StaticFiles(directory=str(simulator_dir), html=True), name="simulator")
        app.mount("/", StaticFiles(directory=str(simulator_dir), html=True), name="root")

    return app


app = create_app()
