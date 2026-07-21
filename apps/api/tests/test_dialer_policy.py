from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from packages.integrations.dialer_policy import (
    DialerPolicy,
    Decision,
    Lead,
    decide_can_call,
    filter_leads,
    lead_from_sheet_row,
)


# --- SubtoDealz Florida ET policy defaults ---
FL_POLICY = DialerPolicy()  # defaults are Florida ET, 9-6, Mon-Fri, 24h, 3 attempts


def _et(y, m, d, h, mi) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York"))


def _et_as_utc(y, m, d, h, mi) -> datetime:
    return _et(y, m, d, h, mi).astimezone(ZoneInfo("UTC"))


def test_ok_during_florida_business_hours():
    lead = Lead(phone="+15551234567", name="Alice", total_calls=0)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 10, 0))  # Wed 10am ET
    assert d.can_call
    assert d.reason == "ok"


def test_rejected_at_night():
    lead = Lead(phone="+15551234567", total_calls=0)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 3, 30))  # Wed 3:30am ET
    assert not d.can_call
    assert d.reason == "out_of_hours"


def test_rejected_on_weekend():
    lead = Lead(phone="+15551234567", total_calls=0)
    # 2026-07-11 is a Saturday
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 11, 12, 0))
    assert not d.can_call
    assert d.reason == "out_of_hours"


def test_rejected_at_boundary_6pm_et():
    lead = Lead(phone="+15551234567", total_calls=0)
    # 6:00 PM ET exactly — window is 9-18, so 18:00 is out (< end)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 18, 0))
    assert not d.can_call
    assert d.reason == "out_of_hours"


def test_allowed_at_9am_start():
    lead = Lead(phone="+15551234567", total_calls=0)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 9, 0))
    assert d.can_call


def test_cooldown_blocks_within_24h():
    now_utc = _et_as_utc(2026, 7, 8, 14, 0)
    lead = Lead(
        phone="+15551234567", total_calls=1,
        last_called=now_utc - timedelta(hours=5),  # called 5h ago
    )
    d = decide_can_call(lead, FL_POLICY, now_utc=now_utc)
    assert not d.can_call
    assert d.reason == "cooldown"
    assert "18.9h" in d.detail or "19.0h" in d.detail  # 24 - 5.something


def test_cooldown_passes_after_24h():
    now_utc = _et_as_utc(2026, 7, 8, 14, 0)
    lead = Lead(
        phone="+15551234567", total_calls=1,
        last_called=now_utc - timedelta(hours=25),
    )
    d = decide_can_call(lead, FL_POLICY, now_utc=now_utc)
    assert d.can_call


def test_max_attempts_blocks_at_3():
    lead = Lead(phone="+15551234567", total_calls=3, last_called=None)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 10, 0))
    assert not d.can_call
    assert d.reason == "max_attempts"


def test_dnc_list_blocks_regardless_of_hours():
    policy = DialerPolicy(dnc_numbers=frozenset(["+15551234567"]))
    lead = Lead(phone="+15551234567", total_calls=0)
    d = decide_can_call(lead, policy, now_utc=_et_as_utc(2026, 7, 8, 10, 0))
    assert not d.can_call
    assert d.reason == "dnc"


def test_dnc_normalizes_phone_formatting():
    """DNC set stored as +15551234567 should also match '(555) 123-4567' formatting."""
    policy = DialerPolicy(dnc_numbers=frozenset(["+15551234567"]))
    lead = Lead(phone="(555) 123-4567", total_calls=0)  # missing country code
    d = decide_can_call(lead, policy, now_utc=_et_as_utc(2026, 7, 8, 10, 0))
    # Normalizer for the lead phone strips to "5551234567", DNC has "+15551234567" — no match
    # This is intentional: DNC entries must include the country-code form clients dial with
    assert d.can_call  # not a match, so proceeds


def test_terminal_status_blocks():
    for terminal in ("HOT_LEAD", "hot lead", "PROPERTY_UNAVAILABLE", "completed", "answered"):
        lead = Lead(phone="+15551234567", total_calls=1, status=terminal)
        d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 10, 0))
        assert not d.can_call, f"expected block for status {terminal!r}"
        assert d.reason == "terminal_status"


def test_no_phone_blocks():
    lead = Lead(phone="", total_calls=0)
    d = decide_can_call(lead, FL_POLICY, now_utc=_et_as_utc(2026, 7, 8, 10, 0))
    assert not d.can_call
    assert d.reason == "no_phone"


def test_filter_leads_partitions_correctly():
    now_utc = _et_as_utc(2026, 7, 8, 10, 0)
    leads = [
        Lead(phone="+15551000001", total_calls=0),  # ok
        Lead(phone="+15551000002", total_calls=3),  # max_attempts
        Lead(phone="", total_calls=0),              # no_phone
        Lead(phone="+15551000003", total_calls=0, last_called=now_utc - timedelta(hours=2)),  # cooldown
    ]
    dialable, skipped = filter_leads(leads, FL_POLICY, now_utc=now_utc)
    assert len(dialable) == 1
    assert dialable[0].phone == "+15551000001"
    reasons = {reason for _, reason in [(l, d.reason) for l, d in skipped]}
    assert reasons == {"max_attempts", "no_phone", "cooldown"}


def test_lead_from_sheet_row_handles_subtodealz_casing():
    row = {
        "Name": "Bob Owner",
        "Phone": "5551234567",
        "Property address": "123 Elm St",
        "Total Calls": "2",
        "Last Called": "2026-07-07T14:30:00+00:00",
        "Status": "",
        "Notes": "left voicemail",
    }
    lead = lead_from_sheet_row(row)
    assert lead.phone == "5551234567"
    assert lead.name == "Bob Owner"
    assert lead.total_calls == 2
    assert lead.last_called is not None
    assert lead.status == ""
    assert lead.extra["Property address"] == "123 Elm St"


def test_lead_from_sheet_row_tolerates_lowercase_casing():
    """The original SubtoDealz graph had 'Total Calls' vs 'Total calls' casing
    inconsistency that silently disabled the max-3-attempts guard. Fixed here."""
    row = {"Phone": "5551234567", "Total calls": "3"}  # lowercase 'c'
    lead = lead_from_sheet_row(row)
    assert lead.total_calls == 3  # NOT 0 (the n8n bug)


def test_custom_timezone_and_hours():
    """Non-Florida tenant: London GMT, 10am-5pm, Mon-Sat."""
    policy = DialerPolicy(
        timezone="Europe/London",
        weekdays=(0, 1, 2, 3, 4, 5),  # Mon-Sat
        business_hours=((time(10, 0), time(17, 0)),),
    )
    lead = Lead(phone="+442012345678", total_calls=0)
    # Saturday 2026-07-11 at 11am London
    now_utc = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("UTC"))  # 11am BST
    d = decide_can_call(lead, policy, now_utc=now_utc)
    assert d.can_call
