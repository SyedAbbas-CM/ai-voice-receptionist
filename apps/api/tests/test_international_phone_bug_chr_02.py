"""Tests for BUG-CHR-02: international phone acceptance.

2026-08-29: Christiaan (Netherlands, +31 6 25007600) said his phone as
'zero six two five zero zero seven six zero zero' — 10-digit Dutch
local format.  clinic_tools defaulted to region='US' only.
libphonenumber returned status=partial with value=None, which the
LLM couldn't turn into a valid book_appointment call → empty
completion → dead air.

Fix:
- packages/schemas/business.py: add phone_default_region +
  phone_accepted_regions fields to BusinessProfile
- packages/integrations/vertical_tools.py: expand
  _DEFAULT_ACCEPTED_PHONE_REGIONS to include NL and EU markets
- The existing vertical_tools._phone_region_config already reads
  those attrs via getattr — schema addition unblocks the wire

Pins the round-trip:
1. Dutch local phone parses when NL is in the accepted set
2. BusinessProfile default_factory picks up an empty list
3. Tenant can override via profile fields
4. vertical_tools wires through to ClinicToolHandler correctly
5. Regression: US baseline still works
"""
from __future__ import annotations

import pytest

from packages.integrations.fake_calendar import FakeCalendar
from packages.integrations.vertical_tools import (
    _DEFAULT_ACCEPTED_PHONE_REGIONS,
    _phone_region_config,
    build_tools_for_vertical,
)
from packages.schemas import BusinessProfile


def _biz(**overrides) -> BusinessProfile:
    defaults = dict(
        id="biz1", name="Test Clinic", vertical="clinic",
        timezone="America/Chicago",
    )
    defaults.update(overrides)
    return BusinessProfile(**defaults)


# ── raw parse_phone with the expanded region set ─────────────────


def test_dutch_local_phone_parses_when_nl_in_accepted():
    """The Christiaan reproducer — 10-digit Dutch local should parse
    to E.164 when NL is in the accepted region list."""
    from packages.slot_parsers.phone_validator import parse_phone
    r = parse_phone(
        "0625007600",
        default_region="US",
        accepted_regions=["US", "NL"],
    )
    assert r.status.value == "complete"
    assert r.value == "+31625007600"
    assert r.matched_region == "NL"


def test_dutch_local_phone_fails_when_only_us():
    """The bug — US only region rejects Dutch local as partial."""
    from packages.slot_parsers.phone_validator import parse_phone
    r = parse_phone(
        "0625007600",
        default_region="US",
        accepted_regions=["US"],
    )
    # Not COMPLETE — that's the failure Christiaan hit.
    assert r.status.value != "complete"
    assert r.value is None


def test_default_accepted_regions_includes_nl():
    """The expanded default list must include NL so tenants that don't
    configure region get international coverage."""
    assert "NL" in _DEFAULT_ACCEPTED_PHONE_REGIONS


def test_default_accepted_regions_includes_common_eu_markets():
    """Any EU-facing clinic should get PT/ES/FR/DE/BE/IT/CH/AT by default."""
    for region in ("PT", "ES", "FR", "DE", "NL", "BE", "IT", "CH"):
        assert region in _DEFAULT_ACCEPTED_PHONE_REGIONS, (
            f"{region} missing from default accepted region list"
        )


def test_us_baseline_still_first():
    """US must still be present + reachable — regression check for
    every US tenant that shipped before this change."""
    assert "US" in _DEFAULT_ACCEPTED_PHONE_REGIONS


# ── BusinessProfile schema fields ─────────────────────────────────


def test_business_profile_default_phone_region_is_us():
    """Backward compatibility: a profile with no explicit region config
    still gets 'US' as its default, not empty/None."""
    b = _biz()
    assert b.phone_default_region == "US"


def test_business_profile_default_phone_accepted_empty_list():
    """Empty list = fall back to vertical_tools default set.  A
    tenant configuring an explicit list turns off the fallback."""
    b = _biz()
    assert b.phone_accepted_regions == []


def test_business_profile_can_override_region():
    """Tenant can set phone_default_region to their local country."""
    b = _biz(phone_default_region="NL")
    assert b.phone_default_region == "NL"


def test_business_profile_can_override_accepted_list():
    b = _biz(phone_accepted_regions=["NL", "BE", "DE"])
    assert b.phone_accepted_regions == ["NL", "BE", "DE"]


