"""Tests for packages/compliance/jurisdiction.py.

Pins:
- TWO_PARTY_STATES membership is correct
- infer_us_state parses address + timezone as designed
- audit_business_compliance flags recording-consent gap in 2-party states
- Never raises on garbage input
- log_compliance_audit emits WARNING on gap, INFO on OK
"""
from __future__ import annotations

import logging

from packages.compliance.jurisdiction import (
    TWO_PARTY_STATES,
    audit_business_compliance,
    infer_us_state,
    log_compliance_audit,
)


# ── state list ──────────────────────────────────────────────────


def test_two_party_states_includes_california_and_florida():
    assert "CA" in TWO_PARTY_STATES
    assert "FL" in TWO_PARTY_STATES
    assert "IL" in TWO_PARTY_STATES
    assert "PA" in TWO_PARTY_STATES
    assert "WA" in TWO_PARTY_STATES


def test_two_party_states_excludes_one_party_states():
    # NY / TX / CO / MO / OH are all one-party (federal) states.
    assert "NY" not in TWO_PARTY_STATES
    assert "TX" not in TWO_PARTY_STATES
    assert "CO" not in TWO_PARTY_STATES
    assert "MO" not in TWO_PARTY_STATES
    assert "OH" not in TWO_PARTY_STATES


# ── infer_us_state ──────────────────────────────────────────────


def test_infer_from_address_zip_pattern():
    assert infer_us_state(address="123 Main St, Los Angeles, CA 90001") == "CA"
    assert infer_us_state(address="500 Broadway, New York, NY 10012") == "NY"
    assert infer_us_state(address="789 Ocean Dr, Miami, FL 33139-1234") == "FL"


def test_infer_from_timezone_unambiguous():
    assert infer_us_state(timezone="America/Los_Angeles") == "CA"
    assert infer_us_state(timezone="Pacific/Honolulu") == "HI"


def test_infer_from_timezone_ambiguous_returns_none():
    """Chicago timezone covers IL/WI/MO/AR/TN — ambiguous."""
    assert infer_us_state(timezone="America/Chicago") is None
    assert infer_us_state(timezone="America/New_York") is None


def test_infer_address_wins_over_timezone():
    """Business in TX (one-party) with LA tz should read as TX."""
    assert infer_us_state(
        address="123 Main St, Dallas, TX 75093",
        timezone="America/Los_Angeles",
    ) == "TX"


def test_infer_returns_none_on_junk():
    assert infer_us_state(address="", timezone="") is None
    assert infer_us_state(address=None, timezone=None) is None
    assert infer_us_state(address="not an address") is None


# ── audit_business_compliance ───────────────────────────────────


class _MockBusiness:
    def __init__(
        self,
        *,
        address=None,
        timezone=None,
        recording_notice_enabled=False,
        ai_disclosure_enabled=False,
    ) -> None:
        self.address = address
        self.timezone = timezone
        self.recording_notice_enabled = recording_notice_enabled
        self.ai_disclosure_enabled = ai_disclosure_enabled


def test_audit_two_party_state_no_recording_warns():
    """CA business without recording notice → WARNING."""
    b = _MockBusiness(
        address="123 Main, San Francisco, CA 94103",
        recording_notice_enabled=False,
    )
    audit = audit_business_compliance(b)
    assert audit.ok is False
    assert audit.inferred_state == "CA"
    assert any("CA" in w and "wiretap" in w.lower() for w in audit.warnings)


def test_audit_two_party_state_with_recording_ok():
    """CA business WITH recording notice → OK."""
    b = _MockBusiness(
        address="123 Main, San Francisco, CA 94103",
        recording_notice_enabled=True,
    )
    audit = audit_business_compliance(b)
    assert audit.ok is True
    assert audit.warnings == ()
    assert any("CA" in n for n in audit.notes)


def test_audit_one_party_state_no_recording_ok():
    """TX (one-party) without recording notice → OK, just a note."""
    b = _MockBusiness(
        address="123 Main, Dallas, TX 75093",
        recording_notice_enabled=False,
    )
    audit = audit_business_compliance(b)
    assert audit.ok is True
    assert audit.inferred_state == "TX"
    assert audit.warnings == ()
    # Recommendation still surfaces as a note.
    assert any("optional" in n.lower() or "recommended" in n.lower()
               for n in audit.notes)


def test_audit_unknown_state_notes_the_gap():
    b = _MockBusiness(address=None, timezone=None)
    audit = audit_business_compliance(b)
    assert audit.inferred_state is None
    assert audit.ok is True  # no confirmed gap
    assert any("could not infer" in n.lower() for n in audit.notes)


def test_audit_ai_disclosure_disabled_notes():
    """AI disclosure off → note, not warning (advisory)."""
    b = _MockBusiness(
        address="123 Main, Austin, TX 78701",
        ai_disclosure_enabled=False,
    )
    audit = audit_business_compliance(b)
    assert any("California SB 1001" in n or "Utah" in n for n in audit.notes)


def test_audit_never_raises_on_garbage():
    class _Garbage:
        pass
    audit = audit_business_compliance(_Garbage())
    # Should NOT raise — returns a valid ComplianceAudit.
    assert audit is not None


# ── log_compliance_audit ────────────────────────────────────────


def test_log_compliance_audit_emits_warning_on_gap(caplog):
    b = _MockBusiness(
        address="123 Main, Chicago, IL 60601",
        recording_notice_enabled=False,
    )
    with caplog.at_level(logging.WARNING,
                          logger="packages.compliance.jurisdiction"):
        audit = log_compliance_audit(b, source="test")
    assert audit.ok is False
    # WARNING log should mention the state.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 1
    assert any("IL" in r.message for r in warnings)


def test_log_compliance_audit_no_warning_when_ok(caplog):
    b = _MockBusiness(
        address="123 Main, Austin, TX 78701",
        recording_notice_enabled=False,
    )
    with caplog.at_level(logging.INFO,
                          logger="packages.compliance.jurisdiction"):
        log_compliance_audit(b, source="test")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == []


# ── format_human (human-readable output) ────────────────────────


def test_format_human_includes_state_and_warnings():
    b = _MockBusiness(
        address="123 Main, LA, CA 90001",
        recording_notice_enabled=False,
    )
    audit = audit_business_compliance(b)
    out = audit.format_human()
    assert "CA" in out
    assert "Warnings" in out


def test_format_human_ok_case():
    b = _MockBusiness(
        address="123 Main, Austin, TX 78701",
        recording_notice_enabled=False,
    )
    audit = audit_business_compliance(b)
    out = audit.format_human()
    assert "TX" in out
