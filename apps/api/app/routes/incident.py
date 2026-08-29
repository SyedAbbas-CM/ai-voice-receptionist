"""P1 incident view — one query for a call's full timeline.

FULL-CODEBASE-AUDIT-2026-08-26-CHATGPT.md flagged: "no canonical call-trace
lookup — pieces exist but not one call → tenant → transcript → tool_calls
→ booking → CRM_writes → SMS/email view."

This is that view. Read-only. Admin-gated (same ADMIN_TOKEN as tenant
onboarding routes). No schema changes.

## Why this matters right now

Voice-agent is investigating BUG-02 (Roxana call, "Wed Sept 9" → agent
said "Sept 13"). Without this route, bisecting takes 30 min of grepping.
With it, one HTTP call returns:

  * session row (business, status, timings)
  * every transcript turn (user + assistant)
  * every tool_call + tool_result
  * every booking created during the session
  * every call_event row (STT, LLM, TTS, tool markers, errors)

Fed as a single JSON blob, sorted by timestamp. UI can lay it out however.

## Endpoints

  * `GET /admin/calls/{call_id}/incident`
      Full aggregated timeline for one call.

  * `GET /admin/calls/{call_id}/summary`
      Same call but stripped of the noisy per-frame events — just
      transcript + tool calls + booking + errors. What a support-eng
      opens first.

## Not in v1

- No pagination on per-call events (5000-row cap on write path already limits).
- No cross-call queries (`recent errors for tenant X`) — that's what
  `debug.py` already exposes via `/debug/errors/recent`.
- Not exposed via /debug/ prefix — /debug/ has its own auth gating
  (P0.2 landed) and this belongs with tenant-admin ops, not observability.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import BookingRow, SessionRow, TranscriptRow
from app.db.session import get_session

# Reuse the admin-token gate that provisioning routes already use.
# Same security posture — this is a support-engineering tool, not a
# tenant-facing endpoint.
from app.routes.admin import _require_admin

router = APIRouter(prefix="/admin/calls", tags=["admin", "incident"])


# ─── helpers ────────────────────────────────────────────────────────────────


def _session_id_from_call_id(call_id: str) -> str:
    """We store sessions with id = f"twilio_{CallSid}" for Twilio calls.
    Support both the raw CA-SID and the prefixed form so support-eng can
    paste either into the URL."""
    if call_id.startswith("twilio_"):
        return call_id
    return f"twilio_{call_id}"


def _to_iso(dt: Any) -> Optional[str]:
    """Datetime → ISO 8601 string, tolerating None + already-string."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _load_call_events(call_id: str, limit: int = 5000) -> list[dict]:
    """Pull raw call_events rows for a call. Empty list if the log is
    disabled or the call has no events."""
    try:
        from packages.observability.call_event_log import get_call_event_log
        log = get_call_event_log()
        # timeline() returns newest first; we want oldest → newest for
        # incident reconstruction, so reverse.
        return list(reversed(log.timeline(call_id, limit=limit)))
    except Exception:
        return []


def _classify_summary_kinds(events: list[dict]) -> list[dict]:
    """Filter call_events down to the ones a human incident-responder
    cares about. Drops per-frame STT_PARTIAL noise and audio chunk
    markers, keeps turn boundaries + tool calls + errors + explicit
    state transitions."""
    KEEP = {
        "STT_FINAL", "STT_VAD",
        "LLM_STREAM_START", "LLM_STREAM_DONE", "LLM_FIRST_TEXT",
        "TOOL_CALL", "TOOL_RESULT",
        "TTS_STREAM_START", "TTS_SENTENCE_QUEUED", "TTS_STREAM_DONE",
        "TWILIO_START", "TWILIO_END_CALL_OK", "TWILIO_STATUS_CALLBACK",
        "TURN_STALLED", "SPEECH_GATE_DROPPED", "STREAM_REPLY_REPLACED",
        "NEXT_ACTION_SYNTH_HIT",
        "CallActor started", "CallActor stopped",
    }
    return [
        e for e in events
        if e.get("kind") in KEEP or e.get("error_category")
    ]


# ─── routes ────────────────────────────────────────────────────────────────


@router.get("/{call_id}/incident")
def get_incident(
    call_id: str,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    """Full per-call trace. Returns everything we know about the call
    in one JSON blob. Support engineers paste the CA-SID into the URL
    and get back the complete timeline without grepping."""
    _require_admin(request)

    session_id = _session_id_from_call_id(call_id)

    # ── session row (may be absent for a call that never got past accept) ──
    # skip_tenant_filter because this is an admin tool — cross-tenant
    # ops are the point. Same pattern as the api-key lookup in auth.py.
    sess = (
        db.query(SessionRow)
        .execution_options(skip_tenant_filter=True)
        .filter(SessionRow.id == session_id)
        .one_or_none()
    )
    session_data: Optional[dict] = None
    if sess is not None:
        session_data = {
            "id": sess.id,
            "tenant_id": sess.tenant_id,
            "business_id": sess.business_id,
            "status": sess.status,
            "started_at": _to_iso(sess.started_at),
            "ended_at": _to_iso(sess.ended_at),
            "extracted": sess.extracted,
            "escalation_reason": sess.escalation_reason,
        }

    # ── transcript turns ──
    turns = (
        db.query(TranscriptRow)
        .execution_options(skip_tenant_filter=True)
        .filter(TranscriptRow.session_id == session_id)
        .order_by(TranscriptRow.timestamp.asc())
        .all()
    )
    transcript = [
        {
            "id": t.id,
            "role": t.role,
            "text": t.text,
            "timestamp": _to_iso(t.timestamp),
            "tool_name": t.tool_name,
            "tool_args": t.tool_args,
            "tool_result": t.tool_result,
        }
        for t in turns
    ]

    # ── bookings created during the session ──
    bookings = (
        db.query(BookingRow)
        .execution_options(skip_tenant_filter=True)
        .filter(BookingRow.session_id == session_id)
        .order_by(BookingRow.created_at.asc())
        .all()
    )
    bookings_data = [
        {
            "id": b.id,
            "tenant_id": b.tenant_id,
            "caller_name": b.caller_name,
            "phone": b.phone,
            "service": b.service,
            "scheduled_for": _to_iso(b.scheduled_for),
            "duration_minutes": b.duration_minutes,
            "status": b.status,
            "notes": b.notes,
            "created_at": _to_iso(b.created_at),
        }
        for b in bookings
    ]

    # ── raw call_events (STT/LLM/TTS markers + errors) ──
    events = _load_call_events(call_id)

    # ── outcome: exists if we found ANY signal, else 404 ──
    if session_data is None and not transcript and not events:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no signal for call_id={call_id!r}. Checked sessions "
                f"({session_id!r}), transcript, bookings, and call_events."
            ),
        )

    return {
        "call_id": call_id,
        "session_id": session_id,
        "session": session_data,
        "transcript_turns": len(transcript),
        "transcript": transcript,
        "booking_count": len(bookings_data),
        "bookings": bookings_data,
        "event_count": len(events),
        "events": events,
    }


@router.get("/{call_id}/summary")
def get_incident_summary(
    call_id: str,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    """Same call but stripped of noisy per-frame events. What a
    support-eng opens first when the caller says 'the agent said the
    wrong date' — 30 seconds to a hypothesis instead of 30 minutes."""
    full = get_incident(call_id, request, db)
    full["events"] = _classify_summary_kinds(full["events"])
    full["event_count"] = len(full["events"])
    full["_summary_mode"] = True
    return full
