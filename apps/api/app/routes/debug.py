"""Debug + observability endpoints.

- GET /debug/traces          → recent spans (requires TRACER_KIND=memory)
- GET /debug/traces/summary  → per-span-name stats: count, P50, P95, mean cost
- GET /debug/config          → what providers + models are currently wired
- POST /debug/traces/clear   → wipe the in-memory buffer

These only work when TRACER_KIND=memory. In prod you'd hit your OTel/Langfuse
dashboard instead — same span data, different consumer.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException

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
    }
