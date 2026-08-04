"""Debug + observability endpoints.

- GET /debug/traces          → recent spans (requires TRACER_KIND=memory)
- GET /debug/traces/summary  → per-span-name stats: count, P50, P95, mean cost
- GET /debug/config          → what providers + models are currently wired
- POST /debug/traces/clear   → wipe the in-memory buffer

These only work when TRACER_KIND=memory. In prod you'd hit your OTel/Langfuse
dashboard instead — same span data, different consumer.
"""
from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.core.config import settings
from packages.observability import (
    InMemoryTracer,
    get_tracer,
    estimate_llm_cost,
    estimate_stt_cost,
    estimate_tts_cost,
)


router = APIRouter(prefix="/debug", tags=["debug"])


def _require_memory_tracer() -> InMemoryTracer:
    t = get_tracer()
    if not isinstance(t, InMemoryTracer):
        raise HTTPException(
            400,
            f"tracer kind is '{t.name}'; /debug/traces requires TRACER_KIND=memory",
        )
    return t


def _span_cost_usd(span) -> float:
    """Estimate USD cost for a single span from its attributes."""
    attrs = span.attributes or {}
    system = attrs.get("gen_ai.system") or ""
    model = attrs.get("gen_ai.request.model") or ""
    if not system:
        return 0.0
    input_tokens = int(attrs.get("gen_ai.usage.input_tokens") or 0)
    output_tokens = int(attrs.get("gen_ai.usage.output_tokens") or 0)
    cache_read = int(attrs.get("gen_ai.usage.cache_read_input_tokens") or 0)
    return estimate_llm_cost(
        provider=system, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read,
    )


@router.get("/traces")
def get_traces(
    limit: int = 50,
    session_id: Optional[str] = None,
    name_prefix: Optional[str] = None,
) -> dict:
    """Return the most recent spans. Filters: session_id, name prefix, limit."""
    tracer = _require_memory_tracer()
    spans = list(tracer.spans)
    if session_id:
        spans = [s for s in spans if s.attributes.get("session_id") == session_id]
    if name_prefix:
        spans = [s for s in spans if s.name.startswith(name_prefix)]
    # Newest first
    spans = list(reversed(spans))[:limit]

    result = []
    total_cost = 0.0
    for s in spans:
        cost = _span_cost_usd(s)
        total_cost += cost
        result.append({
            "span_id": s.span_id,
            "name": s.name,
            "duration_ms": round(s.duration_ms or 0, 1),
            "status": s.status,
            "error": s.error_message,
            "cost_usd": round(cost, 5) if cost else 0,
            "attributes": s.attributes,
            "start_ms": s.start_ms,
        })

    return {
        "count": len(result),
        "total_cost_usd_shown": round(total_cost, 4),
        "buffer_size": len(tracer.spans),
        "spans": result,
    }


@router.get("/traces/summary")
def traces_summary() -> dict:
    """Per-span-name aggregates: count, mean/p50/p95 latency, total cost."""
    tracer = _require_memory_tracer()
    by_name: dict[str, list[float]] = defaultdict(list)
    cost_by_name: dict[str, float] = defaultdict(float)
    errors_by_name: dict[str, int] = defaultdict(int)

    for s in tracer.spans:
        if s.duration_ms is None:
            continue
        by_name[s.name].append(s.duration_ms)
        cost_by_name[s.name] += _span_cost_usd(s)
        if s.status == "error":
            errors_by_name[s.name] += 1

    summary = []
    for name, durations in sorted(by_name.items()):
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        p50 = statistics.median(durations_sorted)
        p95 = durations_sorted[int(n * 0.95)] if n >= 20 else durations_sorted[-1]
        summary.append({
            "span": name,
            "count": n,
            "errors": errors_by_name[name],
            "mean_ms": round(statistics.mean(durations_sorted), 1),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "min_ms": round(min(durations_sorted), 1),
            "max_ms": round(max(durations_sorted), 1),
            "total_cost_usd": round(cost_by_name[name], 4),
        })
    return {"summary": summary, "total_spans": len(tracer.spans)}


@router.post("/traces/clear")
def clear_traces() -> dict:
    tracer = _require_memory_tracer()
    n = len(tracer.spans)
    tracer.spans = []
    return {"cleared": n}


