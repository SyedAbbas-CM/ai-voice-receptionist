"""R3 P4 slim v1 (task #371): phone precondition on book_appointment.

Contract:
  * Tool handler validates call.arguments["phone"] through libphonenumber
    BEFORE hitting the calendar.
  * VALID / POSSIBLE  → proceed; phone stored as canonical E.164.
  * PARTIAL / INVALID / TOO_LONG / EMPTY  → structured error the LLM
    sees; NO calendar write.
  * brain.py's _reply_lies_about_booking treats these structured errors
    as "not a real booking" — otherwise the LLM saying "you're booked"
    would slip past the guard.
  * brain.py's on_tool_receipt signal for the SpeechCommitGate reports
    ok=False for these — held ACTION_CONFIRMATION sentences stay held.

These tests exercise the tool handler + the brain helper directly.
The full LLM flow is covered by the existing brain_booking_flow tests
(all still pass with these changes).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from packages.integrations.clinic_tools import ClinicToolHandler
from packages.integrations.fake_calendar import FakeCalendar
from packages.schemas import BusinessProfile, ToolCall


@pytest.fixture
def calendar(tmp_path) -> FakeCalendar:
    return FakeCalendar(tmp_path / "cal.json")


def _biz() -> BusinessProfile:
    return BusinessProfile(
        id="test-biz", name="Test Clinic", vertical="clinic",
        timezone="America/Chicago",
        services=[{"name": "cleaning", "duration_minutes": 45}],
        escalation_phone="+15550000000",
    )


def _handler(calendar, region="US", accepted=None) -> ClinicToolHandler:
    return ClinicToolHandler(
        business=_biz(), calendar=calendar,
        default_phone_region=region,
        accepted_phone_regions=accepted or [region],
    )


def _book_call(phone: str) -> ToolCall:
    return ToolCall(
        id="test-1",
        name="book_appointment",
        arguments={
            "caller_name": "Sarah",
            "phone": phone,
            "service": "cleaning",
            "start_iso": datetime(2026, 8, 20, 10, 0).isoformat(),
        },
    )


# ── valid inputs: proceed to book, phone stored as E.164 ────────────

@pytest.mark.asyncio
async def test_valid_phone_proceeds_to_book(calendar):
    h = _handler(calendar)
    r = await h(_book_call("+16502530000"))
    # Not a precondition error — real book outcome.
    assert not r.result.get("phone_invalid")
    assert not r.result.get("phone_partial")
    assert r.result.get("booked") is True


@pytest.mark.asyncio
async def test_us_number_without_plus_is_normalized(calendar):
    h = _handler(calendar)
    r = await h(_book_call("6502530000"))
    assert r.result.get("booked") is True
    # Stored phone must be canonical E.164.
    assert r.result["event"]["phone"] == "+16502530000"


@pytest.mark.asyncio
async def test_us_number_with_hyphens_is_normalized(calendar):
    h = _handler(calendar)
    r = await h(_book_call("650-253-0000"))
    assert r.result.get("booked") is True
    assert r.result["event"]["phone"] == "+16502530000"


@pytest.mark.asyncio
async def test_pk_number_accepted_for_pk_tenant(calendar):
    """The Karachi-tester case — a US tenant that also serves PK
    callers passes accepted_phone_regions=['US', 'PK'].  A PK-only
    tenant validates PK numbers natively."""
    h = _handler(calendar, region="PK")
    r = await h(_book_call("03335244772"))
    assert r.result.get("booked") is True
    assert r.result["event"]["phone"] == "+923335244772"


# ── precondition failures: NO calendar write ────────────────────────

@pytest.mark.asyncio
async def test_missing_phone_returns_structured_error(calendar):
    h = _handler(calendar)
    r = await h(_book_call(""))
    assert r.result.get("phone_missing") is True
    # Calendar was NOT written.
    import json
    events = json.loads(calendar.path.read_text()) if calendar.path.exists() else []
    assert events == []


@pytest.mark.asyncio
async def test_partial_phone_returns_structured_error(calendar):
    h = _handler(calendar)
    r = await h(_book_call("650"))   # only 3 digits
    assert r.result.get("phone_partial") is True
    assert "phone_input" in r.result


@pytest.mark.asyncio
async def test_too_long_phone_returns_structured_error(calendar):
    h = _handler(calendar)
    r = await h(_book_call("650253000000000000"))  # way too long
    # libphonenumber treats this as too_long / invalid depending on
    # region.  Either shape is a precondition failure for the caller.
    assert (r.result.get("phone_too_long")
            or r.result.get("phone_invalid")) is True


@pytest.mark.asyncio
async def test_garbage_phone_returns_invalid(calendar):
    h = _handler(calendar)
    r = await h(_book_call("this is not a number"))
    # Either invalid or partial (spoken-digit normalizer strips all
    # non-digits, leaving empty → EMPTY status → mapped by our helper
    # to phone_missing).  Whatever the shape, it must NOT be a book.
    assert r.result.get("booked") is not True
    assert any(k in r.result for k in (
        "phone_invalid", "phone_missing", "phone_partial",
    ))


# ── brain.py guard: precondition errors don't count as bookings ─────

def test_reply_lies_about_booking_treats_phone_invalid_as_no_write():
    """If the LLM says 'you're booked' after a precondition failure,
    _reply_lies_about_booking MUST return True (rewrite triggered)."""
    from packages.core_agent.brain import _reply_lies_about_booking

    tool_results = [{
        "name": "book_appointment",
        "arguments": {"phone": "bad"},
        "result": {"phone_invalid": True, "reason": "..."},
        "error": None,
    }]
    assert _reply_lies_about_booking("You're all set!", tool_results) is True


def test_reply_lies_about_booking_treats_phone_missing_as_no_write():
    from packages.core_agent.brain import _reply_lies_about_booking
    tool_results = [{
        "name": "book_appointment",
        "arguments": {},
        "result": {"phone_missing": True, "reason": "..."},
        "error": None,
    }]
    assert _reply_lies_about_booking("You're booked for tomorrow.", tool_results) is True


def test_reply_lies_about_booking_accepts_real_receipt():
    """Sanity: a real successful booking receipt must NOT be flagged."""
    from packages.core_agent.brain import _reply_lies_about_booking
    tool_results = [{
        "name": "book_appointment",
        "arguments": {"phone": "+16502530000"},
        "result": {
            "booked": True,
            "event": {"id": "evt_1", "phone": "+16502530000"},
        },
        "error": None,
    }]
    assert _reply_lies_about_booking("You're all set!", tool_results) is False
