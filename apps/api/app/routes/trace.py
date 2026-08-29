"""GET /trace/{call_id} — business-owner-scoped humanness trace view.

2026-08-29: incident.py already exposes an admin-gated raw JSON blob
for support engineers.  This route is the business-owner analog:

  * Tenant-scoped (uses the same dashboard bearer resolver).  A tenant
    can only see calls that belong to them.
  * Consumes the structured `humanness_events` schema landing in this
    same batch and projects each event into a narrative row the owner
    can read without knowing what "STT_FINAL" means.
  * Two response shapes:
      - GET /trace/{call_id}         → HTML for browser viewing
      - GET /trace/{call_id}?f=json  → JSON for tooling / scripts

Why this is worth its own route (not just adding to /dashboard):
  * dashboard shows a call list; trace opens ONE call for
    "what did the agent do, why, and what tripped up."
  * Owners want the humanness story — "agent detected the caller was
    frustrated, dropped the upbeat energy, took a message when the
    transfer failed" — not the STT frame log.
  * Support tickets link to a trace URL and the owner can inspect
    without an admin key.

Design notes:
  * Read-only.  No side effects.
  * Falls back to /admin/calls/{call_id}/incident semantics when
    humanness events are absent, so pre-schema calls still render
    something useful.
  * Every render is <100 KB and inlines its own CSS — no external deps.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.db import SessionRow, TranscriptRow, BookingRow
from app.db.session import get_session


log = logging.getLogger(__name__)


router = APIRouter(prefix="/trace", tags=["trace"])


# ── tenant resolution (borrows dashboard.py's pattern) ─────────────


def _resolve_trace_tenant(request: Request) -> str:
    """Resolve the tenant for a /trace request.

    Prefers the tenant already set by the auth middleware
    (request.state.tenant_id) — that's how the rest of the app works
    when API_AUTH_ENFORCE=true.  Falls back to the dashboard resolver
    (Bearer / ?token= handling) so an explicit token still works from
    tests or a widget context that talks straight to /trace.
    """
    # Middleware-set tenant wins.
    middleware_tenant = getattr(
        request.state, "tenant_id", None,
    )
    if middleware_tenant and middleware_tenant != "dev":
        return middleware_tenant
    # Enforce is off (dev) OR middleware skipped this path — read the
    # Bearer explicitly so tenant scoping is still meaningful.
    from app.routes.dashboard import _resolve_dashboard_tenant
    return _resolve_dashboard_tenant(request)


def _session_id_from_call_id(call_id: str) -> str:
    if call_id.startswith("twilio_"):
        return call_id
    return f"twilio_{call_id}"


# ── humanness projections ──────────────────────────────────────────


# What we render for each humanness event_kind.  Owner-friendly copy,
# severity used for row coloring.
_HUMANNESS_KIND_LABELS: dict[str, tuple[str, str]] = {
    # (label, severity: info/warn/error)
    "empty_llm_completion": (
        "LLM returned nothing — will retry", "warn",
    ),
    "empty_llm_rescue": (
        "Rescue retry after empty LLM completion", "warn",
    ),
    "empty_llm_deterministic_fallback": (
        "LLM failed twice — spoke canned fallback", "error",
    ),
    "policy_decision": (
        "Turn policy decided next action", "info",
    ),
    "turn_signal_reduced": (
        "Caller turn signals read", "info",
    ),
    "service_resolution": (
        "Service name canonicalized", "info",
    ),
    "barge_in_detected": (
        "Caller interrupted", "info",
    ),
    "speech_gate_dropped": (
        "Speech gate blocked a sentence", "warn",
    ),
    "transfer_attempt": (
        "Transfer to human attempted", "info",
    ),
    "llm_claim_guard": (
        "Guard blocked an unverifiable claim", "warn",
    ),
}


def _humanness_insight(kind: str, payload: dict) -> str:
    """One-line reader-friendly explanation of what this event means
    to a business owner (no engineering jargon)."""
    p = payload or {}
    if kind == "empty_llm_completion":
        return (
            f"The AI got the caller message "
            f"{p.get('user_text', '')!r} but returned zero words. "
            f"Watchdog will retry once."
        )
    if kind == "empty_llm_rescue":
        recovered = (
            p.get("recovered_text") or p.get("recovered_tools")
        )
        return (
            "Retry succeeded." if recovered
            else "Retry also empty — deterministic fallback next."
        )
    if kind == "empty_llm_deterministic_fallback":
        return (
            f"Both attempts empty.  Agent spoke: "
            f"{p.get('fallback_text', '')!r}"
        )
    if kind == "policy_decision":
        act = p.get("action", "?")
        ack = p.get("acknowledgment") or "none"
        delivery = p.get("delivery_intent", "standard")
        return (
            f"Action={act}, ack={ack}, delivery={delivery}"
            + (
                f", requesting slot: {p['requested_slot']}"
                if p.get("requested_slot") else ""
            )
        )
    if kind == "turn_signal_reduced":
        flags = []
        if p.get("caller_shared_hardship"):
            flags.append("hardship")
        if p.get("caller_corrected_us"):
            flags.append("correction")
        if p.get("caller_is_dictating"):
            flags.append("dictating")
        if p.get("caller_asked_to_wait"):
            flags.append("wait")
        return (
            "Signals: " + (", ".join(flags) or "none")
            + (
                f" (reasons: {', '.join(p.get('reasons', []))})"
                if p.get("reasons") else ""
            )
        )
    if kind == "service_resolution":
        spoken = p.get("spoken", "?")
        kind_val = p.get("kind", "?")
        canon = p.get("canonical_name")
        if kind_val == "match_exact" and canon:
            return f"Caller said {spoken!r} → matched {canon!r}"
        if kind_val == "match_fuzzy" and canon:
            return (
                f"Caller said {spoken!r} → fuzzy-matched {canon!r} "
                f"(confidence {p.get('confidence', 0):.2f})"
            )
        if kind_val == "ambiguous":
            return (
                f"Caller said {spoken!r} — matched multiple: "
                f"{', '.join(p.get('candidates', []))}"
            )
        if kind_val == "unknown":
            return (
                f"Caller said {spoken!r} — no matching service; "
                f"agent asked for clarification"
            )
        return f"Resolution: {kind_val}"
    if kind == "barge_in_detected":
        bkind = p.get("kind", "real")
        wc = p.get("word_count", 0)
        dur = p.get("speech_duration_ms", 0)
        if bkind == "real":
            return f"Real interruption ({wc} words / {dur} ms)"
        if bkind == "false_positive":
            return f"False positive — kept speaking ({wc} words)"
        if bkind == "backchannel":
            return "Backchannel (mhm/yeah) — kept speaking"
        if bkind == "min_words_not_met":
            return (
                f"Too short to be a real interrupt ({wc} words vs "
                f"min {p.get('min_words_required', 2)})"
            )
        return f"Barge-in kind: {bkind}"
    if kind == "speech_gate_dropped":
        return (
            f"Category {p.get('category', 'safe')} — dropped: "
            f"{p.get('sentence_preview', '')!r}"
        )
    if kind == "transfer_attempt":
        mode = p.get("mode", "?")
        outcome = p.get("outcome", "in_progress")
        dest = p.get("destination_label") or p.get("destination_id") or "?"
        return f"{mode.title()} transfer to {dest} — {outcome}"
    if kind == "llm_claim_guard":
        return (
            f"Guard {p.get('guard', '?')}: "
            f"{p.get('action_taken', 'rewrote')} — "
            f"{p.get('claim_text_preview', '')!r}"
        )
    return ""


def _severity_for_event(kind: str, payload: dict) -> str:
    label = _HUMANNESS_KIND_LABELS.get(kind)
    if not label:
        return "info"
    base = label[1]
    # Escalate certain outcomes based on payload.
    if kind == "transfer_attempt":
        outcome = (payload or {}).get("outcome", "")
        if outcome in ("failed", "policy_blocked"):
            return "error"
        if outcome == "no_answer":
            return "warn"
    if kind == "service_resolution":
        if (payload or {}).get("kind") in ("ambiguous", "unknown"):
            return "warn"
    return base


def _to_iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _project_events(events: list[dict]) -> list[dict]:
    """Turn raw call_events rows into narrative-friendly row dicts.

    Every row: {ts, kind, label, severity, insight, raw}
    """
    rows = []
    for e in events:
        kind = e.get("kind", "")
        payload = e.get("payload") or {}
        if kind in _HUMANNESS_KIND_LABELS:
            label = _HUMANNESS_KIND_LABELS[kind][0]
            severity = _severity_for_event(kind, payload)
            insight = _humanness_insight(kind, payload)
        else:
            # Non-humanness event — render minimally so the timeline
            # still has continuity with STT/TTS milestones.
            label = kind
            severity = "info"
            insight = ""
        rows.append({
            "ts": _to_iso(e.get("ts") or e.get("timestamp")),
            "kind": kind,
            "label": label,
            "severity": severity,
            "insight": insight,
            "raw": e,
        })
    return rows


# ── data loading ────────────────────────────────────────────────


def _load_call_events(call_id: str, limit: int = 5000) -> list[dict]:
    """Pull raw call_events for a call.  Empty on missing log."""
    try:
        from packages.observability.call_event_log import (
            get_call_event_log,
        )
        log_writer = get_call_event_log()
        if log_writer is None:
            return []
        return list(reversed(log_writer.timeline(call_id, limit=limit)))
    except Exception as e:
        log.warning("trace._load_call_events failed for %s: %s", call_id, e)
        return []


def _load_session_and_verify(
    db: Session, call_id: str, tenant_id: str,
) -> Optional[SessionRow]:
    """Look up the session and enforce tenant ownership.

    Returns None if the session doesn't exist.  Raises 403 if it
    exists but belongs to a different tenant — otherwise a tenant
    could probe call IDs and see whether they exist.
    """
    session_id = _session_id_from_call_id(call_id)
    sess = (
        db.query(SessionRow)
        .execution_options(skip_tenant_filter=True)
        .filter(SessionRow.id == session_id)
        .one_or_none()
    )
    if sess is None:
        return None
    if sess.tenant_id != tenant_id:
        # Ownership violation.  Return the same 404 shape as
        # session-not-found to avoid leaking existence.
        return None
    return sess


# ── endpoints ───────────────────────────────────────────────────


@router.get("/{call_id}")
def get_trace(
    call_id: str,
    request: Request,
    f: str = Query(
        "html",
        description="Response format: 'html' (default) or 'json'.",
    ),
    db: Session = Depends(get_session),
):
    """Business-owner-scoped humanness trace for one call."""
    tenant_id = _resolve_trace_tenant(request)
    session_id = _session_id_from_call_id(call_id)

    sess = _load_session_and_verify(db, call_id, tenant_id)
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"call {call_id!r} not found or not accessible to "
                f"tenant {tenant_id!r}"
            ),
        )

    # Load transcript, bookings, events.
    turns = (
        db.query(TranscriptRow)
        .execution_options(skip_tenant_filter=True)
        .filter(TranscriptRow.session_id == session_id)
        .order_by(TranscriptRow.timestamp.asc())
        .all()
    )
    transcript = [
        {
            "role": t.role, "text": t.text,
            "ts": _to_iso(t.timestamp),
            "tool_name": t.tool_name,
        }
        for t in turns
    ]
    bookings = (
        db.query(BookingRow)
        .execution_options(skip_tenant_filter=True)
        .filter(BookingRow.session_id == session_id)
        .order_by(BookingRow.created_at.asc())
        .all()
    )
    bookings_data = [
        {
            "id": b.id, "service": b.service,
            "scheduled_for": _to_iso(b.scheduled_for),
            "caller_name": b.caller_name, "phone": b.phone,
            "status": b.status,
        }
        for b in bookings
    ]
    raw_events = _load_call_events(call_id)
    projected = _project_events(raw_events)

    trace_body = {
        "call_id": call_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "session": {
            "status": sess.status,
            "started_at": _to_iso(sess.started_at),
            "ended_at": _to_iso(sess.ended_at),
            "escalation_reason": sess.escalation_reason,
        },
        "transcript": transcript,
        "bookings": bookings_data,
        "events": projected,
        "counts": {
            "turns": len(transcript),
            "bookings": len(bookings_data),
            "events": len(projected),
            "humanness_events": sum(
                1 for r in projected
                if r["kind"] in _HUMANNESS_KIND_LABELS
            ),
        },
    }

    if f.lower() == "json":
        return JSONResponse(trace_body)
    return HTMLResponse(_render_trace_html(trace_body))


# ── HTML render ─────────────────────────────────────────────────


_TRACE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 0; padding: 20px; background: #f7f7f9; color: #1a1a1a; }
h1 { font-size: 18px; margin: 0 0 4px 0; }
h2 { font-size: 14px; margin: 24px 0 8px 0;
     text-transform: uppercase; color: #666; letter-spacing: 0.05em; }
.meta { color: #666; font-size: 12px; margin-bottom: 20px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 10px;
        font-size: 11px; margin-right: 6px; }
.pill-info { background: #e8f0fe; color: #1a56db; }
.pill-warn { background: #fef3c7; color: #92400e; }
.pill-error { background: #fee2e2; color: #991b1b; }
table { width: 100%; border-collapse: collapse; background: white;
        border-radius: 6px; overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee;
         font-size: 13px; vertical-align: top; }
th { background: #f0f0f2; font-weight: 600; }
.ts { color: #888; font-size: 11px; font-family: monospace;
      white-space: nowrap; }
.role-assistant { background: #f0f7ff; }
.role-user { background: white; }
.severity-warn td { background: #fffbeb !important; }
.severity-error td { background: #fef2f2 !important; }
details { margin-top: 4px; }
summary { cursor: pointer; color: #666; font-size: 11px; }
pre { background: #f5f5f5; padding: 8px; border-radius: 4px;
      overflow-x: auto; font-size: 11px; }
"""


