"""GET /trace/{call_id} — business-owner humanness trace view tests.

Contract:
  1. Auth: requires a valid tenant bearer (dashboard-style resolver).
  2. Tenant scoping: a tenant can only see its own calls; a call
     belonging to a different tenant returns 404 (not 403 — we don't
     want to leak existence via response code).
  3. Response shape: JSON payload has session / transcript / bookings
     / events / counts fields.
  4. Humanness projection: raw ServiceResolutionEvent / PolicyDecision
     etc. get labels + severity + human-readable insight fields.
  5. HTML render: default format ships a self-contained HTML doc with
     the transcript + timeline visible.
  6. JSON format: f=json returns application/json.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient


TENANT_ID = "trace-test-tenant"
TENANT_TOKEN = "trace-test-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    # Auth enforced: middleware needs a real bearer for /trace/*.
    monkeypatch.setenv("API_AUTH_ENFORCE", "true")
    monkeypatch.setenv(
        "API_KEYS_JSON", f'{{"{TENANT_TOKEN}": "{TENANT_ID}"}}',
    )
    monkeypatch.setenv(
        "CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"),
    )
    from packages.observability import call_event_log as _cel
    _cel.reset_singleton_for_tests()
    yield
    _cel.reset_singleton_for_tests()


def _client():
    from app.main import create_app
    return TestClient(create_app())


def _hdr(token: str = TENANT_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _seed_call(
    db,
    call_id: str,
    tenant_id: str = TENANT_ID,
    with_humanness: bool = True,
):
    """Create a session + transcript + booking + a couple of humanness
    events for the given call.  Returns the session_id."""
    from app.db import BookingRow, SessionRow, TranscriptRow
    from app.db.session import set_current_tenant, reset_current_tenant

    session_id = f"twilio_{call_id}"
    tok = set_current_tenant(tenant_id)
    try:
        db.add(SessionRow(
            id=session_id, tenant_id=tenant_id,
            business_id="test-biz", status="active",
            started_at=datetime.now(timezone.utc),
        ))
        db.flush()
        db.add(TranscriptRow(
            tenant_id=tenant_id, session_id=session_id,
            role="user", text="I need a follow-up appointment.",
            timestamp=datetime.now(timezone.utc),
        ))
        db.add(TranscriptRow(
            tenant_id=tenant_id, session_id=session_id,
            role="assistant",
            text="Sure — let me check availability.",
            timestamp=datetime.now(timezone.utc),
        ))
        db.add(BookingRow(
            id=f"bk_{uuid.uuid4().hex[:8]}", tenant_id=tenant_id,
            session_id=session_id, business_id="test-biz",
            caller_name="Test Caller", phone="+15551234567",
            service="Follow-up visit",
            scheduled_for=datetime.now(timezone.utc),
            duration_minutes=30, status="confirmed",
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        reset_current_tenant(tok)

    if with_humanness:
        from packages.observability.humanness_events import (
            ServiceResolutionEvent, PolicyDecisionEvent,
            BargeInDetectedEvent, TransferAttemptEvent,
            emit_humanness_event,
        )
        emit_humanness_event(ServiceResolutionEvent(
            call_id=call_id, tenant_id=tenant_id, session_id=session_id,
            spoken="A follow-up", kind="match_exact",
            canonical_name="Follow-up visit", confidence=0.95,
            reason="alias match",
        ))
        emit_humanness_event(PolicyDecisionEvent(
            call_id=call_id, tenant_id=tenant_id, session_id=session_id,
            action="confirm_action", acknowledgment="ack_understood",
            delivery_intent="warm", max_tokens=40,
        ))
        emit_humanness_event(BargeInDetectedEvent(
            call_id=call_id, tenant_id=tenant_id, session_id=session_id,
            kind="min_words_not_met",
            word_count=1, min_words_required=2,
        ))
        emit_humanness_event(TransferAttemptEvent(
            call_id=call_id, tenant_id=tenant_id, session_id=session_id,
            mode="warm", destination_label="Dr. Chen",
            reason="complaint", outcome="bridged",
        ))
    return session_id


# ── auth ────────────────────────────────────────────────────────────


def test_trace_requires_bearer():
    with _client() as c:
        r = c.get("/trace/CAdoesnotexist")
    assert r.status_code == 401


def test_trace_rejects_unknown_bearer():
    with _client() as c:
        r = c.get(
            "/trace/CAdoesnotexist",
            headers={"Authorization": "Bearer nope"},
        )
    assert r.status_code == 401


# ── missing call ────────────────────────────────────────────────


def test_trace_404_when_call_absent():
    with _client() as c:
        r = c.get(
            "/trace/CAnonexistent",
            headers=_hdr(), params={"f": "json"},
        )
    assert r.status_code == 404


# ── tenant scoping ─────────────────────────────────────────────


def test_trace_404_when_call_belongs_to_other_tenant(monkeypatch):
    """A tenant probing another tenant's call_id must get 404, not
    200 with the data and not 403 (which would leak existence)."""
    from app.db.session import SessionLocal

    call_id = f"CAother{uuid.uuid4().hex[:12]}"
    other_tenant = "other-tenant"
    # Register a second key so we can seed a call for that tenant.
    monkeypatch.setenv(
        "API_KEYS_JSON",
        f'{{"{TENANT_TOKEN}": "{TENANT_ID}", '
        f'"other-token": "{other_tenant}"}}',
    )

    with SessionLocal() as db:
        _seed_call(db, call_id, tenant_id=other_tenant,
                    with_humanness=False)

    # Query with OUR token — must 404 even though the call exists.
    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    assert r.status_code == 404


# ── happy path shape ───────────────────────────────────────────


def test_trace_json_returns_full_shape():
    from app.db.session import SessionLocal

    call_id = f"CAhappy{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["call_id"] == call_id
    assert body["tenant_id"] == TENANT_ID
    assert body["session"]["status"] == "active"
    assert body["counts"]["turns"] == 2
    assert body["counts"]["bookings"] == 1
    # Humanness events are present.
    assert body["counts"]["humanness_events"] >= 4


# ── humanness projection ─────────────────────────────────────


def test_trace_projects_service_resolution_event():
    from app.db.session import SessionLocal

    call_id = f"CAsvc{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    body = r.json()
    svc_rows = [
        e for e in body["events"] if e["kind"] == "service_resolution"
    ]
    assert svc_rows
    row = svc_rows[0]
    assert row["label"] == "Service name canonicalized"
    assert "A follow-up" in row["insight"]
    assert "Follow-up visit" in row["insight"]
    assert row["severity"] == "info"


def test_trace_marks_ambiguous_service_as_warn():
    """Ambiguous/unknown service resolution should render as WARN
    severity so business owners spot the class of failure quickly."""
    from app.db.session import SessionLocal
    from packages.observability.humanness_events import (
        ServiceResolutionEvent, emit_humanness_event,
    )

    call_id = f"CAamb{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, with_humanness=False)
    emit_humanness_event(ServiceResolutionEvent(
        call_id=call_id, tenant_id=TENANT_ID, session_id=f"twilio_{call_id}",
        spoken="consultation", kind="ambiguous",
        candidates=["Invisalign consultation", "Implant consultation"],
        confidence=0.7,
    ))

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    body = r.json()
    svc_rows = [
        e for e in body["events"] if e["kind"] == "service_resolution"
    ]
    assert svc_rows
    assert svc_rows[0]["severity"] == "warn"
    assert "Invisalign" in svc_rows[0]["insight"]


def test_trace_marks_failed_transfer_as_error():
    """A failed transfer must render as ERROR severity — owners
    absolutely need to notice these."""
    from app.db.session import SessionLocal
    from packages.observability.humanness_events import (
        TransferAttemptEvent, emit_humanness_event,
    )

    call_id = f"CAfail{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, with_humanness=False)
    emit_humanness_event(TransferAttemptEvent(
        call_id=call_id, tenant_id=TENANT_ID,
        session_id=f"twilio_{call_id}",
        mode="warm", destination_label="Dr. Chen",
        reason="complaint", outcome="failed",
        failure_detail="dial timeout",
    ))

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    body = r.json()
    tx_rows = [
        e for e in body["events"] if e["kind"] == "transfer_attempt"
    ]
    assert tx_rows
    assert tx_rows[0]["severity"] == "error"


def test_trace_marks_deterministic_fallback_as_error():
    """Empty-completion → deterministic fallback is the last-resort
    branch — every fire is a red flag."""
    from app.db.session import SessionLocal
    from packages.observability.humanness_events import (
        EmptyLlmDeterministicFallbackEvent, emit_humanness_event,
    )

    call_id = f"CAdet{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, with_humanness=False)
    emit_humanness_event(EmptyLlmDeterministicFallbackEvent(
        call_id=call_id, tenant_id=TENANT_ID,
        session_id=f"twilio_{call_id}",
        user_text="I need help",
        fallback_text="I'm sorry, could you say that again?",
    ))

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    body = r.json()
    fb_rows = [
        e for e in body["events"]
        if e["kind"] == "empty_llm_deterministic_fallback"
    ]
    assert fb_rows
    assert fb_rows[0]["severity"] == "error"


# ── format flag ─────────────────────────────────────────────


def test_trace_default_returns_html():
    from app.db.session import SessionLocal
    call_id = f"CAhtml{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        r = c.get(f"/trace/{call_id}", headers=_hdr())
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Call trace" in body
    assert call_id in body
    assert "Follow-up visit" in body
    assert "Humanness timeline" in body


def test_trace_html_escapes_transcript_content():
    """XSS guard — arbitrary caller/tool text in transcript must be
    HTML-escaped."""
    from app.db import TranscriptRow
    from app.db.session import (
        SessionLocal, set_current_tenant, reset_current_tenant,
    )

    call_id = f"CAxss{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, with_humanness=False)
        tok = set_current_tenant(TENANT_ID)
        try:
            db.add(TranscriptRow(
                tenant_id=TENANT_ID,
                session_id=f"twilio_{call_id}",
                role="user",
                text="<script>alert('xss')</script>",
                timestamp=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            reset_current_tenant(tok)

    with _client() as c:
        r = c.get(f"/trace/{call_id}", headers=_hdr())
    assert r.status_code == 200
    body = r.text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_trace_json_content_type():
    from app.db.session import SessionLocal
    call_id = f"CAjson{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id)

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    assert "application/json" in r.headers["content-type"]


# ── shape robustness ──────────────────────────────────────


def test_trace_survives_call_with_no_humanness_events():
    """Pre-humanness-schema calls must still render — the transcript
    + bookings alone are useful."""
    from app.db.session import SessionLocal

    call_id = f"CAnohuman{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        _seed_call(db, call_id, with_humanness=False)

    with _client() as c:
        r = c.get(
            f"/trace/{call_id}",
            headers=_hdr(), params={"f": "json"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["humanness_events"] == 0
    assert body["counts"]["turns"] == 2   # transcript still rendered
