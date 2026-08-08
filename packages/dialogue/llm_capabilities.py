"""Capability-Aware LLM Routing (Sprint 11a).

The audit's finding:
    RouterLLM is availability routing, not intelligence routing.  A
    booking call can silently move from a strong model to a weaker
    model halfway through, because rank order doesn't consider what
    the operation actually needs.

Fix: every model declares its capabilities + which operations it's
approved for.  CapabilityRouter picks per-operation instead of
per-rank.

Example:
    main brain (tool-heavy booking) → must be reliable_tool_calling +
        structured_output; groq/llama-3.3-70b or cerebras/gpt-oss-120b
    perf planner (structured output, latency-critical) → fast_class +
        structured_output; groq/llama-3.1-8b
    extractor (background, cheap) → any latency class
    write_guard (safety-critical) → same tier as main brain, no
        downgrade

Kept as a policy layer OVER the existing RouterLLM.  When a caller
asks the router for `operation=main_brain`, we filter the router's
provider list to approved_operations includes 'main_brain', then let
the existing router handle failover within that filtered list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LatencyClass(str, Enum):
    """Realtime  = < 500ms p50, use for perf planner + realtime scoring
    Fast      = 500-1500ms p50, use for main brain + write guard
    Deliberate = > 1500ms ok, use for background extraction + analytics"""
    REALTIME = "realtime"
    FAST = "fast"
    DELIBERATE = "deliberate"


class Operation(str, Enum):
    """Which kind of LLM call is this?  Every call site declares its
    operation; the router picks a suitable model."""
    MAIN_BRAIN = "main_brain"          # receptionist reasoning + tool selection
    PERF_PLANNER = "perf_planner"      # VPL delivery block, latency-critical
    EXTRACTOR = "extractor"            # background structured extraction
    WRITE_GUARD = "write_guard"        # safety validation before booking
    RAG_SHAPER = "rag_shaper"          # rewrite retrieved text for voice
    SEMANTIC_PLANNER = "semantic_planner"  # future: emit dialogue plan
    SUMMARIZATION = "summarization"    # post-call summary


@dataclass(frozen=True)
class ModelCapabilities:
    """Per-model contract.  Immutable — construct at config-load time.

    approved_operations governs which call sites the router will hand
    this model.  A weaker model with structured_output=True but
    reliable_tool_calling=False stays out of MAIN_BRAIN even if it's
    faster + cheaper."""
    provider: str
    model: str
    reliable_tool_calling: bool
    structured_output: bool
    audio_input: bool = False
    streaming_tool_deltas: bool = False
    multilingual: frozenset[str] = frozenset({"en"})
    latency_class: LatencyClass = LatencyClass.FAST
    approved_operations: frozenset[Operation] = frozenset()
    # Free-text hint for humans reading /debug/capabilities
    notes: str = ""


# ── real capability table (benched 2026-08-04) ──────────────────────

# Every entry below is grounded in bench_llms.py output.  Keep this
# table in sync with the benchmark — if a model changes latency class
# or loses tool-calling, update here.
CAPABILITY_TABLE: list[ModelCapabilities] = [
    # ═══ GROQ ═══
    ModelCapabilities(
        provider="groq", model="llama-3.3-70b-versatile",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.FAST,   # 1400ms p50
        approved_operations=frozenset({
            Operation.MAIN_BRAIN, Operation.SEMANTIC_PLANNER,
            Operation.WRITE_GUARD, Operation.EXTRACTOR,
            Operation.SUMMARIZATION, Operation.RAG_SHAPER,
        }),
        notes="Workhorse — reliable on all serious operations",
    ),
    ModelCapabilities(
        provider="groq", model="llama-3.1-8b-instant",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.REALTIME,   # 213ms — fastest overall
        approved_operations=frozenset({
            Operation.PERF_PLANNER, Operation.EXTRACTOR,
            Operation.RAG_SHAPER,
        }),
        notes="Fastest anywhere; use for perf planner + background work",
    ),
    ModelCapabilities(
        provider="groq", model="openai/gpt-oss-120b",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.FAST,   # 665ms
        approved_operations=frozenset({
            Operation.MAIN_BRAIN, Operation.SEMANTIC_PLANNER,
            Operation.SUMMARIZATION,
        }),
        notes="Big reasoning model at fast speed — good for tough turns",
    ),
    # ═══ CEREBRAS ═══
    ModelCapabilities(
        provider="cerebras", model="gpt-oss-120b",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.FAST,   # 485ms
        approved_operations=frozenset({
            Operation.MAIN_BRAIN, Operation.SEMANTIC_PLANNER,
            Operation.WRITE_GUARD, Operation.SUMMARIZATION,
        }),
        notes="~2000 tok/s Cerebras hardware; big model, fast",
    ),
    # ═══ MISTRAL ═══
    ModelCapabilities(
        provider="mistral", model="mistral-large-latest",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.FAST,   # 655ms
        multilingual=frozenset({"en", "fr", "de", "es", "it"}),
        approved_operations=frozenset({
            Operation.MAIN_BRAIN, Operation.SEMANTIC_PLANNER,
            Operation.WRITE_GUARD, Operation.EXTRACTOR,
        }),
        notes="EU-hosted, strong multilingual",
    ),
    ModelCapabilities(
        provider="mistral", model="ministral-8b-latest",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.REALTIME,   # 546ms
        approved_operations=frozenset({
            Operation.PERF_PLANNER, Operation.EXTRACTOR,
            Operation.RAG_SHAPER,
        }),
    ),
    ModelCapabilities(
        provider="mistral", model="ministral-3b-latest",
        reliable_tool_calling=False, structured_output=True,
        latency_class=LatencyClass.REALTIME,   # 485ms
        approved_operations=frozenset({
            Operation.PERF_PLANNER, Operation.EXTRACTOR,
        }),
        notes="Too small for tool calling reliably; JSON-only",
    ),
    # ═══ GEMINI ═══
    ModelCapabilities(
        provider="gemini", model="gemini-2.5-flash-lite",
        reliable_tool_calling=False, structured_output=True,
        latency_class=LatencyClass.FAST,   # 1535ms
        multilingual=frozenset({"en", "es", "fr", "de", "hi", "ja", "zh"}),
        approved_operations=frozenset({
            Operation.EXTRACTOR, Operation.SUMMARIZATION,
            Operation.RAG_SHAPER,
        }),
        notes="Tool-calling flaky on flash-lite; safe for text-only",
    ),
    # ═══ NVIDIA ═══
    ModelCapabilities(
        provider="nvidia", model="meta/llama-3.1-70b-instruct",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.DELIBERATE,   # 1070ms — slower
        approved_operations=frozenset({
            Operation.MAIN_BRAIN, Operation.WRITE_GUARD,
            Operation.EXTRACTOR,
        }),
        notes="Backup — use when groq/cerebras/mistral all cooling down",
    ),
    ModelCapabilities(
        provider="nvidia", model="meta/llama-3.1-8b-instruct",
        reliable_tool_calling=True, structured_output=True,
        latency_class=LatencyClass.DELIBERATE,   # 864ms
        approved_operations=frozenset({
            Operation.PERF_PLANNER, Operation.EXTRACTOR,
        }),
    ),
]


# ── capability lookup helpers ───────────────────────────────────────

def models_for_operation(
    operation: Operation,
    max_latency: Optional[LatencyClass] = None,
    require_tool_calling: bool = False,
    locale: str = "en",
) -> list[ModelCapabilities]:
    """Return all models approved for the given operation, filtered
    by latency + tool-calling + locale requirements.  Ordered by
    latency_class (fastest first) then by table order (stable)."""
    latency_rank = {
        LatencyClass.REALTIME: 0,
        LatencyClass.FAST: 1,
        LatencyClass.DELIBERATE: 2,
    }
    max_rank = latency_rank[max_latency] if max_latency else 999

    def _accept(cap: ModelCapabilities) -> bool:
        if operation not in cap.approved_operations:
            return False
        if latency_rank[cap.latency_class] > max_rank:
            return False
        if require_tool_calling and not cap.reliable_tool_calling:
            return False
        if locale not in cap.multilingual:
            return False
        return True

    approved = [c for c in CAPABILITY_TABLE if _accept(c)]
    approved.sort(key=lambda c: latency_rank[c.latency_class])
    return approved


def preferred_order_for(
    operation: Operation,
    max_latency: Optional[LatencyClass] = None,
    require_tool_calling: bool = False,
    locale: str = "en",
) -> str:
    """Return an LLM_ROUTER_ORDER-compatible provider list for a
    specific operation, ready to hand to RouterLLM.  De-duplicates
    providers (first-approved model per provider wins)."""
    approved = models_for_operation(
        operation, max_latency=max_latency,
        require_tool_calling=require_tool_calling, locale=locale,
    )
    seen: set[str] = set()
    order: list[str] = []
    for cap in approved:
        if cap.provider in seen:
            continue
        seen.add(cap.provider)
        order.append(cap.provider)
    return ",".join(order)


def preferred_model_for(
    operation: Operation,
    max_latency: Optional[LatencyClass] = None,
    require_tool_calling: bool = False,
    locale: str = "en",
) -> Optional[ModelCapabilities]:
    """Return the SINGLE best model for an operation (fastest approved
    that satisfies constraints).  Used by call sites that want a
    dedicated client rather than a router (e.g. PerformancePlanner)."""
    approved = models_for_operation(
        operation, max_latency=max_latency,
        require_tool_calling=require_tool_calling, locale=locale,
    )
    return approved[0] if approved else None


def capability_snapshot() -> dict:
    """Debug endpoint — dump the full capability table + per-operation
    approved-model lists.  Wire into /debug/capabilities."""
    per_operation = {}
    for op in Operation:
        per_operation[op.value] = [
            f"{c.provider}:{c.model}"
            for c in models_for_operation(op)
        ]
    table = [
        {
            "provider": c.provider, "model": c.model,
            "latency_class": c.latency_class.value,
            "reliable_tool_calling": c.reliable_tool_calling,
            "structured_output": c.structured_output,
            "approved_operations": sorted(op.value for op in c.approved_operations),
            "multilingual": sorted(c.multilingual),
            "notes": c.notes,
        }
        for c in CAPABILITY_TABLE
    ]
    return {"table": table, "per_operation": per_operation}
