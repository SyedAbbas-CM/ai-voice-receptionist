"""Multi-tenancy tests — Sprint 6g.

Coverage:
  * Auto-inject: rows created inside `set_current_tenant("X")` get tenant_id X.
  * Cross-tenant leak guard: a query against tenant-scoped tables without a
    tenant filter raises CrossTenantLeakError.
  * Idempotency: repeated (tenant, key, scope) returns the cached response.
  * Admin flow: /admin/tenants + /admin/tenants/{id}/api-keys +
    /admin/tenants/{id}/businesses provisions atomically.
  * Cross-tenant fuzz: tenant A cannot fetch tenant B's session by ID.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")  # widget/simulator paths
    yield


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_session():
    """Fresh DB session for a test — rolls back on exit."""
    from app.db.session import SessionLocal, init_db
    init_db()  # ensure tables exist (SQLite in-memory test path may need this)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def two_tenants(db_session):
    """Provision two tenants + a session each so cross-tenant tests have data."""
    from app.db import SessionRow, Tenant
    from app.db.session import set_current_tenant, reset_current_tenant

    # Create tenants (bypass guard — tenants table is global)
    t_a = Tenant(id=f"tenant-a-{uuid.uuid4().hex[:8]}", name="Tenant A")
    t_b = Tenant(id=f"tenant-b-{uuid.uuid4().hex[:8]}", name="Tenant B")
    db_session.add_all([t_a, t_b])
    db_session.commit()

    # Create one session per tenant (using contextvar so auto-inject fires)
    for t in (t_a, t_b):
        token = set_current_tenant(t.id)
        try:
            row = SessionRow(
                id=f"sess-{t.id}", business_id="biz-x", status="active",
            )
            db_session.add(row)
            db_session.commit()
        finally:
            reset_current_tenant(token)

    return t_a, t_b


# ─── Auto-inject listener ────────────────────────────────────────────────────

def test_auto_inject_stamps_tenant_on_insert(db_session):
    from app.db import SessionRow
    from app.db.session import set_current_tenant, reset_current_tenant

    token = set_current_tenant("acme-corp")
    try:
        row = SessionRow(id=f"s-{uuid.uuid4().hex[:8]}", business_id="biz-1")
        db_session.add(row)
        db_session.commit()
        assert row.tenant_id == "acme-corp"
    finally:
        reset_current_tenant(token)


def test_auto_inject_no_op_when_context_unset(db_session):
    from app.db import SessionRow

    # No contextvar → tenant_id stays whatever the model default is (None)
    row = SessionRow(id=f"s-{uuid.uuid4().hex[:8]}", business_id="biz-1")
    db_session.add(row)
    db_session.commit()
    assert row.tenant_id is None


def test_explicit_tenant_id_not_overwritten(db_session):
    from app.db import SessionRow
    from app.db.session import set_current_tenant, reset_current_tenant

    token = set_current_tenant("wrong-tenant")
    try:
        # Handler explicitly sets tenant_id — auto-inject shouldn't override
        row = SessionRow(id=f"s-{uuid.uuid4().hex[:8]}", business_id="biz-1",
                         tenant_id="explicit-tenant")
        db_session.add(row)
        db_session.commit()
        assert row.tenant_id == "explicit-tenant"
    finally:
        reset_current_tenant(token)


# ─── Cross-tenant leak guard ────────────────────────────────────────────────

def test_leak_guard_blocks_unfiltered_query(db_session, monkeypatch):
    """A raw SELECT on sessions with no tenant filter must raise."""
    from app.db import SessionRow
    from app.db.tenant_guard import CrossTenantLeakError

    monkeypatch.setenv("TENANT_GUARD_ENFORCE", "true")
    with pytest.raises(CrossTenantLeakError):
        # This query has no tenant_id filter — guard raises before execution
        db_session.query(SessionRow).all()


def test_leak_guard_allows_filtered_query(db_session, two_tenants):
    """SELECT with a tenant_id filter is fine."""
    from app.db import SessionRow

    t_a, _ = two_tenants
    # Filtered query — passes the guard
    rows = db_session.query(SessionRow).filter(SessionRow.tenant_id == t_a.id).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == t_a.id


def test_leak_guard_disabled_via_env(db_session, monkeypatch):
    """When TENANT_GUARD_ENFORCE=false, unfiltered queries succeed (dev mode)."""
    from app.db import SessionRow

    monkeypatch.setenv("TENANT_GUARD_ENFORCE", "false")
    # Should not raise
    _ = db_session.query(SessionRow).limit(1).all()


# ─── Idempotency ────────────────────────────────────────────────────────────

def test_idempotency_records_and_replays():
    from app.db.idempotency import (
        check_or_reserve_webhook_event,
        record_webhook_result,
    )
    import asyncio

    tenant = "acme-corp"
    scope = "webhook:vapi"
    event_id = f"call-{uuid.uuid4().hex[:12]}"

    # First look: miss
    cached = asyncio.run(check_or_reserve_webhook_event(tenant, scope, event_id))
    assert cached is None

    # Record the response
    record_webhook_result(tenant, scope, event_id, 200, {"ok": True, "processed": "first"})

    # Second look: hit
    cached = asyncio.run(check_or_reserve_webhook_event(tenant, scope, event_id))
    assert cached is not None
    assert cached["replay"] is True
    assert cached["body"] == {"ok": True, "processed": "first"}


def test_idempotency_isolated_per_tenant():
    """Same event_id under two tenants doesn't collide."""
    from app.db.idempotency import (
        check_or_reserve_webhook_event,
        record_webhook_result,
    )
    import asyncio

    event_id = f"call-{uuid.uuid4().hex[:12]}"
    record_webhook_result("tenant-x", "webhook:vapi", event_id, 200, {"who": "x"})

    cached = asyncio.run(check_or_reserve_webhook_event("tenant-y", "webhook:vapi", event_id))
    assert cached is None, "tenant-y should not see tenant-x's idempotency record"

    cached = asyncio.run(check_or_reserve_webhook_event("tenant-x", "webhook:vapi", event_id))
    assert cached is not None and cached["body"] == {"who": "x"}