@router.get("/config")
def get_debug_config() -> dict:
    """What providers + models are wired in the running server. Useful for
    verifying a demo is using the right stack before recording."""
    return {
        "llm": {
            "provider": settings.llm_provider,
            "model": {
                "openai": settings.openai_model,
                "anthropic": settings.anthropic_model,
                "groq": settings.groq_model,
                "gemini": settings.gemini_model,
                "cerebras": settings.cerebras_model,
                "openrouter": settings.openrouter_model,
                "nvidia": settings.nvidia_model,
                "ollama": settings.ollama_model,
            }.get(settings.llm_provider),
        },
        "stt": {
            "provider": settings.stt_provider,
            "model": {
                "groq": settings.groq_stt_model,
                "openai": settings.openai_stt_model,
                "deepgram": settings.deepgram_model,
                "local": settings.local_whisper_model,
            }.get(settings.stt_provider),
        },
        "tts": {
            "provider": settings.tts_provider,
            "voice_or_model": {
                "elevenlabs": settings.elevenlabs_voice_id,
                "openai": settings.openai_tts_voice,
                "deepgram": settings.deepgram_tts_voice,
                "cartesia": settings.cartesia_voice_id,
                "qwen3": settings.qwen3_tts_model_id,
                "kokoro": settings.kokoro_voice,
                "local": settings.piper_binary,
            }.get(settings.tts_provider),
        },
        "vad_kind": settings.vad_kind,
        "pii_redactor": settings.pii_redactor,
        "tracer_kind": settings.tracer_kind,
        "business_profile_path": settings.business_profile_path,
        # Sprint 9 + Sprint 10 flag state — one call to /debug/config
        # tells us what intelligence is active for the current call.
        "intelligence_flags": {
            "twilio_use_actor": getattr(settings, "twilio_use_actor", False),
            "two_planner_enabled": getattr(settings, "two_planner_enabled", False),
            "two_stage_barge_in_enabled": getattr(
                settings, "two_stage_barge_in_enabled", False,
            ),
            "dialogue_kernel_enabled": getattr(
                settings, "dialogue_kernel_enabled", False,
            ),
            "streaming_stt_enabled": getattr(
                settings, "streaming_stt_enabled", False,
            ),
            "turn_manager_enabled": getattr(
                settings, "turn_manager_enabled", False,
            ),
            "telephony_output_gain_db": getattr(
                settings, "telephony_output_gain_db", 0.0,
            ),
        },
    }


# ── Sprint 10 obs: call event log endpoints ─────────────────────────

@router.get("/call/{call_id}")
def get_call_timeline(call_id: str, limit: int = 500) -> dict:
    """Return the full event timeline for a single call from the
    durable call_events table.  Includes state transitions, tool
    outcomes, classified errors, playback marks.

    Feed this into `jq`, a browser tab, or the /graph dashboard's
    call detail view.  Newest first."""
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    events = log.timeline(call_id, limit=limit)
    return {
        "call_id": call_id,
        "event_count": len(events),
        "events": events,
    }


@router.get("/errors/recent")
def get_recent_errors(hours: int = 24, tenant_id: Optional[str] = None,
                      limit: int = 200) -> dict:
    """Recent classified errors, newest first.  Filter by tenant_id
    query param for per-tenant failure investigation."""
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    events = log.recent_errors(tenant_id=tenant_id, hours=hours, limit=limit)
    return {
        "window_hours": hours,
        "tenant_id": tenant_id,
        "count": len(events),
        "errors": events,
    }


@router.get("/capabilities")
def get_capabilities() -> dict:
    """Sprint 11a: full LLM capability table + per-operation approved
    model lists.  Answers 'which model handles main_brain vs perf_planner
    vs write_guard right now?'"""
    from packages.dialogue import capability_snapshot
    return capability_snapshot()


@router.get("/errors/taxonomy")
def get_error_taxonomy(hours: int = 24, tenant_id: Optional[str] = None) -> dict:
    """Rollup counts by ErrorCategory over the last N hours.  This is
    the "measure failures" surface — see which failure modes are
    dominating and plan fixes accordingly."""
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    counts = log.error_category_counts(tenant_id=tenant_id, hours=hours)
    total = sum(counts.values())
    return {
        "window_hours": hours,
        "tenant_id": tenant_id,
        "total_errors": total,
        "by_category": counts,
    }


