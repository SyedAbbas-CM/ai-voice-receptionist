"""P0.4 regression — resolve_tenant_from_phone must NEVER return a fallback
tenant unless the dev-fallback env flag is explicitly set.

BACKEND-AUDIT-2026-08-25-CHATGPT.md finding #4: the Twilio WSS handler
hardcoded `tenant_id="default"` on every inbound call. This module is what
replaces that hardcode — an empty or unmapped E.164 must fail-closed
(return None) so the WSS handler refuses the call, not silently routes it
to some catchall tenant.

Tests cover:
  1. Empty/whitespace phone → None
  2. Unmapped phone with dev-fallback OFF → None
  3. Unmapped phone with dev-fallback ON → warning + fallback route
  4. Mapped phone → correct (tenant_id, business_id)
  5. Revoked mapping → None (never route to a paused tenant)
  6. Cache TTL — hit within 60s, miss after
  7. invalidate_phone_cache() clears cache
"""
from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from app.telephony.tenant_from_phone import (
    TenantRoute,
    _reset_cache_for_tests,
    invalidate_phone_cache,
    resolve_tenant_from_phone,
)


@pytest.fixture(autouse=True)
def _cache_isolated(monkeypatch):
    """Every test starts with a clean cache and no dev-fallback env
    leaking from other tests."""
    _reset_cache_for_tests()
    monkeypatch.delenv("PHONE_ROUTING_ALLOW_DEV_FALLBACK", raising=False)
    monkeypatch.delenv("PHONE_ROUTING_DEV_FALLBACK_TENANT", raising=False)
    yield
    _reset_cache_for_tests()


# ─── 1. Empty phone ──────────────────────────────────────────────────────────


def test_empty_phone_returns_none():
    assert resolve_tenant_from_phone("") is None


def test_none_phone_returns_none():
    # Type-wise `phone_e164: str` but real inbound TwiML can send None
    # if the customParameter is missing. Must fail-closed, not crash.
    # Defensive test — the annotation says str but production must not
    # blow up on missing input.
    assert resolve_tenant_from_phone(None) is None  # type: ignore[arg-type]


# ─── 2/3. Unmapped phone — with/without dev fallback ────────────────────────


def _mock_db_row(row):
    """Patch the DB lookup path so tests don't need a real sqlite. The
    real lookup runs `SessionLocal().query(PhoneNumberMapping)...one_or_none()`
    — patch at the SessionLocal boundary."""
    fake_db = MagicMock()
    fake_db.__enter__ = MagicMock(return_value=fake_db)
    fake_db.__exit__ = MagicMock(return_value=None)
    fake_query = MagicMock()
    fake_query.execution_options.return_value = fake_query
    fake_query.filter.return_value = fake_query
    fake_query.one_or_none.return_value = row
    fake_db.query.return_value = fake_query
    return patch("app.db.session.SessionLocal", return_value=fake_db)


def test_unmapped_phone_without_dev_fallback_returns_none(monkeypatch):
    """The most important case — a call to an unrecognized number must NOT
    slide into any catchall. This is the exact bypass P0.4 is closing."""
    monkeypatch.delenv("PHONE_ROUTING_ALLOW_DEV_FALLBACK", raising=False)
    with _mock_db_row(None):
        result = resolve_tenant_from_phone("+15559999999")
    assert result is None, (
        "P0.4 REGRESSION: unmapped phone returned non-None without "
        "dev-fallback enabled — this reopens the supertenant bypass. "
        "Check tenant_from_phone.py._dev_fallback_enabled() logic."
    )


def test_unmapped_phone_with_dev_fallback_returns_fallback_route(monkeypatch):
    """Dev fallback is the intentional escape hatch. It exists so a dev
    running against a Twilio test subaccount can dial without seeding
    the mapping table. NEVER on in prod."""
    monkeypatch.setenv("PHONE_ROUTING_ALLOW_DEV_FALLBACK", "true")
    monkeypatch.setenv("PHONE_ROUTING_DEV_FALLBACK_TENANT", "demo-tenant")
    with _mock_db_row(None):
        result = resolve_tenant_from_phone("+15559999999")
    assert result == TenantRoute(tenant_id="demo-tenant", business_id=None)


def test_dev_fallback_case_insensitive_off_values(monkeypatch):
    """`"false"`, `"0"`, `"no"`, and missing all count as OFF. Guard
    against a typo like `"False"` silently keeping fallback OFF the
    way we intend."""
    for off_val in ("false", "False", "0", "no", "NO", ""):
        monkeypatch.setenv("PHONE_ROUTING_ALLOW_DEV_FALLBACK", off_val)
        _reset_cache_for_tests()
        with _mock_db_row(None):
            result = resolve_tenant_from_phone("+15559999999")
        assert result is None, (
            f"env value {off_val!r} incorrectly enabled dev fallback"
        )


# ─── 4. Mapped phone → correct route ────────────────────────────────────────