def _esc(s: object) -> str:
    return html.escape(str(s or ""), quote=True)


def _render_trace_html(body: dict) -> str:
    session = body["session"]
    counts = body["counts"]
    rows = "".join(
        f'<tr class="role-{_esc(t["role"])}">'
        f'<td class=ts>{_esc(t["ts"])}</td>'
        f'<td>{_esc(t["role"])}</td>'
        f'<td>{_esc(t["text"])}</td>'
        f'<td>{_esc(t["tool_name"] or "")}</td>'
        f'</tr>'
        for t in body["transcript"]
    ) or (
        '<tr><td colspan=4 style="text-align:center;color:#999;">'
        '(no transcript)</td></tr>'
    )
    event_rows = "".join(
        f'<tr class="severity-{_esc(e["severity"])}">'
        f'<td class=ts>{_esc(e["ts"])}</td>'
        f'<td><span class="pill pill-{_esc(e["severity"])}">'
        f'{_esc(e["label"])}</span></td>'
        f'<td>{_esc(e["insight"])}</td>'
        f'</tr>'
        for e in body["events"]
        if e["kind"] and e["insight"]
    ) or (
        '<tr><td colspan=3 style="text-align:center;color:#999;">'
        '(no humanness events recorded)</td></tr>'
    )
    booking_rows = "".join(
        f'<tr>'
        f'<td>{_esc(b["service"])}</td>'
        f'<td>{_esc(b["scheduled_for"])}</td>'
        f'<td>{_esc(b["caller_name"])}</td>'
        f'<td>{_esc(b["phone"])}</td>'
        f'<td>{_esc(b["status"])}</td>'
        f'</tr>'
        for b in body["bookings"]
    )
    booking_block = (
        f'<h2>Bookings ({counts["bookings"]})</h2>'
        f'<table><thead><tr>'
        f'<th>Service</th><th>When</th><th>Caller</th>'
        f'<th>Phone</th><th>Status</th>'
        f'</tr></thead><tbody>{booking_rows}</tbody></table>'
    ) if counts["bookings"] else ""

    return (
        f'<!doctype html><html><head>'
        f'<meta charset=utf-8>'
        f'<title>Call {_esc(body["call_id"])} · trace</title>'
        f'<style>{_TRACE_CSS}</style>'
        f'</head><body>'
        f'<h1>Call trace: {_esc(body["call_id"])}</h1>'
        f'<div class=meta>'
        f'Status: {_esc(session["status"])} · '
        f'Started: {_esc(session["started_at"])} · '
        f'Ended: {_esc(session["ended_at"] or "in progress")}'
        + (
            f' · Escalation: {_esc(session["escalation_reason"])}'
            if session["escalation_reason"] else ""
        )
        + f' · Turns: {counts["turns"]} · '
        f'Humanness events: {counts["humanness_events"]}'
        f'</div>'
        f'<h2>Transcript ({counts["turns"]} turns)</h2>'
        f'<table><thead><tr>'
        f'<th>When</th><th>Role</th><th>Text</th><th>Tool</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        f'<h2>Humanness timeline ({counts["humanness_events"]} events)</h2>'
        f'<table><thead><tr>'
        f'<th>When</th><th>What</th><th>Insight</th>'
        f'</tr></thead><tbody>{event_rows}</tbody></table>'
        f'{booking_block}'
        f'</body></html>'
    )