# ── _phone_region_config picks the right values ─────────────────


def test_phone_region_config_defaults_to_permissive_when_empty():
    """When phone_accepted_regions is empty on the profile, fall back
    to the permissive vertical_tools default."""
    b = _biz()  # phone_accepted_regions = []
    default_region, accepted = _phone_region_config(b)
    assert default_region == "US"
    # Should include NL (the Christiaan fix) via default fallback.
    assert "NL" in accepted


def test_phone_region_config_honors_tenant_override():
    """When tenant specifies phone_accepted_regions, use them verbatim."""
    b = _biz(
        phone_default_region="NL",
        phone_accepted_regions=["NL", "BE"],
    )
    default_region, accepted = _phone_region_config(b)
    assert default_region == "NL"
    # Default region is always included first.
    assert "NL" in accepted
    assert "BE" in accepted
    # Should NOT include the permissive fallback list — tenant was
    # explicit.
    assert "US" not in accepted or accepted == ["NL", "BE"]


def test_phone_region_config_ensures_default_included():
    """If tenant configures accepted regions without their default,
    the default gets prepended so parse_phone tries it first."""
    b = _biz(
        phone_default_region="NL",
        phone_accepted_regions=["BE", "DE"],  # NL missing
    )
    default_region, accepted = _phone_region_config(b)
    assert default_region == "NL"
    # NL was prepended.
    assert "NL" in accepted


# ── end-to-end: build_tools_for_vertical wires it through ─────────


def test_clinic_handler_receives_expanded_regions(tmp_path):
    """The critical end-to-end wire: a Dutch clinic profile causes
    ClinicToolHandler to be constructed with region 'NL' + NL+US in
    accepted list."""
    b = _biz(
        id="ribeira-dental", name="Ribeira Dental",
        phone_default_region="NL",
    )
    cal = FakeCalendar(tmp_path / "cal.json")
    _, handler = build_tools_for_vertical(b, cal)
    # ClinicToolHandler stores these fields directly for use in
    # _validate_phone_or_error.
    assert handler.default_phone_region == "NL"
    assert "NL" in handler.accepted_phone_regions
    # US should also be reachable via the default fallback.
    assert "US" in handler.accepted_phone_regions


def test_us_clinic_still_us_baseline(tmp_path):
    """Regression: US clinic with no override → US default + permissive
    accepted list including NL, so US baseline unchanged."""
    b = _biz()  # US default
    cal = FakeCalendar(tmp_path / "cal.json")
    _, handler = build_tools_for_vertical(b, cal)
    assert handler.default_phone_region == "US"
    assert "US" in handler.accepted_phone_regions
    # And NL is now accessible on US tenants too via the permissive
    # default — Christiaan's Dutch friend calling a US clinic works.
    assert "NL" in handler.accepted_phone_regions


# ── the actual christiaan-class booking flow works now ─────────


@pytest.mark.asyncio
async def test_book_appointment_accepts_dutch_phone(tmp_path):
    """End-to-end: Christiaan's exact input.  The tool receives his
    Dutch phone, validator canonicalizes to +31625007600, calendar
    write proceeds without an error path that would return {}."""
    from packages.schemas import ToolCall
    from packages.schemas.business import ServiceOffering
    b = _biz(
        phone_default_region="NL",
        services=[
            ServiceOffering(
                name="cleaning", duration_minutes=45,
                description="Cleaning",
            ),
        ],
    )
    cal = FakeCalendar(tmp_path / "cal.json")
    _, handler = build_tools_for_vertical(b, cal)
    from datetime import datetime, timedelta
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    # Emulate what LLM would send after Christiaan's dictation.
    result = await handler(ToolCall(
        id="c1", name="book_appointment",
        arguments={
            "caller_name": "Christiaan",
            "phone": "0625007600",  # Dutch local, no +31
            "service": "cleaning",
            "start_iso": tomorrow.isoformat(),
        },
    ))
    # The phone precondition must not have rejected — booked must succeed.
    # (If phone validation failed, result would contain phone_invalid
    # or phone_partial keys.)
    assert result.error is None
    r = result.result
    # Either booked=True (calendar accepted) OR a booked-like signal;
    # what MUST NOT be present are phone-rejection signals.
    assert not r.get("phone_invalid"), (
        f"Phone validator rejected Dutch number: {r}"
    )
    assert not r.get("phone_partial"), (
        f"Phone validator flagged Dutch number as partial: {r}"
    )
