"""Dashboard route tests.

Exercises the FastAPI app + a seeded tenant to verify:
- Auth via ?token= query param (browser demo path)
- Auth via Authorization: Bearer header (API path)
- Tenant isolation (tenant A cannot see tenant B's data)
- 404 on cross-tenant session_id lookup
- Renderer XSS safety (transcript text is escaped)

Uses the process's default engine (voiceops.db) since it's already
initialized at import time.  Each test seeds UUID-suffixed tenants +
sessions so nothing collides across runs.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    from app.main import create_app
    return TestClient(create_app())


def _plaintext_and_hash() -> tuple[str, str, str]:
    plaintext = "vk_" + secrets.token_urlsafe(24)
    return (
        plaintext,
        hashlib.sha256(plaintext.encode()).hexdigest(),
        plaintext[:12],
    )


def _seed_tenant(name: str = "Tenant A") -> tuple[str, str]:
    from app.db.session import engine
    from app.db import models
    from sqlalchemy.orm import Session as _S
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    plaintext, key_hash, key_prefix = _plaintext_and_hash()
    with _S(engine) as db:
        db.add(models.Tenant(id=tenant_id, name=name, plan="starter"))
        db.commit()
        db.add(models.ApiKey(
            tenant_id=tenant_id, key_hash=key_hash,
            key_prefix=key_prefix, name="test",
        ))
        db.commit()
    from app.middleware.auth import invalidate_key_cache
    invalidate_key_cache()
    return tenant_id, plaintext


def _seed_session(
    tenant_id: str,
    caller_name: str = "Sarah",
    with_booking: bool = True,
    with_transcript: bool = True,
    scheduled_delta_hours: int = 2,
    transcript_text_user: str = "I want to book a cleaning",
    extracted_override: dict | None = None,
) -> str:
    """Returns the seeded session_id."""
    from app.db.session import engine
    from app.db import models
    from sqlalchemy.orm import Session as _S
    session_id = f"sess-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    with _S(engine) as db:
        db.add(models.SessionRow(
            id=session_id, tenant_id=tenant_id, business_id="biz-1",
            status="completed", started_at=now - timedelta(minutes=5),
            ended_at=now - timedelta(minutes=4),
            extracted=extracted_override or {
                "caller_name": caller_name, "phone": "+15551234567",
                "intent": "book_appointment", "lead_score": 80,
            },
        ))
        db.commit()  # session must exist before FKs from transcript+booking resolve
        if with_transcript:
            db.add(models.TranscriptRow(
                tenant_id=tenant_id, session_id=session_id, role="user",
                text=transcript_text_user,
                timestamp=now - timedelta(minutes=4, seconds=30),
            ))
            db.add(models.TranscriptRow(
                tenant_id=tenant_id, session_id=session_id, role="assistant",
                text="Sure, when works for you?",
                timestamp=now - timedelta(minutes=4, seconds=15),
            ))
        if with_booking:
            db.add(models.BookingRow(
                id=f"book-{uuid.uuid4().hex[:8]}", tenant_id=tenant_id,
                session_id=session_id, business_id="biz-1",
                caller_name=caller_name, phone="+15551234567",
                service="cleaning",
                scheduled_for=now + timedelta(hours=scheduled_delta_hours),
                duration_minutes=45, status="confirmed",
            ))
        db.commit()
    return session_id


# ── auth ────────────────────────────────────────────────────────


def test_dashboard_requires_auth(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 401


def test_dashboard_rejects_invalid_token(client):
    resp = client.get("/dashboard/?token=nope")
    assert resp.status_code == 401


def test_dashboard_accepts_query_token(client):
    tid, key = _seed_tenant()
    resp = client.get(f"/dashboard/?token={key}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert tid in resp.text


def test_dashboard_accepts_bearer_header(client):
    tid, key = _seed_tenant()
    resp = client.get(
        "/dashboard/",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


# ── tenant isolation ────────────────────────────────────────────


def test_dashboard_shows_only_own_bookings(client):
    tid_a, key_a = _seed_tenant("A")
    tid_b, key_b = _seed_tenant("B")
    _seed_session(tid_a, caller_name="Alicevoiceops",
                  scheduled_delta_hours=1)
    _seed_session(tid_b, caller_name="Bobvoiceops",
                  scheduled_delta_hours=1)
    resp = client.get(f"/dashboard/?token={key_a}")
    assert resp.status_code == 200
    assert "Alicevoiceops" in resp.text
    assert "Bobvoiceops" not in resp.text


def test_dashboard_cross_tenant_call_lookup_404s(client):
    tid_a, key_a = _seed_tenant("A")
    tid_b, key_b = _seed_tenant("B")
    b_session_id = _seed_session(tid_b, caller_name="Bob")
    resp = client.get(f"/dashboard/calls/{b_session_id}?token={key_a}")
    assert resp.status_code == 404


# ── content ─────────────────────────────────────────────────────


def test_recent_calls_lists_seeded_sessions(client):
    tid, key = _seed_tenant()
    _seed_session(tid, caller_name="Sarahvoiceops")
    resp = client.get(f"/dashboard/calls?token={key}")
    assert resp.status_code == 200
    assert "Sarahvoiceops" in resp.text
    assert "Open" in resp.text


def test_transcript_page_renders_turns(client):
    tid, key = _seed_tenant()
    session_id = _seed_session(tid)
    resp = client.get(f"/dashboard/calls/{session_id}?token={key}")
    assert resp.status_code == 200
    assert "I want to book a cleaning" in resp.text
    assert "Sure, when works for you?" in resp.text


def test_missed_calls_section_present(client):
    tid, key = _seed_tenant()
    _seed_session(tid, caller_name="Miavoiceops", with_booking=False)
    resp = client.get(f"/dashboard/?token={key}")
    assert "Miavoiceops" in resp.text
    assert "Missed calls" in resp.text


# ── XSS safety ──────────────────────────────────────────────────


def test_transcript_text_is_html_escaped(client):
    tid, key = _seed_tenant()
    session_id = _seed_session(
        tid, transcript_text_user="<script>alert('xss')</script>",
    )
    resp = client.get(f"/dashboard/calls/{session_id}?token={key}")
    assert resp.status_code == 200
    assert "<script>alert('xss')</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_extracted_field_escaping(client):
    """Extracted caller_name is user-controlled; must escape in headers too."""
    tid, key = _seed_tenant()
    session_id = _seed_session(
        tid,
        extracted_override={
            "caller_name": "<img src=x onerror=alert(1)>",
            "phone": "+15551234567", "intent": "book_appointment",
        },
    )
    resp = client.get(f"/dashboard/calls/{session_id}?token={key}")
    assert resp.status_code == 200
    assert "<img src=x onerror" not in resp.text
    assert "&lt;img" in resp.text


# ── all-bookings view ───────────────────────────────────────────


def test_all_bookings_view_lists_recent(client):
    tid, key = _seed_tenant()
    _seed_session(tid, caller_name="Alicevoiceops")
    resp = client.get(f"/dashboard/bookings?token={key}")
    assert resp.status_code == 200
    assert "Alicevoiceops" in resp.text
    assert "cleaning" in resp.text


def test_all_bookings_view_respects_window(client):
    tid, key = _seed_tenant()
    _seed_session(tid, caller_name="Bob")
    resp = client.get(f"/dashboard/bookings?days=1&token={key}")
    assert resp.status_code == 200


# ── security: query-token guard (2026-08-25 security review) ────


def _make_client(monkeypatch, allow_token: bool = True,
                  environment: str = "development") -> TestClient:
    """Build a TestClient with a specific token-in-URL policy."""
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("ENVIRONMENT", environment)
    # Force fresh Settings so the flag picks up.
    from app.core import config as _cfg
    from app.core.config import Settings
    fresh = Settings()
    fresh.dashboard_allow_token_in_url = allow_token
    monkeypatch.setattr(_cfg, "settings", fresh)
    from app.routes import dashboard as _dash_mod
    # The route imports settings inside the function so no monkeypatch
    # of module-level captured settings is needed.
    from app.main import create_app
    return TestClient(create_app())


def test_dashboard_query_token_blocked_when_flag_off(monkeypatch):
    """When dashboard_allow_token_in_url=False in dev, ?token= must 401."""
    client = _make_client(monkeypatch, allow_token=False,
                          environment="development")
    tid, key = _seed_tenant()
    resp = client.get(f"/dashboard/?token={key}")
    assert resp.status_code == 401
    assert "query-string tokens disabled" in resp.text


def test_dashboard_bearer_still_works_when_query_disabled(monkeypatch):
    """Even with ?token= disabled, Authorization: Bearer must work."""
    client = _make_client(monkeypatch, allow_token=False,
                          environment="development")
    tid, key = _seed_tenant()
    resp = client.get(
        "/dashboard/",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


def test_dashboard_production_forces_query_off(monkeypatch):
    """Production ignores allow_flag=True — query tokens ALWAYS blocked
    on prod hosts.  This is the load-bearing "even if a tenant flips the
    flag, production is still protected" guarantee.

    We can't fully boot the app under ENVIRONMENT=production without
    alembic (init_db refuses).  Instead we directly test the guard
    decision by monkey-patching the env at request time via a mock
    Request.
    """
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from app.routes.dashboard import _resolve_dashboard_tenant

    # Craft a request with ?token=stolen-in-a-URL and no Bearer header.
    req = MagicMock()
    req.headers = {}
    req.query_params = {"token": "stolen-anywhere"}
    req.client = None

    # Force env=production + flag=True (i.e. tenant flipped it on but
    # production must still refuse).
    monkeypatch.setenv("ENVIRONMENT", "production")
    from app.core import config as _cfg
    from app.core.config import Settings
    fresh = Settings()
    fresh.dashboard_allow_token_in_url = True
    monkeypatch.setattr(_cfg, "settings", fresh)

    with pytest.raises(HTTPException) as exc:
        _resolve_dashboard_tenant(req)
    assert exc.value.status_code == 401
    assert "query-string tokens disabled" in str(exc.value.detail)


def test_dashboard_production_bearer_works(monkeypatch):
    """In production, Bearer auth is the only path — must still resolve
    when the header carries a valid key."""
    # Same direct-call approach to avoid the alembic dep.
    from unittest.mock import MagicMock
    from app.routes.dashboard import _resolve_dashboard_tenant

    # Boot a client to seed a tenant + get a real key.
    client = _make_client(monkeypatch, allow_token=True,
                          environment="development")
    tid, key = _seed_tenant()

    req = MagicMock()
    req.headers = {"authorization": f"Bearer {key}"}
    req.query_params = {}
    req.client = None

    # Now flip env → production.
    monkeypatch.setenv("ENVIRONMENT", "production")
    resolved = _resolve_dashboard_tenant(req)
    assert resolved == tid


def test_dashboard_missing_auth_returns_401_with_hint(monkeypatch):
    """No Bearer, no query token → 401 with actionable message."""
    client = _make_client(monkeypatch, allow_token=True,
                          environment="development")
    resp = client.get("/dashboard/")
    assert resp.status_code == 401
    assert "Bearer" in resp.text
