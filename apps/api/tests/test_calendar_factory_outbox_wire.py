"""Confirm calendar_factory now wraps with OutboxCalendar automatically.

2026-08-29: the whole point of the outbox design is that the box's
current calendar_backend='fake' setting starts producing an
OutboxCalendar-wrapping-FakeCalendar with zero env changes on next
deploy. This test locks the wire.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from packages.integrations.calendar_factory import build_calendar
from packages.integrations.outbox_calendar import OutboxCalendar
from packages.integrations.fake_calendar import FakeCalendar


class _Settings:
    def __init__(self, tmp_path):
        self.calendar_path = str(tmp_path / "cal.json")
        self.google_service_account_json = None
        self.google_calendar_id = None
        self.outbox_enabled = True


def test_fake_backend_returns_outbox_wrapper(tmp_path):
    """Default 'fake' backend now returns OutboxCalendar."""
    cal = build_calendar("fake", _Settings(tmp_path))
    assert isinstance(cal, OutboxCalendar)
    # Local backend is the original FakeCalendar.
    assert isinstance(cal.local, FakeCalendar)


def test_fake_backend_outbox_disabled_returns_bare_local(tmp_path):
    """outbox_enabled=False (for tests) returns unwrapped FakeCalendar."""
    s = _Settings(tmp_path)
    s.outbox_enabled = False
    cal = build_calendar("fake", s)
    assert isinstance(cal, FakeCalendar)
    assert not isinstance(cal, OutboxCalendar)


def test_outbox_path_defaults_next_to_calendar(tmp_path):
    """Default outbox JSONL sits beside the calendar JSON."""
    cal = build_calendar("fake", _Settings(tmp_path))
    # Sibling file.
    expected = tmp_path / "calendar_outbox.jsonl"
    assert Path(cal.outbox.path) == expected


def test_outbox_path_explicit_override(tmp_path):
    s = _Settings(tmp_path)
    s.calendar_outbox_path = str(tmp_path / "custom_outbox.jsonl")
    cal = build_calendar("fake", s)
    assert Path(cal.outbox.path).name == "custom_outbox.jsonl"


def test_no_google_creds_remote_available_false(tmp_path):
    """Without Google creds, the wrapped calendar reports remote
    unavailable — sync worker will no-op on tick."""
    cal = build_calendar("fake", _Settings(tmp_path))
    assert cal.remote_available() is False


def test_google_backend_unchanged_no_outbox_wrap(tmp_path, monkeypatch):
    """Explicit 'google' backend is passed through unwrapped — Google
    IS the source of truth in that mode; there's nothing to sync."""
    s = _Settings(tmp_path)
    s.google_service_account_json = "/fake/path.json"
    s.google_calendar_id = "primary"

    # Stub out GoogleCalendar so we don't need google creds.
    class _StubGoogle:
        def __init__(self, service_account_json_path, calendar_id):
            self.calendar_id = calendar_id
    monkeypatch.setattr(
        "packages.integrations.google_calendar.GoogleCalendar",
        _StubGoogle,
    )
    cal = build_calendar("google", s)
    assert not isinstance(cal, OutboxCalendar)


def test_writing_through_wrapped_backend_enqueues_outbox(tmp_path):
    """End-to-end: book via factory-produced backend → local + outbox."""
    from datetime import datetime
    from packages.schemas import BusinessHours

    class _Biz:
        hours = BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        )

    cal = build_calendar("fake", _Settings(tmp_path), business=_Biz())
    result = cal.book(
        start=datetime(2026, 9, 1, 10, 0),
        duration_minutes=30,
        caller_name="Abbas",
        phone="+15551234567",
        service="Adult cleaning",
    )
    assert result["booked"] is True
    # Outbox has one record.
    stats = cal.outbox_stats()
    assert stats["pending"] == 1
    assert stats["total"] == 1
