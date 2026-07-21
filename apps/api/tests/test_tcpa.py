"""TCPA compliance tests. Consent lookup + AI-disclosure + dialer_policy
integration. Any test here reflects a legal-exposure scenario we want to
prevent."""
from __future__ import annotations

from datetime import time as _time
from unittest.mock import AsyncMock

import pytest

from packages.compliance import (
    AlwaysConsentProvider,
    ConsentRecord,
    SqliteConsentProvider,
    build_consent_provider,
    build_disclosure_greeting,
    is_ai_disclosure_line,
)
from packages.integrations.dialer_policy import (
    DialerPolicy,
    Lead,
    decide_can_call_with_consent,
    filter_leads_with_consent,
)


# ---- consent record ----

def test_consent_record_current_when_granted_and_not_revoked():
    r = ConsentRecord(phone="+15551234567", consent_granted=True)
    assert r.is_current is True


def test_consent_record_not_current_when_revoked():
    from datetime import datetime
    r = ConsentRecord(
        phone="+15551234567", consent_granted=True,
        revoked_at=datetime.utcnow(),
    )
    assert r.is_current is False


# ---- sqlite consent provider ----

@pytest.mark.asyncio
async def test_sqlite_provider_returns_no_consent_by_default(tmp_path):
    p = SqliteConsentProvider(str(tmp_path / "consent.db"))
    r = await p.has_consent("+15551234567")
    assert r.consent_granted is False
    assert r.is_current is False


@pytest.mark.asyncio
async def test_sqlite_provider_records_and_reads_consent(tmp_path):
    p = SqliteConsentProvider(str(tmp_path / "consent.db"))
    await p.record_consent("+15551234567", source="web_form")
    r = await p.has_consent("+15551234567")
    assert r.consent_granted is True
    assert r.is_current is True
    assert r.source == "web_form"


@pytest.mark.asyncio
async def test_sqlite_provider_revokes_consent(tmp_path):
    p = SqliteConsentProvider(str(tmp_path / "consent.db"))
    await p.record_consent("+15551234567", source="web_form")
    await p.revoke("+15551234567")
    r = await p.has_consent("+15551234567")
    assert r.is_current is False


@pytest.mark.asyncio
async def test_sqlite_provider_normalizes_phone(tmp_path):
    """User records 15551234567; caller looks up (555) 123-4567. Should match."""
    p = SqliteConsentProvider(str(tmp_path / "consent.db"))
    await p.record_consent("15551234567", source="web_form")
    r = await p.has_consent("(555) 123-4567")
    # Both normalize to digits only (or +digits), so lookup finds record
    assert r.is_current is True


# ---- AI disclosure ----

@pytest.mark.parametrize("text", [
    "Hi, this is an AI assistant calling on behalf of Riverside Family Clinic.",
    "This is an AI receptionist for Osteria Verde.",
    "Hey, this is an automated voice assistant.",
    "I'm an AI voice agent — not a human.",
])
def test_disclosure_detects_ai_line(text):
    assert is_ai_disclosure_line(text) is True


@pytest.mark.parametrize("text", [
    "Hi, this is Alex from SubtoDealz.",
    "Hey, how can I help you?",
    "",
    "Just a moment.",
])
def test_disclosure_rejects_non_disclosed_line(text):
    assert is_ai_disclosure_line(text) is False


def test_build_disclosure_greeting_contains_business_name_and_ai_word():
    g = build_disclosure_greeting("Riverside Family Clinic", caller_name="Bob")
    assert "Bob" in g
    assert "Riverside Family Clinic" in g
    assert "AI" in g
    assert is_ai_disclosure_line(g) is True


def test_build_disclosure_greeting_without_name():
    g = build_disclosure_greeting("Osteria Verde")
    assert "Osteria Verde" in g
    assert is_ai_disclosure_line(g) is True


# ---- dialer_policy integration ----

FL_POLICY = DialerPolicy()  # defaults are Florida ET


@pytest.mark.asyncio
async def test_decide_with_no_consent_provider_matches_sync():
    """If consent_provider is None, behavior matches the sync decide_can_call."""
    from datetime import datetime, timezone

    lead = Lead(phone="+15551234567", name="Bob", total_calls=0)
    now_utc = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)  # Wed 10am ET
    d = await decide_can_call_with_consent(lead, FL_POLICY, None, now_utc=now_utc)
    assert d.can_call is True


@pytest.mark.asyncio
async def test_decide_blocks_when_no_consent_recorded(tmp_path):
    from datetime import datetime, timezone

    provider = SqliteConsentProvider(str(tmp_path / "consent.db"))
    lead = Lead(phone="+15551234567", name="Bob", total_calls=0)
    now_utc = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)

    d = await decide_can_call_with_consent(lead, FL_POLICY, provider, now_utc=now_utc)
    assert d.can_call is False
    assert d.reason == "no_consent"


@pytest.mark.asyncio
async def test_decide_passes_when_consent_recorded(tmp_path):
    from datetime import datetime, timezone

    provider = SqliteConsentProvider(str(tmp_path / "consent.db"))
    await provider.record_consent("+15551234567", source="web_form")
    lead = Lead(phone="+15551234567", name="Bob", total_calls=0)
    now_utc = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)

    d = await decide_can_call_with_consent(lead, FL_POLICY, provider, now_utc=now_utc)
    assert d.can_call is True


@pytest.mark.asyncio
async def test_always_provider_never_blocks():
    from datetime import datetime, timezone

    lead = Lead(phone="+15551234567", name="Bob", total_calls=0)
    now_utc = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    d = await decide_can_call_with_consent(lead, FL_POLICY, AlwaysConsentProvider(), now_utc=now_utc)
    assert d.can_call is True


@pytest.mark.asyncio
async def test_filter_leads_with_consent_partitions(tmp_path):
    from datetime import datetime, timezone

    provider = SqliteConsentProvider(str(tmp_path / "consent.db"))
    await provider.record_consent("+15550000001", source="web_form")
    # +15550000002 has no consent

    leads = [
        Lead(phone="+15550000001", total_calls=0),
        Lead(phone="+15550000002", total_calls=0),
    ]
    now_utc = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    dialable, skipped = await filter_leads_with_consent(leads, FL_POLICY, provider, now_utc=now_utc)

    assert len(dialable) == 1
    assert dialable[0].phone == "+15550000001"
    assert len(skipped) == 1
    _, decision = skipped[0]
    assert decision.reason == "no_consent"


def test_factory_kinds():
    assert build_consent_provider("always").name == "always"
    with pytest.raises(ValueError):
        build_consent_provider("magic")


@pytest.mark.asyncio
async def test_http_provider_fails_closed_on_error():
    """TCPA rule: any consent lookup error means NO consent, block the call.
    Never fail open on TCPA."""
    from packages.compliance.tcpa import HttpConsentProvider

    p = HttpConsentProvider(url="http://invalid-hostname-that-does-not-exist.example")
    r = await p.has_consent("+15551234567")
    assert r.is_current is False