# ─── Admin flow (integration) ────────────────────────────────────────────────

def test_admin_provisioning_flow():
    """POST /admin/tenants -> POST /api-keys -> POST /businesses."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app())
    admin_headers = {"Authorization": "Bearer test-admin-token"}
    tenant_id = f"acme-{uuid.uuid4().hex[:8]}"

    # 1. Create tenant
    r = client.post(
        "/admin/tenants",
        headers=admin_headers,
        json={"id": tenant_id, "name": "Acme Dental Group", "plan": "pro"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == tenant_id
    assert body["plan"] == "pro"

    # 2. Issue API key
    r = client.post(
        f"/admin/tenants/{tenant_id}/api-keys",
        headers=admin_headers,
        json={"name": "primary"},
    )
    assert r.status_code == 200, r.text
    key_body = r.json()
    assert key_body["key"].startswith("vk_")
    assert key_body["prefix"] == key_body["key"][:12]

    # 3. Provision business profile
    r = client.post(
        f"/admin/tenants/{tenant_id}/businesses",
        headers=admin_headers,
        json={"profile": {
            "id": "smile-dental-1",
            "name": "Smile Dental Clinic",
            "vertical": "clinic",
        }},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"business_id": "smile-dental-1", "tenant_id": tenant_id}

    # 4. Verify GET returns the provisioned business
    r = client.get(f"/admin/tenants/{tenant_id}", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Acme Dental Group"
    assert any(b["id"] == "smile-dental-1" for b in data["businesses"])


def test_admin_requires_token():
    """Without ADMIN_TOKEN header → 401."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app())
    # Send a schema-valid body so Pydantic doesn't 422 before auth runs
    valid = {"id": f"tenant-{uuid.uuid4().hex[:6]}", "name": "X"}
    r = client.post("/admin/tenants", json=valid)
    assert r.status_code == 401, r.text


def test_admin_bad_token_rejected():
    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app())
    valid = {"id": f"tenant-{uuid.uuid4().hex[:6]}", "name": "X"}
    r = client.post(
        "/admin/tenants",
        headers={"Authorization": "Bearer wrong-token"},
        json=valid,
    )
    assert r.status_code == 401, r.text


def test_admin_disabled_when_token_missing(monkeypatch):
    """No ADMIN_TOKEN env → 503 (route explicitly disabled)."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client = TestClient(create_app())
    valid = {"id": f"tenant-{uuid.uuid4().hex[:6]}", "name": "X"}
    r = client.post(
        "/admin/tenants",
        headers={"Authorization": "Bearer anything"},
        json=valid,
    )
    assert r.status_code == 503, r.text


# ─── Cross-tenant fuzz ──────────────────────────────────────────────────────

def test_db_backed_api_key_end_to_end(monkeypatch):
    """Create tenant + issue key via /admin, then use that key on a
    protected route.  Proves the DB-backed auth lookup works."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.middleware.auth import invalidate_key_cache

    invalidate_key_cache()
    monkeypatch.setenv("API_AUTH_ENFORCE", "true")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("API_KEYS_JSON", raising=False)

    client = TestClient(create_app())
    admin = {"Authorization": "Bearer test-admin-token"}
    tid = f"dbkey-{uuid.uuid4().hex[:8]}"

    # Create tenant + issue key
    assert client.post("/admin/tenants", headers=admin,
                       json={"id": tid, "name": "DB Key Test"}).status_code == 200
    r = client.post(f"/admin/tenants/{tid}/api-keys", headers=admin, json={"name": "primary"})
    assert r.status_code == 200
    plaintext = r.json()["key"]

    # Use the plaintext key on a tenant-scoped route.  /sessions returns [] for
    # a fresh tenant but the important thing is a 200, not a 401.
    r = client.get("/sessions", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 200, r.text

    # Wrong key rejected
    r = client.get("/sessions", headers={"Authorization": "Bearer vk_nonsense"})
    assert r.status_code == 401


def test_tenant_a_cannot_fetch_tenant_b_session(two_tenants):
    """The core cross-tenant leak scenario: tenant A auths, tries to GET
    tenant B's session by its exact ID.  Must 404."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    t_a, t_b = two_tenants

    # Reconfigure so auth is enforced with a per-tenant key mapping
    os.environ["API_AUTH_ENFORCE"] = "true"
    os.environ["API_KEYS_JSON"] = f'{{"key-a": "{t_a.id}", "key-b": "{t_b.id}"}}'
    try:
        client = TestClient(create_app())
        # Tenant A tries to see tenant B's session
        r = client.get(
            f"/sessions/sess-{t_b.id}",
            headers={"Authorization": "Bearer key-a"},
        )
        assert r.status_code == 404, f"cross-tenant leak: {r.status_code} {r.text}"
    finally:
        os.environ.pop("API_KEYS_JSON", None)
        os.environ["API_AUTH_ENFORCE"] = "false"
