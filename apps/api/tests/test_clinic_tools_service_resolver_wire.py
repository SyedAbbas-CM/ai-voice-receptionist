"""BUG-CHR-03 wire test: resolve_service fires inside ClinicToolHandler.

2026-08-29: unit tests confirm service_aliases works.  This test
confirms it's actually WIRED into the clinic tool handler's
check_availability and book_appointment branches — the layer above the
unit test but below the live call flow.

Contract:
- caller says "A follow-up" → book_appointment stores 'Follow-up visit'
- caller says "toothache" on check_availability → looks up availability
  for 'Emergency exam'
- caller says "consultation" (ambiguous) → returns service_ambiguous
  result the LLM sees + re-asks
- caller says "purple submarine" → returns service_unknown result
"""
from __future__ import annotations

import json

import pytest

from packages.integrations.clinic_tools import ClinicToolHandler
from packages.integrations.fake_calendar import FakeCalendar
from packages.schemas import BusinessProfile, ServiceOffering, ToolCall


def _fake_business():
    return BusinessProfile(
        id="test-clinic",
        name="Test Dental",
        vertical="clinic",
        timezone="America/Chicago",
        phone="+15551110000",
        email="test@example.com",
        website="https://example.com",
        address="1 Test St",
        hours={
            "monday": "09:00-17:00", "tuesday": "09:00-17:00",
            "wednesday": "09:00-17:00", "thursday": "09:00-17:00",
            "friday": "09:00-17:00", "saturday": None, "sunday": None,
        },
        services=[
            ServiceOffering(
                name="Adult cleaning", duration_minutes=45,
                description="",
            ),
            ServiceOffering(
                name="Emergency exam", duration_minutes=30,
                description="",
            ),
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="",
            ),
            ServiceOffering(
                name="Invisalign consultation", duration_minutes=45,
                description="",
            ),
            ServiceOffering(
                name="Implant consultation", duration_minutes=60,
                description="",
            ),
        ],
        faqs={},
        escalation_phone="+15551110000",
        voice_persona="Test",
    )


@pytest.fixture
def handler(tmp_path):
    cal_path = tmp_path / "cal.json"
    return ClinicToolHandler(
        business=_fake_business(),
        calendar=FakeCalendar(path=cal_path),
    ), cal_path


def _appointments_on_disk(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return []


# ── check_availability path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_availability_resolves_a_follow_up(handler):
    """'A follow-up' should resolve to 'Follow-up visit' before slot
    lookup — the exact Christiaan trigger."""
    h, _ = handler
    result = await h(ToolCall(
        id="1", name="check_availability",
        arguments={"service": "A follow-up", "date": "2026-09-14"},
    ))
    assert "open_slots" in result.result
    # Tool returns the CANONICAL service name so the LLM speaks it
    # back to the caller correctly ('Follow-up visit', not the
    # caller's shorthand).
    assert result.result["service"] == "Follow-up visit"


@pytest.mark.asyncio
async def test_check_availability_ambiguous_service_returns_structured(
    handler,
):
    """'Consultation' matches both Invisalign + Implant → AMBIGUOUS
    error the LLM sees.  No slot lookup performed."""
    h, _ = handler
    result = await h(ToolCall(
        id="1", name="check_availability",
        arguments={"service": "consultation", "date": "2026-09-14"},
    ))
    assert result.result.get("service_ambiguous") is True
    assert "candidates" in result.result
    assert len(result.result["candidates"]) >= 2


@pytest.mark.asyncio
async def test_check_availability_unknown_service_returns_structured(
    handler,
):
    """'Purple submarine' → UNKNOWN error with available_services list."""
    h, _ = handler
    result = await h(ToolCall(
        id="1", name="check_availability",
        arguments={"service": "purple submarine", "date": "2026-09-14"},
    ))
    assert result.result.get("service_unknown") is True
    assert "available_services" in result.result
    assert any(
        "cleaning" in s.lower()
        for s in result.result["available_services"]
    )


# ── book_appointment path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_book_appointment_resolves_toothache_to_emergency(handler):
    """'toothache' → Emergency exam.  Booking succeeds with the
    canonical duration (30min).  Confirms Christiaan-class 'caller
    used a synonym' inputs get honored."""
    h, _ = handler
    result = await h(ToolCall(
        id="1", name="book_appointment",
        arguments={
            "caller_name": "Test Patient",
            "phone": "+15551234567",
            "service": "toothache",
            "start_iso": "2026-09-14T10:00",
        },
    ))
    # Booking should succeed (not a service_ambiguous / _unknown /
    # phone_invalid dict).
    assert not result.result.get("service_ambiguous")
    assert not result.result.get("service_unknown")


@pytest.mark.asyncio
async def test_book_appointment_ambiguous_service_stops_write(handler):
    """Ambiguous service in book_appointment → LLM must re-ask.
    Calendar must NOT be written."""
    h, cal_path = handler
    result = await h(ToolCall(
        id="1", name="book_appointment",
        arguments={
            "caller_name": "Test",
            "phone": "+15551234567",
            "service": "consultation",  # AMBIGUOUS
            "start_iso": "2026-09-14T10:00",
        },
    ))
    assert result.result.get("service_ambiguous") is True
    # The calendar shouldn't have any booking on that day.
    assert _appointments_on_disk(cal_path) == []


@pytest.mark.asyncio
async def test_book_appointment_unknown_service_stops_write(handler):
    """Unknown service → LLM must ask for a real service.  No
    calendar side effect."""
    h, cal_path = handler
    result = await h(ToolCall(
        id="1", name="book_appointment",
        arguments={
            "caller_name": "Test",
            "phone": "+15551234567",
            "service": "purple submarine",  # UNKNOWN
            "start_iso": "2026-09-14T10:00",
        },
    ))
    assert result.result.get("service_unknown") is True
    assert _appointments_on_disk(cal_path) == []


# ── resolver helper independently exercised ────────────────────


def test_resolve_service_or_error_returns_canonical_string(handler):
    """MATCH_EXACT returns str, not ToolResult."""
    h, _ = handler
    r = h._resolve_service_or_error(
        "A follow-up",
        ToolCall(id="1", name="book_appointment", arguments={}),
    )
    assert isinstance(r, str)
    assert r == "Follow-up visit"


def test_resolve_service_or_error_returns_toolresult_on_ambiguous(handler):
    h, _ = handler
    r = h._resolve_service_or_error(
        "consultation",
        ToolCall(id="1", name="book_appointment", arguments={}),
    )
    # ToolResult, not string.
    assert not isinstance(r, str)
    assert r.result.get("service_ambiguous") is True


def test_resolve_service_or_error_returns_toolresult_on_unknown(handler):
    h, _ = handler
    r = h._resolve_service_or_error(
        "purple submarine",
        ToolCall(id="1", name="book_appointment", arguments={}),
    )
    assert not isinstance(r, str)
    assert r.result.get("service_unknown") is True