def test_mapped_phone_returns_tenant_and_business():
    """The happy path. A row exists → return its tenant_id and business_id."""
    row = MagicMock()
    row.tenant_id = "smile-dental-001"
    row.business_id = "smile-dental-plano"
    with _mock_db_row(row):
        result = resolve_tenant_from_phone("+19725550192")
    assert result == TenantRoute(
        tenant_id="smile-dental-001",
        business_id="smile-dental-plano",
    )


def test_mapped_phone_with_null_business_id_returns_tenant_only():
    """Multi-business tenants can leave business_id NULL — the tenant's
    own config decides business at brain-load time."""
    row = MagicMock()
    row.tenant_id = "ribeira-prime-001"
    row.business_id = None
    with _mock_db_row(row):
        result = resolve_tenant_from_phone("+351215550192")
    assert result.tenant_id == "ribeira-prime-001"
    assert result.business_id is None


# ─── 5. Revoked mappings ────────────────────────────────────────────────────


def test_revoked_mapping_is_treated_as_unmapped():
    """The resolver's DB filter is `revoked_at IS NULL`, so a revoked row
    doesn't come back. Verify the resulting behavior — same as unmapped."""
    # _mock_db_row(None) simulates "the WHERE filter matched nothing"
    # which is exactly what a revoked row would look like.
    with _mock_db_row(None):
        result = resolve_tenant_from_phone("+19725550192")
    assert result is None


# ─── 6. Cache behavior ──────────────────────────────────────────────────────


def test_cache_hit_within_ttl_avoids_db_roundtrip():
    """Second call within TTL must not touch the DB. Verify by patching
    SessionLocal to raise if invoked, then calling twice."""
    row = MagicMock()
    row.tenant_id = "acme"
    row.business_id = None

    with _mock_db_row(row) as db_mock:
        first = resolve_tenant_from_phone("+15551234567")
        db_mock.assert_called_once()

    # Second call — DB is no longer mocked; if the resolver went to the
    # real DB the test would either crash or hit sqlite. It should not.
    with patch("app.db.session.SessionLocal", side_effect=AssertionError(
        "P0.4 REGRESSION: cache hit path took a DB round-trip"
    )):
        second = resolve_tenant_from_phone("+15551234567")

    assert first == second == TenantRoute(tenant_id="acme", business_id=None)


def test_cache_expires_after_ttl(monkeypatch):
    """After 60s+ the entry expires — next call must re-hit DB. Fake
    time.time() by patching it inside the resolver's module namespace."""
    from app.telephony import tenant_from_phone as mod

    row1 = MagicMock(); row1.tenant_id = "old-tenant"; row1.business_id = None
    row2 = MagicMock(); row2.tenant_id = "new-tenant"; row2.business_id = None

    fake_now = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_now[0])

    with _mock_db_row(row1):
        first = resolve_tenant_from_phone("+15551234567")
    assert first.tenant_id == "old-tenant"

    # Advance past the TTL
    fake_now[0] = 1000.0 + 61.0
    with _mock_db_row(row2):
        second = resolve_tenant_from_phone("+15551234567")
    assert second.tenant_id == "new-tenant", (
        "cache didn't expire after TTL — a mapping rotation wouldn't be "
        "picked up within the intended 60s window"
    )


def test_invalidate_phone_cache_clears_cache():
    """Called by admin routes after a mapping mutation."""
    row1 = MagicMock(); row1.tenant_id = "old"; row1.business_id = None
    row2 = MagicMock(); row2.tenant_id = "new"; row2.business_id = None

    with _mock_db_row(row1):
        first = resolve_tenant_from_phone("+15551234567")
    assert first.tenant_id == "old"

    invalidate_phone_cache()

    with _mock_db_row(row2):
        second = resolve_tenant_from_phone("+15551234567")
    assert second.tenant_id == "new"


# ─── 7. Negative-result caching ─────────────────────────────────────────────


def test_negative_result_is_cached_to_defend_against_scanners():
    """A scanner dialing many unmapped numbers in a row should not cause
    a DB hit per call — negative results cache identically."""
    with _mock_db_row(None) as db_mock:
        r1 = resolve_tenant_from_phone("+15550000001")
        db_mock.assert_called_once()

    with patch("app.db.session.SessionLocal", side_effect=AssertionError(
        "negative cache didn't stick — DB was re-queried on second miss"
    )):
        r2 = resolve_tenant_from_phone("+15550000001")

    assert r1 is None and r2 is None


# ─── 8. DB error handling ───────────────────────────────────────────────────


def test_db_error_fails_closed_not_open():
    """If the DB is unreachable mid-call, the resolver must refuse the
    call, NOT fall back to any default tenant. Loud log + None return."""
    with patch(
        "app.db.session.SessionLocal",
        side_effect=RuntimeError("DB down"),
    ):
        result = resolve_tenant_from_phone("+15551234567")
    assert result is None, (
        "P0.4 REGRESSION: DB error path returned a route — this is a "
        "fail-open bug. On DB failure the call must be refused, never "
        "silently assigned to a default tenant."
    )
