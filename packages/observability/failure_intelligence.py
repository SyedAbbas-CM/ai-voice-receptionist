"""Failure Intelligence Pipeline (Sprint 11c).

Built on the existing CallEventLog.  The event log already CAPTURES
+ classifies errors.  This module CLUSTERS them so we can see
patterns:

  * category × signature (e.g. PROVIDER_OUTAGE + "cerebras 503")
  * per-tenant hot spots
  * temporal bursts (12 errors in the last 5 minutes from one cluster)
  * first_bad_turn distribution (do errors always hit turn 3?)

Endpoint: GET /debug/failures/patterns
Returns a ranked list of failure clusters with:
  * cluster_key = category + signature stem
  * count over the window
  * affected calls (sample)
  * first_seen / last_seen
  * suggested_action (heuristic)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .call_event_log import ErrorCategory, get_call_event_log


@dataclass(frozen=True)
class FailureCluster:
    """One cluster of related failures with a suggested action."""
    cluster_key: str
    category: str
    signature_stem: str
    count: int
    affected_call_ids: list[str] = field(default_factory=list)
    first_seen_ts: float = 0.0
    last_seen_ts: float = 0.0
    suggested_action: str = ""


# Signature extraction — normalize error messages so similar failures
# cluster together.  Strip volatile bits: hex IDs, timestamps, phone
# numbers, tenant/call ids.
_SIG_CLEAN_PATTERNS = [
    (re.compile(r"\bCA-[a-zA-Z0-9]+\b"), "CALL_ID"),
    (re.compile(r"\bsess_[a-zA-Z0-9]+\b"), "SESS_ID"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "HEXADDR"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "HEXHASH"),
    (re.compile(r"\b\d{10,}\b"), "LONGINT"),
    (re.compile(r"cool_for_\d+s"), "cool_for_Ns"),
    (re.compile(r"attempts?\s*=\s*[^;]+"), "attempts=..."),
    (re.compile(r"turn_\d+"), "turn_N"),
    (re.compile(r"\bact_[a-f0-9]+\b"), "ACTION_ID"),
    (re.compile(r"\s+"), " "),
]


def _signature_stem(message: str, max_len: int = 120) -> str:
    """Reduce an error message to a stable signature for clustering."""
    if not message:
        return "(empty)"
    stem = message
    for pat, repl in _SIG_CLEAN_PATTERNS:
        stem = pat.sub(repl, stem)
    return stem.strip()[:max_len]


# ── suggested-action heuristics ─────────────────────────────────────

_ACTION_RULES: list[tuple[str, str]] = [
    (ErrorCategory.PROVIDER_OUTAGE.value,
     "Router should already be failing over.  If cluster >20/hr, "
     "check provider dashboard + rotate keys."),
    (ErrorCategory.ASR.value,
     "STT provider unstable.  Check DEEPGRAM_API_KEY + network path.  "
     "Consider falling back to local Whisper temporarily."),
    (ErrorCategory.STATE_REDUCTION.value,
     "Reducer rejected patches — likely the brain is emitting invalid "
     "state transitions.  Inspect the kernel wiring; may need a bug fix."),
    (ErrorCategory.TOOL_SELECTION.value,
     "LLM picked wrong or unknown tool.  Check prompt tool-schema drift "
     "(audit-3 P0-3) or add can_handle to a new handler."),
    (ErrorCategory.ARG_NORMALIZATION.value,
     "Tool got unparseable args (bad ISO date, missing evidence).  "
     "Prompt clarity or TemporalResolver wiring."),
    (ErrorCategory.RETRIEVAL.value,
     "RAG retrieval failing.  Check sqlite-vec install + index size + "
     "confidence threshold."),
    (ErrorCategory.TEMPORAL.value,
     "TemporalResolver rejecting utterances.  Extend patterns or "
     "improve caller re-ask prompt."),
    (ErrorCategory.TURN_TAKING.value,
     "Barge-in classifier confused.  Check VAD tuning + backchannel "
     "regex."),
    (ErrorCategory.DELIVERY.value,
     "TTS/audio pipeline failing.  Check ElevenLabs/Cartesia keys + "
     "voice IDs.  VPL compiler may be emitting bad payloads."),
    (ErrorCategory.POLICY.value,
     "Safety guard fired.  Usually correct behavior — audit if firing "
     "on legitimate turns."),
    (ErrorCategory.UNKNOWN.value,
     "Uncategorized — expand ErrorCategory hints in call_event_log.py."),
]

_ACTION_MAP = dict(_ACTION_RULES)


# ── main pipeline ───────────────────────────────────────────────────

def cluster_recent_failures(
    hours: int = 24,
    tenant_id: Optional[str] = None,
    top_n: int = 20,
    per_cluster_sample_size: int = 5,
) -> list[FailureCluster]:
    """Bucket errors from the last N hours by category+signature.
    Returns clusters ranked by count desc, top_n cap."""
    log = get_call_event_log()
    events = log.recent_errors(tenant_id=tenant_id, hours=hours, limit=5000)
    if not events:
        return []

    # key -> (category, signature_stem, count, calls, first_seen, last_seen)
    buckets: dict[str, dict] = {}
    for ev in events:
        cat = ev.get("error_category") or "unknown"
        payload = ev.get("payload") or {}
        msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
        sig = _signature_stem(msg)
        key = f"{cat}::{sig}"
        b = buckets.setdefault(key, {
            "category": cat,
            "signature_stem": sig,
            "count": 0,
            "calls": [],
            "first_seen": float("inf"),
            "last_seen": 0.0,
        })
        b["count"] += 1
        ts = float(ev.get("wall_ts") or 0)
        b["first_seen"] = min(b["first_seen"], ts)
        b["last_seen"] = max(b["last_seen"], ts)
        cid = ev.get("call_id")
        if cid and cid not in b["calls"] and len(b["calls"]) < per_cluster_sample_size:
            b["calls"].append(cid)

    # Rank + shape into FailureCluster records
    clusters: list[FailureCluster] = []
    for key, b in buckets.items():
        clusters.append(FailureCluster(
            cluster_key=key,
            category=b["category"],
            signature_stem=b["signature_stem"],
            count=b["count"],
            affected_call_ids=b["calls"],
            first_seen_ts=b["first_seen"],
            last_seen_ts=b["last_seen"],
            suggested_action=_ACTION_MAP.get(b["category"], _ACTION_MAP["unknown"]),
        ))
    clusters.sort(key=lambda c: -c.count)
    return clusters[:top_n]


def failure_patterns_report(
    hours: int = 24, tenant_id: Optional[str] = None,
) -> dict:
    """Full report for /debug/failures/patterns.

    Includes clusters + totals + a suggested_next_action for the
    highest-count cluster so operators see one clear thing to fix."""
    clusters = cluster_recent_failures(hours=hours, tenant_id=tenant_id)
    total = sum(c.count for c in clusters)
    return {
        "window_hours": hours,
        "tenant_id": tenant_id,
        "total_classified_errors": total,
        "cluster_count": len(clusters),
        "clusters": [
            {
                "cluster_key": c.cluster_key,
                "category": c.category,
                "signature_stem": c.signature_stem,
                "count": c.count,
                "affected_calls_sample": c.affected_call_ids,
                "first_seen_epoch": c.first_seen_ts,
                "last_seen_epoch": c.last_seen_ts,
                "duration_s": round(c.last_seen_ts - c.first_seen_ts, 1),
                "suggested_action": c.suggested_action,
            }
            for c in clusters
        ],
        "top_action": clusters[0].suggested_action if clusters else None,
    }


def per_call_failure_summary(call_id: str) -> dict:
    """One call's failure profile — used from /debug/call/{id}.
    Answers: which categories fired, at which turns, in what order."""
    log = get_call_event_log()
    timeline = log.timeline(call_id, limit=1000)
    errors = [e for e in timeline if e.get("source") == "error"]
    if not errors:
        return {"call_id": call_id, "error_count": 0, "categories": {}}
    categories: dict[str, int] = {}
    per_turn: dict[int, list[str]] = {}
    for e in errors:
        cat = e.get("error_category") or "unknown"
        categories[cat] = categories.get(cat, 0) + 1
        turn = int(e.get("turn_generation") or 0)
        per_turn.setdefault(turn, []).append(cat)
    first_bad_turn = min(per_turn.keys()) if per_turn else 0
    return {
        "call_id": call_id,
        "error_count": len(errors),
        "categories": categories,
        "per_turn": per_turn,
        "first_bad_turn": first_bad_turn,
    }
