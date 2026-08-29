"""P1 task #77 acceptance — /admin/calls/{call_id}/incident.

Read-only aggregator. Tests:
  1. 404 on unknown call_id (no session, no transcript, no events)
  2. Admin token required (401 without, 200 with)
  3. Both raw CA-SID and prefixed twilio_ CA-SID resolve
  4. Returns session + transcript + bookings + events shape
  5. Summary endpoint strips per-frame noise, keeps turn boundaries
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-p1-incident"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    # Reset the singleton so the per-test tmp_path db is used
    from packages.observability import call_event_log as _cel
    _cel.reset_singleton_for_tests()
    yield
    _cel.reset_singleton_for_tests()


def _client():
    from app.main import create_app
    return TestClient(create_app())


def _hdr():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ─── 1. Auth ────────────────────────────────────────────────────────────────


def test_incident_requires_admin_token():
    with _client() as c:
        r = c.get("/admin/calls/CAdoesnotexist/incident")
    assert r.status_code == 401, (
        "P1 REGRESSION: /admin/calls/*/incident is unauthenticated. "
        "This exposes cross-tenant transcript + booking data."
    )


def test_incident_rejects_wrong_admin_token():
    with _client() as c:
        r = c.get(
            "/admin/calls/CAxyz/incident",
            headers={"Authorization": "Bearer wrong"},
        )
    assert r.status_code == 401


# ─── 2. Missing call ────────────────────────────────────────────────────────


def test_incident_404_on_unknown_call_id():
    with _client() as c:
        r = c.get("/admin/calls/CAnothing_here/incident", headers=_hdr())
    assert r.status_code == 404
    assert "no signal" in r.json()["detail"].lower()


# ─── 3. Real call: session + transcript + bookings + events ─────────────────


def _seed_call(db, call_id: str, tenant_id: str = "clinic") -> str:
    """Create a session + a transcript turn + a booking + an event.
    Returns the session_id."""
    from app.db import BookingRow, SessionRow, TranscriptRow
    from app.db.session import set_current_tenant, reset_current_tenant

    session_id = f"twilio_{call_id}"
    tok = set_current_tenant(tenant_id)
    try:
        # Insert session FIRST + flush so FK targets exist before dependent
        # rows are added. Otherwise sqlite raises FOREIGN KEY constraint
        # failed on the booking insert.
        db.add(SessionRow(
            id=session_id,
            tenant_id=tenant_id,
            business_id="clinic-main",
            status="active",
            started_at=datetime.now(timezone.utc),
        ))
        db.flush()
        db.add(TranscriptRow(
            tenant_id=tenant_id,
            session_id=session_id,
            role="user",
            text="I want to book an appointment",
            timestamp=datetime.now(timezone.utc),
        ))
        db.add(BookingRow(
            id=f"bk_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            session_id=session_id,
            business_id="clinic-main",
            caller_name="Test Caller",
            phone="+15551234567",
            service="cleaning",
            scheduled_for=datetime.now(timezone.utc),
            duration_minutes=30,
            status="confirmed",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        reset_current_tenant(tok)

    # Also emit a call_event so the events array isn't empty
    from packages.observability.call_event_log import (
        CallEvent, EventSourceKind, get_call_event_log,
    )
    get_call_event_log().write(CallEvent(
        call_id=call_id,
        tenant_id=tenant_id,
        source=EventSourceKind.STT,
        kind="STT_FINAL",
        payload={"text": "I want to book an appointment"},
    ))
    return session_id


def test_incident_returns_full_shape():
    from app.db.session import SessionLocal

    call_id = f"CAtest{uuid.uuid4().hex[:16]}"
    with SessionLocal() as db:
        session_id = _seed_call(db, call_id)

    with _client() as c:
        r = c.get(f"/admin/calls/{call_id}/incident", headers=_hdr())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["call_id"] == call_id
    assert body["session_id"] == session_id
    assert body["session"] is not None
    assert body["session"]["tenant_id"] == "clinic"
    assert body["transcript_turns"] == 1
    assert body["transcript"][0]["role"] == "user"
    assert body["booking_count"] == 1
    assert body["bookings"][0]["phone"] == "+15551234567"
    assert body["event_count"] >= 1
    assert any(e["kind"] == "STT_FINAL" for e in body["events"])


def test_incident_accepts_both_ca_sid_forms():
    """Support engineers paste either 'CAxxx' or 'twilio_CAxxx' —
    both must resolve to the same session."""
    from app.db.session import SessionLocal

    call_id = f"CAform{uuid.uuid4().hex[:16]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        raw = c.get(f"/admin/calls/{call_id}/incident", headers=_hdr())
        prefixed = c.get(f"/admin/calls/twilio_{call_id}/incident", headers=_hdr())

    assert raw.status_code == 200
    assert prefixed.status_code == 200
    # Both point at the same session_id
    assert raw.json()["session_id"] == prefixed.json()["session_id"]


# ─── 4. Summary endpoint filters noise ──────────────────────────────────────


def test_summary_strips_stt_partial_and_frame_noise():
    """The summary view should drop per-frame markers but keep STT_FINAL,
    LLM_STREAM_START/DONE, tool calls, and errors."""
    from app.db.session import SessionLocal
    from packages.observability.call_event_log import (
        CallEvent, EventSourceKind, get_call_event_log,
    )

    call_id = f"CAsum{uuid.uuid4().hex[:16]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    log = get_call_event_log()
    # Add some noise + some signal
    for kind in ("STT_PARTIAL", "STT_PARTIAL", "STT_PARTIAL"):  # noise
        log.write(CallEvent(
            call_id=call_id, tenant_id="clinic",
            source=EventSourceKind.STT, kind=kind, payload={},
        ))
    log.write(CallEvent(
        call_id=call_id, tenant_id="clinic",
        source=EventSourceKind.LLM, kind="LLM_STREAM_START", payload={},
    ))
    log.write(CallEvent(
        call_id=call_id, tenant_id="clinic",
        source=EventSourceKind.LLM, kind="TOOL_CALL",
        payload={"tool": "book_appointment"},
    ))

    with _client() as c:
        full = c.get(f"/admin/calls/{call_id}/incident", headers=_hdr()).json()
        summary = c.get(f"/admin/calls/{call_id}/summary", headers=_hdr()).json()

    # Summary must have fewer events than full (noise dropped)
    assert summary["event_count"] < full["event_count"]
    # Signal-kind events survived
    kinds = [e["kind"] for e in summary["events"]]
    assert "STT_FINAL" in kinds  # from _seed_call
    assert "LLM_STREAM_START" in kinds
    assert "TOOL_CALL" in kinds
    # Noise dropped
    assert "STT_PARTIAL" not in kinds
    # Meta flag set
    assert summary["_summary_mode"] is True
