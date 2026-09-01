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
from app.routes import admin, admin_login, admin_tenants, annotate, channels, chat, dashboard, debug, elevenlabs_compat, incident, outbound, plivo, recordings, sessions, signalwire, telnyx, trace, twilio, vapi, voice
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

    # 2026-08-13: per-call log extractor.  Every log line that mentions
    # a Twilio call SID (CA<32 hex>) gets duplicated into
    # data/logs/calls/<CA...>.log — one file per call, never pruned.
    # Uvicorn logs still rotate per-restart but call-scoped logs are
    # forever, so we can compare timings on a call weeks later.
    try:
        from packages.observability.per_call_logger import install_per_call_logger
        install_per_call_logger()
    except Exception as e:
        print(f"[startup] per-call logger install failed: {e}")

    init_db()

    # 2026-08-25 P0.3/P0.4 startup guard: check SHORT_TICKET_SECRET so a
    # misconfigured box surfaces at boot instead of on the first Twilio
    # call.  Ticket mint/verify hard-fails if the secret is missing or
    # < 32 chars — without this warning, the first symptom would be
    # every WSS upgrade returning 401 with an opaque log line.  We do
    # NOT crash on missing secret because the WSS ticket flow may not
    # be wired yet in some branches; the check emits a WARNING that's
    # loud enough to catch in journalctl but leaves boot successful.
    import os as _os_st
    _st_secret = _os_st.environ.get("SHORT_TICKET_SECRET", "").strip()
    _st_log = _logging.getLogger(__name__)
    if not _st_secret:
        _st_log.warning(
            "SHORT_TICKET_SECRET is UNSET — any code that calls "
            "packages.auth.mint_ticket() or verify_ticket() will fail. "
            "Generate one with `openssl rand -hex 32` and add to .env "
            "before enabling the Twilio WSS ticket flow (P0.4) or the "
            "dashboard signed-session flow (P0.3)."
        )
    elif len(_st_secret) < 32:
        _st_log.warning(
            "SHORT_TICKET_SECRET is only %d chars — HMAC-SHA256 wants at "
            "least 32 for safety. Regenerate with `openssl rand -hex 32`.",
            len(_st_secret),
        )
    else:
        _st_log.info(
            "SHORT_TICKET_SECRET configured (%d chars) — ticket mint/verify ready.",
            len(_st_secret),
        )

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
    # 2026-08-25 P0.2: /debug/* exposes traces + per-call timelines + a
    # live WebSocket call-event stream — cross-tenant call content.
    # Now requires auth (removed from _PUBLIC_PATH_PREFIXES in auth.py)
    # AND in production is only mounted when explicitly opted in via
    # OBSERVABILITY_API_ENABLED=true.  Dev/staging always mount so the
    # dashboards keep working — an authed API key is still required.
    import os as _os
    _env = _os.environ.get("ENVIRONMENT", "development").lower()
    if _env != "production" or settings.observability_api_enabled:
        app.include_router(debug.router)
    else:
        _logging.getLogger(__name__).info(
            "AUTH: /debug/* NOT mounted (ENVIRONMENT=production and "
            "OBSERVABILITY_API_ENABLED=false).  Set OBSERVABILITY_API_ENABLED=true "
            "for the incident window if you need live debug routes."
        )
    app.include_router(admin.router)
    # 2026-08-29 task #77: /admin/calls/{id}/incident — aggregated per-call
    # trace (session + transcript + bookings + call_events) in ONE query.
    # Gated by the same ADMIN_TOKEN as admin.router. Unblocks voice-agent's
    # BUG-02 investigation without needing to grep raw log files.
    app.include_router(incident.router)
    # 2026-08-30 task #99: /admin/login + /admin/logout — password auth
    # + HMAC-signed session cookie so browsers can hit /admin/* without
    # a bearer header extension. Mounts BEFORE annotate/incident so the
    # login form is reachable when other admin routes 401.
    app.include_router(admin_login.router)
    # 2026-08-30 task #94: /admin/annotate — human QA feedback dashboard.
    # Per-call annotation UI (verdict + per-turn tags + notes + gold flag).
    # Foundation for LK-judge auto-labels (task #96) + regression sweep
    # against golden corpus (task #97).
    app.include_router(annotate.router)
    # 2026-08-31 task #104-followup: /admin/recordings/{call_id}.mp3
    # serves the stereo MP3 written by AudioRecorder. Admin-gated.
    app.include_router(recordings.router)
    # 2026-09-01 GHL-wave-2 (part D): /admin/tenants — per-tenant
    # integration config UI. Lists every sample-data/*/business.json,
    # edit form for BusinessProfile.integrations, live-test buttons
    # for each backend's creds. Admin-gated.
    app.include_router(admin_tenants.router)
    app.include_router(dashboard.router)
    # 2026-08-29 (humanness debugging + traceability):
    # /trace/{call_id} — tenant-scoped humanness trace view.  Reads
    # structured humanness_events + transcript + bookings and renders
    # a business-owner-friendly timeline.  Companion to incident.py
    # which is admin-gated raw JSON.
    app.include_router(trace.router)

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
    async def _compliance_audit_boot() -> None:
        """Run compliance audit on the loaded business profile at boot.

        Surfaces two-party-state recording-consent gaps + missing AI
        disclosure as WARNING log lines so ops sees them before the
        first call.  Non-fatal — bad profile just yields notes, not a
        crash.  See packages/compliance/jurisdiction.py for the state
        list + statute cites.
        """
        try:
            from app.core.session_manager import load_business
            from packages.compliance import log_compliance_audit
            business = load_business()
            log_compliance_audit(business, source="boot")
        except Exception as e:
            print(f"[startup] compliance audit skipped: {e}")

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
                # 2026-08-10: MUST stay in sync with ReceptionistBrain.greet().
                # Kept short — 3-sentence greetings burn 7-15 sec of µ-law audio.
                # 2026-08-25: reworded disclosure to "automated receptionist"
                # (see brain.py greet() — keep IN SYNC with that string).
                include_disclosure = getattr(business, "ai_disclosure_enabled", False)
                include_recording = getattr(business, "recording_notice_enabled", False)
                # 2026-08-31 CALL-BUG-08: include agent name in greeting
                # (keep IN SYNC with brain.py greet()).
                _agent_name = getattr(business, "agent_name", None) or "Ava"
                parts = [
                    f"Thanks for calling {business.name}, "
                    f"this is {_agent_name} — how can I help?"
                ]
                if include_disclosure:
                    parts.append("You're speaking with our automated receptionist.")
                if include_recording:
                    parts.append("This call may be recorded.")
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
    async def _warm_conversation_control_fastpath() -> None:
        """2026-08-13 (A1 patch): warm the TTS cache for deterministic
        conversation-control replies ("Yep, I can hear you...", "Hi
        there...").  The actor's fastpath speaks these canonical strings
        via _speak, which hits the TTS disk-cache shortcut — but only if
        the bytes are already there.  Warm both the browser-format TTS
        and the µ-law phone singleton the same way the greeting warmup
        does."""
        if settings.tts_provider == "qwen3":
            print("[startup] conv-control fastpath skipped (Qwen3-TTS too slow)")
            return
        try:
            from app.providers import get_tts
            from packages.voice import (
                all_conversation_control_replies,
                warm_greeting_cache,
            )
            replies = all_conversation_control_replies()
            n_browser = 0
            for text in replies:
                if await warm_greeting_cache(text, get_tts()):
                    n_browser += 1
            n_phone = 0
            try:
                from app.routes.twilio import _get_telephony_tts
                from packages.tts_cache import TTSCacheWrapper
                from packages.tts_cache.cache import get_shared_cache
                telephony = _get_telephony_tts()
                if not isinstance(telephony, TTSCacheWrapper):
                    telephony = TTSCacheWrapper(telephony, cache=get_shared_cache())
                    import app.routes.twilio as _tw
                    _tw._telephony_tts_singleton = telephony
                for text in replies:
                    try:
                        await telephony.synthesize(text)
                        n_phone += 1
                    except Exception as _e:
                        print(f"[startup] conv-control phone warm FAILED for {text!r}: {_e}")
            except Exception as _e:
                print(f"[startup] conv-control phone warm skipped: {_e}")
            print(f"[startup] conv-control fastpath warmed: {n_browser}/{len(replies)} browser, "
                  f"{n_phone}/{len(replies)} phone")
        except Exception as e:
            print(f"[startup] conv-control fastpath skipped: {e}")

    @app.on_event("startup")
    async def _warm_response_cache() -> None:
        """2026-08-19: seed the response cache with per-business FAQ
        turns so the FIRST caller who asks 'do you take Delta Dental?'
        gets a ~250ms reply from cache instead of a ~2s LLM call.

        Previously the response cache was cold on every boot — it only
        accumulated entries after the LLM answered a NON-tool question
        AND a second caller asked the exact same thing.  For a demo
        that's zero hits.

        We also pre-generate the TTS bytes for each reply into the
        disk cache so the fastpath is disk-only end-to-end.
        """
        if settings.tts_provider == "qwen3":
            print("[startup] response cache warmup skipped (Qwen3-TTS too slow)")
            return
        try:
            from app.core import session_manager
            from packages.response_cache import get_shared_response_cache
            from packages.response_cache.common_turns import common_turns_for
            business = session_manager.load_business()
            pairs = common_turns_for(business)
            if not pairs:
                print(f"[startup] response cache warmup: 0 pairs for vertical={business.vertical!r}")
                return

            cache = get_shared_response_cache()
            n_seeded = 0
            for input_text, reply_text in pairs:
                try:
                    cache.put(business.id, "default", input_text, reply_text)
                    n_seeded += 1
                except Exception as _e:
                    print(f"[startup] response cache put failed for {input_text!r}: {_e}")

            # Pre-generate TTS bytes for each UNIQUE reply so the disk
            # cache is hot when the fastpath speaks.
            n_tts = 0
            unique_replies = sorted({r for _, r in pairs})
            try:
                from app.routes.twilio import _get_telephony_tts
                from packages.tts_cache import TTSCacheWrapper
                from packages.tts_cache.cache import get_shared_cache
                telephony = _get_telephony_tts()
                if not isinstance(telephony, TTSCacheWrapper):
                    telephony = TTSCacheWrapper(telephony, cache=get_shared_cache())
                    import app.routes.twilio as _tw
                    _tw._telephony_tts_singleton = telephony
                for reply in unique_replies:
                    try:
                        await telephony.synthesize(reply)
                        n_tts += 1
                    except Exception as _e:
                        print(f"[startup] response cache tts warm failed for {reply[:40]!r}: {_e}")
            except Exception as _e:
                print(f"[startup] response cache tts warm skipped: {_e}")

            print(f"[startup] response cache warmed: {n_seeded} pairs, "
                  f"{n_tts}/{len(unique_replies)} unique replies pre-TTS'd")
        except Exception as e:
            print(f"[startup] response cache warmup skipped: {e}")

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
            from packages.tts_cache.warmup import DEFAULT_BACKCHANNELS
            from packages.voice.filler import DEFAULT_FILLERS
            # 2026-08-10 FIX: filler pool + speech-act cache both need
            # entries in the ULAW format the phone path reads.  Old code
            # only warmed the browser (mp3) TTS wrapper — filler cache
            # MISSed on every real phone call, degrading to silent, so
            # callers heard no reassurance during 1-4s brain latency.
            # Now warms both wrappers with backchannels + fillers.
            phrases = list(dict.fromkeys(list(DEFAULT_BACKCHANNELS) + list(DEFAULT_FILLERS)))
            wrappers = []
            tts = get_tts()
            if isinstance(tts, TTSCacheWrapper):
                wrappers.append(("browser", tts))
            try:
                from app.routes.twilio import _get_telephony_tts
                phone_tts = _get_telephony_tts()
                if isinstance(phone_tts, TTSCacheWrapper):
                    wrappers.append(("phone", phone_tts))
            except Exception as e:
                print(f"[startup] tts_cache: phone wrapper unavailable ({e})")
            if not wrappers:
                print("[startup] tts_cache warmup skipped (no wrapped providers)")
                return
            for label, wrapper in wrappers:
                results = await warm_common_utterances(wrapper, phrases=phrases)
                warmed = sum(1 for v in results.values() if v == "warmed")
                cached = sum(1 for v in results.values() if v == "cached")
                errors = sum(1 for v in results.values() if v.startswith("error"))
                print(
                    f"[startup] tts_cache warmup ({label}): "
                    f"{cached} cached, {warmed} newly warmed, {errors} errors"
                )
        except Exception as e:
            print(f"[startup] tts_cache warmup skipped: {e}")

    @app.on_event("startup")
    async def _warm_llm_router() -> None:
        """2026-08-08: pre-warm the LLM router's HTTP clients + TLS session.
        2026-08-11: also send a WITH-TOOLS call so the router's
        capability-aware routing picks the SAME provider+model that
        real brain calls will use.
        2026-08-20 (SPEED-EXTRA-D): warm with the FULL production tool
        schema — including `emit_semantic_plan` (T-SP1) and the vertical
        tools for the tenant's business.  OpenAI compiles JSON schemas
        into constrained grammars on first use, adding 200-400ms to
        the first real caller's TTFT.  Warming here shifts that cost
        to boot.  See docs/openai-speed-research-2026-08-20.md lever 8."""
        try:
            from app.core import session_manager
            from app.providers import get_llm
            from packages.core_agent.plan_realizer import (
                semantic_plan_tool_definition,
            )
            from packages.integrations import build_tools_for_vertical

            router = get_llm()

            # Build the REAL production tool list so schema-compile
            # happens against real schemas, not a fake `check_hours` stub.
            try:
                business = session_manager.load_business()
                calendar = session_manager.get_calendar()
                real_tools, _handler = build_tools_for_vertical(
                    business, calendar, retriever=None, shaper_llm=None,
                )
                # ReceptionistBrain adds emit_semantic_plan at __init__;
                # mirror that here.
                real_tools = list(real_tools) + [semantic_plan_tool_definition()]
            except Exception as _e:
                print(f"[startup] llm warmup: couldn't build real tools ({_e}) "
                      f"— falling back to dummy tool")
                from packages.schemas import ToolDefinition
                real_tools = [ToolDefinition(
                    name="check_hours",
                    description="Check business hours.",
                    parameters={"type": "object", "properties": {}, "required": []},
                )]

            resp = await router.complete(
                messages=[
                    {"role": "system", "content": "You are a helper. Reply 'ok'."},
                    {"role": "user", "content": "hi"},
                ],
                tools=real_tools,
                temperature=0.0,
                max_tokens=60,  # reasoning models (gpt-oss-120b) burn tokens on internal thinking; must be > 20 or content=null
                site="brain.warmup",
            )
            print(f"[startup] llm router warmed (with {len(real_tools)} real tools): "
                  f"reply={resp.text[:20]!r}")
        except Exception as e:
            print(f"[startup] llm router warmup skipped: {e}")

    @app.on_event("startup")
    async def _warm_llm_prompt_cache() -> None:
        """2026-08-21: warm OpenAI's `prompt_cache_key` slot with the
        REAL production system prompt (~14-17k chars) so the first real
        caller's turn 0 hits a warm cache instead of paying the full
        prefill cost.

        The existing `brain.warmup` (above) uses a dummy 30-char system
        prompt so it warms tool-schema JIT but NOT the real prompt-cache
        slot. This second call fires the same shape as real brain
        traffic (real system prompt + real biz-<hash> cache key), which
        populates the actual cache slot the runtime hits on turn 0.

        Expected saving: 500-700ms off first-caller turn-0 first-token
        latency (previously observed ~1500ms cold, ~800ms warm). Cost:
        one billed API request per startup, ~4k input tokens.

        Runs AFTER the tool-schema warmup so if this one fails we still
        have the tool JIT primed."""
        try:
            from app.core import session_manager
            from app.core.config import settings
            from app.providers import get_llm
            from packages.core_agent.prompt import build_system_prompt
            from packages.core_agent.plan_realizer import (
                semantic_plan_tool_definition,
            )
            from packages.integrations import build_tools_for_vertical

            business = session_manager.load_business()
            calendar = session_manager.get_calendar()
            real_tools, _handler = build_tools_for_vertical(
                business, calendar, retriever=None, shaper_llm=None,
            )
            real_tools = list(real_tools) + [semantic_plan_tool_definition()]

            # Assemble the SAME system prompt build_system_prompt yields
            # at runtime. Byte-for-byte match matters because
            # openai_llm._derive_cache_key hashes the exact string.
            system_prompt = build_system_prompt(business)

            router = get_llm()
            resp = await router.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "hi"},
                ],
                tools=real_tools,
                temperature=0.0,
                max_tokens=8,
                site="brain.cache_warmup",
            )
            print(
                f"[startup] prompt-cache warmed: {len(system_prompt)} char prompt, "
                f"{len(real_tools)} tools, reply={resp.text[:20]!r}"
            )

            # 2026-08-22 NET Ship 4: fire a SECOND warmup ~2s after the
            # first.  OpenAI Fast tier appears to route requests via a
            # consistent-hashing scheme where the `prompt_cache_key`
            # picks a backend — but two consecutive requests with the
            # same key can land on DIFFERENT backends until the routing
            # tier converges.  A single warmup only populates ONE
            # backend's cache slot.  On CAa7effd6273 turn 0 saw a
            # 3.2-second first-token even with prompt-cache prewarm
            # shipped — the caller's request hit a "second backend"
            # that had never seen the key.  Two consecutive warms
            # increase the odds both backends are primed.  Cost: one
            # additional billed API request per startup, ~4k input
            # tokens.  Cheap for the resilience.
            import asyncio as _aio
            await _aio.sleep(2.0)
            resp2 = await router.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "hi"},
                ],
                tools=real_tools,
                temperature=0.0,
                max_tokens=8,
                site="brain.cache_warmup_2",
            )
            print(
                f"[startup] prompt-cache warmed (2nd fire): "
                f"reply={resp2.text[:20]!r} — populates alt backend cache slot"
            )
        except Exception as e:
            print(f"[startup] prompt-cache warmup skipped: {e}")

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