@router.get("/call/{call_id}/timeline")
def get_call_semantic_timeline(call_id: str, limit: int = 500) -> dict:
    """Sprint 10 STREAMING WIRING: readable semantic timeline for
    demo debrief.  Turns the raw event log into a per-turn narrative:

        turn 0 (greeting)
          agent → "Hi, thanks for calling ..."
        turn 1 (user)
          [stt_partial x3, stt_final]
          user → "book a cleaning next Thursday"
          [end_of_turn]
          [state: task_added book_1]
          agent → "Sure — is 10:30 AM okay?"
        turn 2 (user)
          [stt_partial]
          [interruption]
          [reconcile → heard: "Sure — is 10:30"]
          user → "actually make that Thursday afternoon"
          ...

    Use this to figure out what went wrong on a demo call in one glance."""
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    events = log.timeline(call_id, limit=limit)

    # Group by turn_generation and preserve chronological order within each
    turns: dict[int, list[dict]] = {}
    for ev in reversed(events):   # log returns newest-first; want oldest-first
        tg = int(ev.get("turn_generation") or 0)
        turns.setdefault(tg, []).append(ev)

    # Build narrative
    narrative = []
    for tg in sorted(turns.keys()):
        turn_events = turns[tg]
        events_summary = []
        for ev in turn_events:
            src = ev.get("source")
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            summary = {
                "source": src, "kind": kind,
                "wall_ts": ev.get("wall_ts"),
            }
            # Extract the most useful field per event type
            if src == "stt":
                summary["text"] = payload.get("text", "")[:200]
            elif src == "control":
                summary["text"] = payload.get("text", "")[:200]
            elif src == "state":
                summary["detail"] = {k: str(v)[:100]
                                     for k, v in payload.items()
                                     if k in ("task_id", "kind", "slot",
                                              "value", "status")}
            elif src == "commit":
                summary["detail"] = {
                    "outcome": payload.get("outcome"),
                    "action_id": payload.get("action_id"),
                    "error": payload.get("error"),
                }
            elif src == "error":
                summary["error_category"] = ev.get("error_category")
                summary["message"] = payload.get("message", "")[:200]
            events_summary.append(summary)
        narrative.append({"turn": tg, "events": events_summary})

    return {
        "call_id": call_id,
        "turn_count": len(turns),
        "event_count": len(events),
        "timeline": narrative,
    }


@router.get("/failures/patterns")
def get_failure_patterns(hours: int = 24, tenant_id: Optional[str] = None) -> dict:
    """Sprint 11c: cluster recent failures by category + signature +
    suggest a next action per cluster.  Answers 'what's my #1 failure
    mode right now and what should I do about it?'"""
    from packages.observability.failure_intelligence import failure_patterns_report
    return failure_patterns_report(hours=hours, tenant_id=tenant_id)


@router.get("/failures/call/{call_id}")
def get_failure_summary_for_call(call_id: str) -> dict:
    """Per-call failure profile — used from /debug/call/{id}.  Which
    categories fired, at which turns."""
    from packages.observability.failure_intelligence import per_call_failure_summary
    return per_call_failure_summary(call_id)


# ── Sprint 10 streaming-parity: live event stream for the browser widget ──

@router.websocket("/live")
async def debug_live_ws(ws: WebSocket, call_id: str) -> None:
    """Live-tail every classified event for `call_id` to the browser
    /call-stream widget.  Backfills the last 50 events on connect, then
    forwards each new fan-out from CallEventLog.subscribe.

    Dev-only surface — do not expose without auth in production."""
    await ws.accept()
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)

    def _enqueue_nowait(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # bounded drop rather than block writer

    def _on_event(event: dict) -> None:
        # Called from any thread (SQLite writer may be off the loop).
        # Schedule the queue put back on the loop so we don't touch it
        # from a foreign thread.
        try:
            loop.call_soon_threadsafe(_enqueue_nowait, event)
        except RuntimeError:
            # Loop closed — client is gone, nothing to do.
            pass

    log.subscribe(call_id, _on_event)
    try:
        # Backfill: last 50 events, oldest first (timeline returns newest first)
        history = list(reversed(log.timeline(call_id, limit=50)))
        for ev in history:
            await ws.send_json(ev)
        # Live tail
        while True:
            ev = await queue.get()
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Never crash on a client hangup or transient error.
        pass
    finally:
        log.unsubscribe(call_id, _on_event)
