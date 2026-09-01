"""Per-tenant calendar backend construction from BusinessProfile.integrations.

Covers part C of GHL-wave-2:
  - build_calendar_from_business chooses fake / google / ghl based on
    business.integrations.calendar_backend
  - GHL calendar backend picks up creds from integrations, not env
  - Missing creds raise a clear per-tenant RuntimeError
  - Default (unset) falls through to fake
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from packages.integrations.calendar_factory import (
    build_calendar_from_business,
)
from packages.schemas.business import BusinessProfile, Integrations


def _biz(**integ_kwargs) -> BusinessProfile:
    return BusinessProfile(
        id="clinic-x", name="Test Clinic",
        integrations=Integrations(**integ_kwargs),
    )


def test_default_backend_is_fake(tmp_path):
    class _S: calendar_path = str(tmp_path / "cal.json")
    cal = build_calendar_from_business(_biz(), _S())
    # FakeCalendar has a list_slots method
    assert hasattr(cal, "list_slots")
    assert hasattr(cal, "book")


def test_fake_backend_explicit(tmp_path):
    class _S: calendar_path = str(tmp_path / "cal.json")
    cal = build_calendar_from_business(
        _biz(calendar_backend="fake"), _S(),
    )
    assert hasattr(cal, "list_slots")


def test_ghl_backend_missing_token_raises():
    with pytest.raises(RuntimeError, match="ghl_api_token"):
        build_calendar_from_business(
            _biz(calendar_backend="ghl", ghl_location_id="loc123"),
        )


def test_ghl_backend_missing_location_raises():
    with pytest.raises(RuntimeError, match="ghl_location_id"):
        build_calendar_from_business(
            _biz(calendar_backend="ghl", ghl_api_token="pit-abc"),
        )


def test_ghl_backend_missing_calendar_id_raises():
    with pytest.raises(RuntimeError, match="ghl_calendar_id"):
        build_calendar_from_business(
            _biz(
                calendar_backend="ghl",
                ghl_api_token="pit-abc",
                ghl_location_id="loc123",
                # ghl_calendar_id missing
            ),
        )


def test_ghl_backend_returns_ghl_calendar():
    from packages.integrations.ghl_calendar import GHLCalendar
    cal = build_calendar_from_business(
        _biz(
            calendar_backend="ghl",
            ghl_api_token="pit-abc",
            ghl_location_id="loc123",
            ghl_calendar_id="cal456",
        ),
    )
    assert isinstance(cal, GHLCalendar)
    assert cal.calendar_id == "cal456"


def test_google_backend_missing_service_account_raises():
    with pytest.raises(RuntimeError, match="google_service_account_json|google_calendar_id"):
        build_calendar_from_business(
            _biz(
                calendar_backend="google",
                google_calendar_id="primary@grp",
                # service account missing
            ),
        )


def test_unknown_backend_raises():
    # Skip pydantic validation by mutating after construct
    biz = _biz(calendar_backend="fake")
    biz.integrations.__dict__["calendar_backend"] = "supabase"
    with pytest.raises(ValueError, match="calendar_backend"):
        build_calendar_from_business(biz)


# ─── GHLCalendar.list_slots parsing ─────────────────────────────────────


def test_ghl_calendar_list_slots_parses_iso():
    """GHL returns slots as ISO strings; we surface HH:MM."""
    from packages.integrations.ghl_calendar import GHLCalendar
    from datetime import datetime
    mock_client = MagicMock()
    mock_client.default_calendar_id = "cal456"
    mock_client.list_free_slots = AsyncMock(return_value=[
        "2026-09-01T09:00:00-05:00",
        "2026-09-01T09:30:00-05:00",
        "2026-09-01T10:00:00-05:00",
    ])
    cal = GHLCalendar(client=mock_client, calendar_id="cal456")
    slots = cal.list_slots(
        datetime(2026, 9, 1), duration_minutes=30,
        open_hhmm="09:00", close_hhmm="11:00",
    )
    assert "09:00" in slots
    assert "09:30" in slots
    assert "10:00" in slots


def test_ghl_calendar_book_upserts_then_books():
    """book() calls upsert_contact THEN book_appointment on the client."""
    from packages.integrations.ghl_calendar import GHLCalendar
    from datetime import datetime
    mock_client = MagicMock()
    mock_client.default_calendar_id = "cal456"
    mock_client.upsert_contact = AsyncMock(return_value={"id": "contact_xyz"})
    mock_client.book_appointment = AsyncMock(return_value={
        "id": "evt_123",
        "startTime": "2026-09-01T09:00:00-05:00",
        "endTime": "2026-09-01T09:30:00-05:00",
    })
    cal = GHLCalendar(client=mock_client, calendar_id="cal456")
    result = cal.book(
        start=datetime(2026, 9, 1, 9, 0),
        duration_minutes=30,
        caller_name="Abbas",
        phone="+15551234567",
        service="Adult cleaning",
        notes=None,
    )
    assert result["booked"] is True
    assert result["event"]["id"] == "evt_123"
    assert result["event"]["contact_id"] == "contact_xyz"
    mock_client.upsert_contact.assert_called_once()
    mock_client.book_appointment.assert_called_once()


def test_ghl_calendar_book_failure_returns_booked_false():
    from packages.integrations.ghl_calendar import GHLCalendar
    from datetime import datetime
    mock_client = MagicMock()
    mock_client.default_calendar_id = "cal456"
    mock_client.upsert_contact = AsyncMock(side_effect=RuntimeError("GHL 500"))
    cal = GHLCalendar(client=mock_client, calendar_id="cal456")
    result = cal.book(
        start=datetime(2026, 9, 1, 9, 0),
        duration_minutes=30,
        caller_name="Test",
        phone="+15551234567",
        service="Cleaning",
    )
    assert result["booked"] is False
    assert "reason" in result
