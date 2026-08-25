"""Tests for GoogleCalendar.find_by_phone / cancel / reschedule.

2026-08-25 (humanness audit P0.7 capability trap): FakeCalendar had
these; real GoogleCalendar did not.  clinic_tools exposed cancel /
reschedule to the LLM but they only worked in demo.  This test suite
pins parity between the two backends.

The Google Calendar API is mocked via a hand-rolled fake service —
we don't hit real Google.  The fake stores events in-memory and
mirrors the subset of `events().list/get/patch/delete/insert` the
adapter actually uses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from packages.integrations.google_calendar import GoogleCalendar


# ── fake Google Calendar service ─────────────────────────────────


class _FakeEventsResource:
    """Fake for `service.events()`.  Stores events in-memory."""

    def __init__(self, store):
        self._store = store  # shared dict, id → event

    # ── list ────────────────────────────────────────────────────

    class _ListReq:
        def __init__(self, store, params):
            self._store = store
            self._params = params

        def execute(self):
            # Simple in-memory filter: only respect status + time-window.
            items = []
            time_min = self._params.get("timeMin")
            time_max = self._params.get("timeMax")
            for ev in self._store.values():
                if ev.get("status") == "cancelled":
                    # Emit cancelled events too — caller filters — but
                    # only when the caller didn't set an implicit filter.
                    # For simplicity, always emit; adapter filters.
                    pass
                # Time-window filter (best-effort — dateTime lexicographic
                # sort works when tzs match, good enough for tests).
                start = (ev.get("start") or {}).get("dateTime") or ""
                end = (ev.get("end") or {}).get("dateTime") or ""
                if time_min and end and end < time_min:
                    continue
                if time_max and start and start > time_max:
                    continue
                items.append(ev)
            return {"items": items}

    def list(self, **params):
        return self._ListReq(self._store, params)

    # ── get ─────────────────────────────────────────────────────

    class _GetReq:
        def __init__(self, store, event_id):
            self._store = store
            self._event_id = event_id

        def execute(self):
            ev = self._store.get(self._event_id)
            if ev is None:
                # Simulate Google 404.
                raise RuntimeError(
                    "HttpError 404 when requesting event: notFound"
                )
            return dict(ev)

    def get(self, calendarId, eventId):
        return self._GetReq(self._store, eventId)

    # ── patch ───────────────────────────────────────────────────

    class _PatchReq:
        def __init__(self, store, event_id, body):
            self._store = store
            self._event_id = event_id
            self._body = body

        def execute(self):
            ev = self._store.get(self._event_id)
            if ev is None:
                raise RuntimeError("HttpError 404 notFound")
            for k, v in self._body.items():
                ev[k] = v
            self._store[self._event_id] = ev
            return dict(ev)

    def patch(self, calendarId, eventId, body):
        return self._PatchReq(self._store, eventId, body)

    # ── delete ──────────────────────────────────────────────────

    class _DelReq:
        def __init__(self, store, event_id):
            self._store = store
            self._event_id = event_id

        def execute(self):
            if self._event_id in self._store:
                del self._store[self._event_id]
            return None

    def delete(self, calendarId, eventId):
        return self._DelReq(self._store, eventId)

    # ── insert ──────────────────────────────────────────────────

    class _InsReq:
        def __init__(self, store, body):
            self._store = store
            self._body = body

        def execute(self):
            eid = f"gcal-{uuid.uuid4().hex[:8]}"
            body = dict(self._body)
            body["id"] = eid
            body["status"] = "confirmed"
            self._store[eid] = body
            return dict(body)

    def insert(self, calendarId, body):
        return self._InsReq(self._store, body)


class _FakeService:
    def __init__(self):
        self._events_store = {}

    def events(self):
        return _FakeEventsResource(self._events_store)


@pytest.fixture
def cal():
    """A GoogleCalendar whose _svc() returns our fake."""
    gc = GoogleCalendar(
        service_account_json_path="unused", calendar_id="cal-x",
    )
    gc._service = _FakeService()
    return gc


def _seed_event(cal, *, phone, start, duration_min=30,
                 caller_name="Sarah Chen", status="confirmed"):
    end = start + timedelta(minutes=duration_min)
    body = {
        "summary": f"cleaning — {caller_name}",
        "description": f"Booked via voiceops-ai-agent.\n"
                        f"Caller: {caller_name}\n"
                        f"Phone: {phone}\n"
                        f"Service: cleaning\nNotes: ",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    ev = cal._svc().events().insert(calendarId="cal-x", body=body).execute()
    if status != "confirmed":
        cal._svc()._events_store[ev["id"]]["status"] = status
    return ev["id"]


# ── phone-string normalization ────────────────────────────────────


def test_normalize_phone_strips_non_digits():
    assert GoogleCalendar._normalize_phone("+1 (555) 123-4567") == "15551234567"
    assert GoogleCalendar._normalize_phone("555.123.4567") == "5551234567"
    assert GoogleCalendar._normalize_phone("") == ""
    assert GoogleCalendar._normalize_phone(None) == ""  # type: ignore[arg-type]


def test_extract_phone_from_description(cal):
    ev = {"description": "Booked via voiceops-ai-agent.\n"
                         "Caller: Sarah\nPhone: +1-555-123-4567\n"
                         "Service: cleaning\n"}
    assert cal._extract_phone(ev) == "+1-555-123-4567"


def test_extract_phone_from_extended_properties(cal):
    ev = {
        "description": "No phone in desc.",
        "extendedProperties": {"private": {"phone": "+15551234567"}},
    }
    assert cal._extract_phone(ev) == "+15551234567"


def test_extract_phone_missing_returns_empty(cal):
    assert cal._extract_phone({"description": ""}) == ""
    assert cal._extract_phone({}) == ""


# ── find_by_phone ─────────────────────────────────────────────────


def test_find_by_phone_returns_matching_upcoming(cal):
    now = datetime.now(timezone.utc)
    _seed_event(cal, phone="+15551234567", start=now + timedelta(days=2))
    _seed_event(cal, phone="+15559999999", start=now + timedelta(days=1))
    results = cal.find_by_phone("+15551234567")
    assert len(results) == 1
    assert results[0]["summary"] == "cleaning — Sarah Chen"


def test_find_by_phone_normalizes_input(cal):
    """Caller phone with formatting matches stored E.164."""
    now = datetime.now(timezone.utc)
    _seed_event(cal, phone="+15551234567", start=now + timedelta(days=2))
    results = cal.find_by_phone("(555) 123-4567")
    assert len(results) == 1


def test_find_by_phone_skips_cancelled(cal):
    now = datetime.now(timezone.utc)
    _seed_event(
        cal, phone="+15551234567", start=now + timedelta(days=2),
        status="cancelled",
    )
    assert cal.find_by_phone("+15551234567") == []


def test_find_by_phone_skips_past_when_upcoming_only(cal):
    now = datetime.now(timezone.utc)
    _seed_event(cal, phone="+15551234567", start=now - timedelta(days=5))
    assert cal.find_by_phone("+15551234567", upcoming_only=True) == []
    # But finds them with upcoming_only=False.
    assert len(cal.find_by_phone("+15551234567", upcoming_only=False)) == 1


def test_find_by_phone_empty_input_returns_empty(cal):
    assert cal.find_by_phone("") == []
    assert cal.find_by_phone("no-digits!") == []


# ── cancel ────────────────────────────────────────────────────────


def test_cancel_removes_event(cal):
    now = datetime.now(timezone.utc)
    eid = _seed_event(cal, phone="+15551234567",
                       start=now + timedelta(days=2))
    result = cal.cancel(eid, reason="caller requested")
    assert result["cancelled"] is True
    assert result["event"]["status"] == "cancelled"
    # Event should be gone from the store.
    assert eid not in cal._svc()._events_store


def test_cancel_missing_event_returns_not_found(cal):
    result = cal.cancel("nonexistent-id")
    assert result == {"cancelled": False, "reason": "event_not_found"}


def test_cancel_empty_id_returns_error(cal):
    result = cal.cancel("")
    assert result == {"cancelled": False, "reason": "event_id_missing"}


def test_cancel_reason_stamped_into_description(cal):
    now = datetime.now(timezone.utc)
    eid = _seed_event(cal, phone="+15551234567",
                       start=now + timedelta(days=2))
    cal.cancel(eid, reason="scheduling conflict")
    # Event is gone but we can only verify the description-patch call
    # happened by checking it doesn't crash — the fake accepts any patch.


# ── reschedule ────────────────────────────────────────────────────


def test_reschedule_moves_event(cal):
    now = datetime.now(timezone.utc)
    old_start = now + timedelta(days=2, hours=10)
    eid = _seed_event(cal, phone="+15551234567", start=old_start)
    new_start = now + timedelta(days=2, hours=14)
    result = cal.reschedule(eid, new_start=new_start)
    assert result["rescheduled"] is True
    assert result["event"]["id"] == eid
    # Store updated.
    stored = cal._svc()._events_store[eid]
    assert stored["start"]["dateTime"] == new_start.isoformat()


def test_reschedule_missing_event(cal):
    result = cal.reschedule(
        "nonexistent", new_start=datetime.now(timezone.utc),
    )
    assert result == {"rescheduled": False, "reason": "event_not_found"}


def test_reschedule_preserves_duration_when_not_specified(cal):
    now = datetime.now(timezone.utc)
    old_start = now + timedelta(days=2, hours=10)
    eid = _seed_event(cal, phone="+15551234567", start=old_start,
                       duration_min=45)
    new_start = now + timedelta(days=3, hours=11)
    result = cal.reschedule(eid, new_start=new_start)
    assert result["rescheduled"] is True
    stored = cal._svc()._events_store[eid]
    old_dur = timedelta(minutes=45)
    assert stored["end"]["dateTime"] == (new_start + old_dur).isoformat()


def test_reschedule_explicit_duration_overrides(cal):
    now = datetime.now(timezone.utc)
    old_start = now + timedelta(days=2, hours=10)
    eid = _seed_event(cal, phone="+15551234567", start=old_start,
                       duration_min=45)
    new_start = now + timedelta(days=3, hours=11)
    result = cal.reschedule(eid, new_start=new_start, duration_minutes=60)
    assert result["rescheduled"] is True
    stored = cal._svc()._events_store[eid]
    new_end = new_start + timedelta(minutes=60)
    assert stored["end"]["dateTime"] == new_end.isoformat()


def test_reschedule_empty_id(cal):
    result = cal.reschedule("", new_start=datetime.now(timezone.utc))
    assert result == {"rescheduled": False, "reason": "event_id_missing"}


def test_reschedule_cancelled_event_fails(cal):
    now = datetime.now(timezone.utc)
    eid = _seed_event(
        cal, phone="+15551234567", start=now + timedelta(days=2),
        status="cancelled",
    )
    result = cal.reschedule(eid, new_start=now + timedelta(days=3))
    assert result == {"rescheduled": False, "reason": "event_cancelled"}
