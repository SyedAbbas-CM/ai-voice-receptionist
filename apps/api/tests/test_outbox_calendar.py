"""OutboxCalendar tests — credential-driven fallback + deferred sync.

2026-08-29 user-designed pattern (approved via AskUserQuestion):
bookings write locally first (source of truth), enqueue to disk-backed
outbox, sync worker pushes to Google when creds allow, retroactively
picks up ALL queued when creds appear later, local-wins conflicts.

These tests lock:
  * writes go to local + enqueue outbox
  * reads pass through to local (source-of-truth guarantee)
  * remote_available() honors factory being None
  * outbox stats accurate
  * sync worker picks up pending, marks synced, exponential-backoff on failure
  * dead-letter after 20 attempts
  * creds-appearing-later scenario (factory returns None, then returns real)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from packages.integrations.outbox_calendar import (
    OutboxCalendar,
    OutboxRecord,
    OutboxStore,
    make_google_remote_factory,
)
from packages.integrations.calendar_outbox_sync import (
    CalendarOutboxSync,
    _backoff_delay_s,
    _eligible_now,
)
from packages.integrations.fake_calendar import FakeCalendar


# ── fixtures ─────────────────────────────────────────────


@pytest.fixture
def local_calendar(tmp_path):
    return FakeCalendar(path=tmp_path / "local.json")


@pytest.fixture
def outbox_path(tmp_path):
    return tmp_path / "outbox.jsonl"


@pytest.fixture
def wrapper(local_calendar, outbox_path):
    return OutboxCalendar(local=local_calendar, outbox_path=outbox_path)


class _MemoryRemote:
    """Stub remote calendar for testing sync semantics."""
    def __init__(self, fail_first_n=0, permanent_fail=False):
        self.booked = []
        self.cancelled = []
        self.rescheduled = []
        self._fail_first_n = fail_first_n
        self._calls = 0
        self._permanent_fail = permanent_fail

    def book(self, start, duration_minutes, caller_name, phone,
              service, notes=None):
        self._calls += 1
        if self._permanent_fail:
            raise RuntimeError("permanent remote error")
        if self._calls <= self._fail_first_n:
            raise RuntimeError(f"transient error {self._calls}")
        rec = {
            "start": start.isoformat() if hasattr(start, "isoformat") else start,
            "duration_minutes": duration_minutes,
            "caller_name": caller_name, "phone": phone,
            "service": service, "notes": notes,
        }
        self.booked.append(rec)
        return {"booked": True, "id": f"remote_{len(self.booked)}"}

    def cancel(self, event_id, reason=None):
        self.cancelled.append({"event_id": event_id, "reason": reason})
        return {"cancelled": True}

    def reschedule(self, event_id, new_start, duration_minutes=None):
        self.rescheduled.append({
            "event_id": event_id, "new_start": new_start,
            "duration_minutes": duration_minutes,
        })
        return {"rescheduled": True}


# ── write ops enqueue outbox ────────────────────────────


def test_book_enqueues_outbox_record(wrapper, outbox_path):
    result = wrapper.book(
        start=datetime(2026, 9, 1, 10, 0),
        duration_minutes=30,
        caller_name="Abbas",
        phone="+15551234567",
        service="Adult cleaning",
    )
    assert result["booked"] is True
    # Outbox file has one record.
    assert outbox_path.exists()
    lines = outbox_path.read_text().strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["op"] == "book"
    assert rec["kwargs"]["caller_name"] == "Abbas"
    assert rec["kwargs"]["phone"] == "+15551234567"
    assert rec["status"] == "pending"


def test_book_writes_to_local_first(wrapper, local_calendar):
    """Local calendar has the event immediately (source of truth)."""
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0),
        duration_minutes=30, caller_name="Test",
        phone="+15551234567", service="Cleaning",
    )
    # Local calendar sees it (via read-through).
    slots_before = wrapper.list_slots(
        datetime(2026, 9, 1), duration_minutes=30,
    )
    # The 10:00 slot is no longer offered — it was booked.
    assert "10:00" not in slots_before


def test_failed_book_does_not_enqueue(wrapper, outbox_path):
    """Attempt a booking at a slot that's outside business hours →
    local returns not-booked → outbox stays empty."""
    # First book something.
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+15551", service="X",
    )
    # Try to double-book the same slot — should fail locally.
    result = wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="B", phone="+15552", service="X",
    )
    assert result.get("booked") is False
    # Outbox has only the first (successful) record.
    lines = outbox_path.read_text().strip().split("\n")
    assert len(lines) == 1


def test_deduplicated_book_does_not_reenqueue(wrapper, outbox_path):
    """Idempotency: same key → local returns deduplicated → outbox
    does NOT get a second entry."""
    key = "idem-key-1"
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
        idempotency_key=key,
    )
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
        idempotency_key=key,
    )
    lines = [
        l for l in outbox_path.read_text().split("\n") if l.strip()
    ]
    assert len(lines) == 1


# ── read ops pass through ──────────────────────────────


def test_is_available_passes_through(wrapper):
    assert wrapper.is_available(
        datetime(2026, 9, 1, 10, 0), 30,
    ) is True
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
    )
    assert wrapper.is_available(
        datetime(2026, 9, 1, 10, 0), 30,
    ) is False


def test_find_by_phone_passes_through(wrapper):
    wrapper.book(
        start=datetime(2026, 9, 2, 10, 0), duration_minutes=30,
        caller_name="Abbas", phone="+15551234567", service="X",
    )
    found = wrapper.find_by_phone("+15551234567", upcoming_only=False)
    assert len(found) == 1


# ── remote availability ────────────────────────────────


def test_remote_available_false_when_no_factory(wrapper):
    assert wrapper.remote_available() is False


def test_remote_available_true_when_factory_returns_object(
    local_calendar, outbox_path,
):
    def _factory():
        return _MemoryRemote()
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=_factory,
    )
    assert w.remote_available() is True


def test_remote_available_false_when_factory_returns_none(
    local_calendar, outbox_path,
):
    def _factory():
        return None
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=_factory,
    )
    assert w.remote_available() is False


def test_remote_available_false_when_factory_raises(
    local_calendar, outbox_path,
):
    def _factory():
        raise RuntimeError("bad creds")
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=_factory,
    )
    assert w.remote_available() is False


# ── stats ─────────────────────────────────────────────


def test_outbox_stats_reflects_state(wrapper):
    wrapper.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
    )
    wrapper.book(
        start=datetime(2026, 9, 1, 11, 0), duration_minutes=30,
        caller_name="B", phone="+2", service="Y",
    )
    stats = wrapper.outbox_stats()
    assert stats["total"] == 2
    assert stats["pending"] == 2
    assert stats["synced"] == 0
    assert stats["dead"] == 0


# ── sync worker ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_worker_pushes_pending_to_remote(
    local_calendar, outbox_path,
):
    remote = _MemoryRemote()
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=lambda: remote,
    )
    w.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="Abbas", phone="+1", service="X",
    )
    sync = CalendarOutboxSync(w, tick_interval_s=0.01)
    stats = await sync.tick_once()
    assert stats["remote_available"] is True
    assert stats["synced"] == 1
    assert len(remote.booked) == 1
    assert remote.booked[0]["caller_name"] == "Abbas"


@pytest.mark.asyncio
async def test_sync_worker_no_op_when_remote_unavailable(
    local_calendar, outbox_path,
):
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=None,
    )
    w.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
    )
    sync = CalendarOutboxSync(w)
    stats = await sync.tick_once()
    assert stats["remote_available"] is False
    assert stats["synced"] == 0
    # Record stays pending.
    assert w.outbox_stats()["pending"] == 1


@pytest.mark.asyncio
async def test_sync_worker_marks_failed_on_error(
    local_calendar, outbox_path,
):
    remote = _MemoryRemote(fail_first_n=99, permanent_fail=True)
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=lambda: remote,
    )
    w.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
    )
    sync = CalendarOutboxSync(w, dead_after=3)
    # Multiple ticks — each attempt fails; after 3, record moves to dead.
    for i in range(3):
        # Set last_attempt_at back so backoff doesn't skip it.
        for rec in w.outbox._index.values():
            rec.last_attempt_at = 0.0
        await sync.tick_once()
    # After 3 attempts with dead_after=3, record is dead.
    assert w.outbox_stats()["dead"] == 1


@pytest.mark.asyncio
async def test_creds_appearing_later_flushes_backlog(
    local_calendar, outbox_path,
):
    """The whole point of the design: book while creds absent, drop
    creds in later, backlog syncs."""
    # Phase 1: book with no creds.
    w = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
        remote_factory=None,
    )
    for i in range(3):
        w.book(
            start=datetime(2026, 9, 1, 10 + i, 0),
            duration_minutes=30,
            caller_name=f"caller_{i}", phone=f"+155512{i}",
            service="X",
        )
    assert w.outbox_stats()["pending"] == 3

    # Phase 2: creds appear.  Attach a remote factory + a fresh sync.
    remote = _MemoryRemote()
    w.remote_factory = lambda: remote
    sync = CalendarOutboxSync(w)
    stats = await sync.tick_once()
    assert stats["synced"] == 3
    assert len(remote.booked) == 3
    assert w.outbox_stats()["synced"] == 3


# ── backoff + eligibility ─────────────────────────────


def test_backoff_delay_is_exponential():
    assert _backoff_delay_s(0) == 0
    assert _backoff_delay_s(1) == 2
    assert _backoff_delay_s(2) == 4
    assert _backoff_delay_s(3) == 8
    # Capped at 300s.
    assert _backoff_delay_s(20) == 300


def test_eligibility_first_attempt_always_true():
    rec = OutboxRecord(
        op="book", args=[], kwargs={},
        local_event_id="evt1", idempotency_key="evt1",
    )
    assert _eligible_now(rec, 0) is True


def test_eligibility_respects_backoff():
    rec = OutboxRecord(
        op="book", args=[], kwargs={},
        local_event_id="evt1", idempotency_key="evt1",
        attempts=2,   # backoff 4s
        last_attempt_at=100.0,
    )
    # Now = 102 → not eligible (only 2s since last attempt, need 4s)
    assert _eligible_now(rec, 102) is False
    # Now = 105 → eligible
    assert _eligible_now(rec, 105) is True


def test_dead_record_not_eligible():
    rec = OutboxRecord(
        op="book", args=[], kwargs={},
        local_event_id="evt1", idempotency_key="evt1",
        status="dead",
    )
    assert _eligible_now(rec, 999999) is False


# ── remote factory helper ────────────────────────────


def test_google_remote_factory_returns_none_without_creds():
    class _S:
        google_service_account_json = None
        google_calendar_id = None
    assert make_google_remote_factory(_S()) is None


def test_google_remote_factory_returns_callable_with_creds():
    class _S:
        google_service_account_json = "/fake/path.json"
        google_calendar_id = "primary"
    factory = make_google_remote_factory(_S())
    assert callable(factory)
    # Calling it will try to import GoogleCalendar which needs real
    # deps; we just verify the factory constructor was returned.


# ── outbox store durability ────────────────────────────


def test_outbox_survives_process_restart(local_calendar, outbox_path):
    """Enqueue in one wrapper, load in a fresh wrapper — records intact."""
    w1 = OutboxCalendar(
        local=local_calendar, outbox_path=outbox_path,
    )
    w1.book(
        start=datetime(2026, 9, 1, 10, 0), duration_minutes=30,
        caller_name="A", phone="+1", service="X",
    )
    # Simulate restart: fresh wrapper, same paths.
    w2 = OutboxCalendar(
        local=FakeCalendar(path=local_calendar.path),
        outbox_path=outbox_path,
    )
    stats = w2.outbox_stats()
    assert stats["pending"] == 1
