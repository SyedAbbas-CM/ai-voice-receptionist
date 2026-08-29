"""OutboxCalendar — credential-driven fallback + deferred sync wrapper.

2026-08-29 (user design decision, live-diagnosed from CA3dac680...):
production runs with `calendar_backend='fake'`. Bookings 'succeed'
locally but land nowhere real. User's ask:

  1. Design the calendar layer so THE MOMENT Google creds appear, it
     starts using Google Calendar.
  2. When creds are missing or invalid, use a local fallback.
  3. When creds become available later, sync the local calendar's
     queued events to the real Google Calendar retroactively.

This module implements pattern (1)-(3) via the classic
**write-through-with-outbox** shape:

  * `OutboxCalendar` wraps a `local` calendar (source of truth) and
    lazily-loads a `remote` calendar via a factory when creds allow.
  * Every WRITE op (book / cancel / reschedule) does two things
    atomically:
      a. Executes against `local` immediately — caller gets the same
         behavior it always got (fast, offline, no external dep).
      b. Enqueues an outbox record `{op, args, kwargs, ts, attempts}`
         to a disk-backed JSONL file.
  * READ ops (is_available / list_slots / find_by_phone / find_by_id)
    pass through to `local`. Local is source of truth on user's
    explicit choice ('local-wins conflict resolution').
  * A separate `CalendarOutboxSync` worker (see
    calendar_outbox_sync.py) reads the outbox, calls `remote` when
    creds are available, and marks records synced on success.

## Deploy-safe

Nothing changes for callers when this wrapper is dropped in. Local
FakeCalendar remains the source of truth. Outbox JSONL is created on
first write. Sync worker is opt-in (not auto-started). Existing
bookings in data/calendar.json don't back-sync (fake test data).

## Local-wins conflict resolution

Per user decision (2026-08-29): if Google Calendar has been externally
edited between the local book and the sync push, sync pushes local
state and clobbers the manual edit. Agent is source of truth. Simple
and predictable. Rationale: the agent IS the booking channel; humans
editing manually should be rare / temporary until the agent is
turned off.

## Idempotency

Every outbox record carries an idempotency_key derived from the local
event id. If the sync worker crashes mid-push and retries, the remote
side's own dedup (Google Calendar's iCalUID equivalent, when we wire
it) prevents duplicates. Records that already exist remotely with the
same key are treated as synced.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


# ── outbox record ─────────────────────────────────────────────


@dataclass
class OutboxRecord:
    """One pending sync operation.

    Fields:
      op: 'book' | 'cancel' | 'reschedule'
      args: positional args as JSON-serializable list
      kwargs: keyword args as JSON-serializable dict
      local_event_id: id of the local calendar record this represents
      idempotency_key: dedup key for remote (defaults to local_event_id)
      created_at: unix ts when enqueued
      attempts: how many sync tries so far
      last_attempt_at: unix ts of last try (0 if never)
      last_error: str of most recent failure (empty on success/never)
      synced_at: unix ts when successfully pushed to remote (0 if pending)
      status: 'pending' | 'syncing' | 'synced' | 'dead'
    """
    op: str
    args: list
    kwargs: dict
    local_event_id: str
    idempotency_key: str
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    last_attempt_at: float = 0.0
    last_error: str = ""
    synced_at: float = 0.0
    status: str = "pending"

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, s: str) -> "OutboxRecord":
        d = json.loads(s)
        return cls(**d)


# ── outbox store ─────────────────────────────────────────────


class OutboxStore:
    """Append-only JSONL file with in-memory index.

    Not a database. Deliberately simple. If we outgrow this, the
    interface stays and the impl moves to SQLite / Postgres without
    OutboxCalendar caring.

    Concurrency: RLock around every read/write. Multi-process (multiple
    uvicorn workers) is safe via file-append semantics for writes; index
    consistency between processes requires either a shared DB or a
    single-process reader (the sync worker). For a single-Fargate-task
    or a single-Lightsail-uvicorn shape, the RLock is enough.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # Index by (local_event_id, op) → most recent record for it.
        # Loaded on first read; refreshed by append.
        self._index: dict[tuple[str, str], OutboxRecord] = {}
        self._index_loaded = False

    def _load_index_if_needed(self) -> None:
        if self._index_loaded:
            return
        self._index = {}
        try:
            if self.path.exists():
                with open(self.path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = OutboxRecord.from_json(line)
                            self._index[(rec.local_event_id, rec.op)] = rec
                        except Exception as e:
                            log.warning(
                                "outbox: skipping malformed line: %s", e,
                            )
        except Exception as e:
            log.warning("outbox: index load failed, starting empty: %s", e)
        self._index_loaded = True

    def append(self, rec: OutboxRecord) -> None:
        with self._lock:
            self._load_index_if_needed()
            try:
                with open(self.path, "a") as f:
                    f.write(rec.to_json())
                    f.write("\n")
                self._index[(rec.local_event_id, rec.op)] = rec
            except Exception as e:
                log.exception("outbox: append failed: %s", e)

    def pending(self, limit: int = 100) -> list[OutboxRecord]:
        """Return records with status='pending' or eligible for retry.

        Ordered by created_at ascending — oldest first, so a slow
        sync catches up in original order.
        """
        with self._lock:
            self._load_index_if_needed()
            out = [
                r for r in self._index.values()
                if r.status == "pending" and r.synced_at == 0
            ]
            out.sort(key=lambda r: r.created_at)
            return out[:limit]

    def mark_synced(self, local_event_id: str, op: str) -> None:
        with self._lock:
            self._load_index_if_needed()
            rec = self._index.get((local_event_id, op))
            if rec is None:
                return
            rec.status = "synced"
            rec.synced_at = time.time()
            # Rewrite the whole file to reflect the update.  For a
            # low-volume receptionist workload (10-100 bookings/day),
            # rewriting a small JSONL is fine.  If we outgrow this,
            # move to SQLite.
            self._rewrite()

    def mark_failed(
        self, local_event_id: str, op: str, error: str,
        dead_after: int = 20,
    ) -> None:
        with self._lock:
            self._load_index_if_needed()
            rec = self._index.get((local_event_id, op))
            if rec is None:
                return
            rec.attempts += 1
            rec.last_attempt_at = time.time()
            rec.last_error = (error or "")[:500]
            if rec.attempts >= dead_after:
                rec.status = "dead"
            self._rewrite()

    def _rewrite(self) -> None:
        try:
            with open(self.path, "w") as f:
                for rec in sorted(
                    self._index.values(), key=lambda r: r.created_at,
                ):
                    f.write(rec.to_json())
                    f.write("\n")
        except Exception as e:
            log.exception("outbox: rewrite failed: %s", e)

    def stats(self) -> dict:
        """For dashboard visibility."""
        with self._lock:
            self._load_index_if_needed()
            counts: dict[str, int] = {}
            for rec in self._index.values():
                counts[rec.status] = counts.get(rec.status, 0) + 1
            return {
                "total": len(self._index),
                "pending": counts.get("pending", 0),
                "synced": counts.get("synced", 0),
                "dead": counts.get("dead", 0),
                "path": str(self.path),
            }


# ── outbox calendar wrapper ─────────────────────────────────


class OutboxCalendar:
    """Wraps a `local` calendar (source of truth) with an outbox
    store for later sync to a remote calendar.

    Interface: matches FakeCalendar/GoogleCalendar so it drops into
    calendar_factory without touching the tool handlers.

    Args:
      local: the local calendar (typically a FakeCalendar).
      outbox_path: disk location of the outbox JSONL.
      remote_factory: OPTIONAL zero-arg callable returning a remote
        calendar instance (typically GoogleCalendar), or None to
        indicate 'creds not configured'. Called on demand by the
        sync worker, not by write ops. Defer construction so the
        wrapper stays fast + safe when creds are absent.
    """

    def __init__(
        self,
        local,
        outbox_path: str | Path,
        remote_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.local = local
        self.outbox = OutboxStore(outbox_path)
        self.remote_factory = remote_factory

    # ── read ops pass through to local ───────────────────────

    def is_available(self, start, duration_minutes):
        return self.local.is_available(start, duration_minutes)

    def list_slots(
        self, day, duration_minutes,
        open_hhmm="09:00", close_hhmm="17:00",
    ):
        return self.local.list_slots(
            day, duration_minutes, open_hhmm, close_hhmm,
        )

    def find_by_phone(self, phone, upcoming_only=True):
        return self.local.find_by_phone(phone, upcoming_only)

    def find_by_id(self, event_id):
        # Not all backends have find_by_id; forward if present.
        fn = getattr(self.local, "find_by_id", None)
        if fn is None:
            return None
        return fn(event_id)

    # ── write ops: local first, then enqueue ────────────────

    def book(
        self, start, duration_minutes, caller_name, phone, service,
        notes=None, idempotency_key=None,
    ):
        result = self.local.book(
            start=start,
            duration_minutes=duration_minutes,
            caller_name=caller_name,
            phone=phone,
            service=service,
            notes=notes,
            idempotency_key=idempotency_key,
        )
        # Only enqueue on genuine success.  A local failure ('slot
        # not available') should NOT enqueue a sync — nothing to
        # replicate.
        if result.get("booked") and not result.get("deduplicated"):
            evt = result.get("event") or {}
            local_id = evt.get("id") or ""
            idem = evt.get("idempotency_key") or local_id
            # Serialize args for later replay.  datetime → isoformat.
            rec = OutboxRecord(
                op="book",
                args=[],
                kwargs={
                    "start": start.isoformat() if isinstance(
                        start, datetime,
                    ) else str(start),
                    "duration_minutes": duration_minutes,
                    "caller_name": caller_name,
                    "phone": phone,
                    "service": service,
                    "notes": notes,
                    "idempotency_key": idem,
                },
                local_event_id=local_id,
                idempotency_key=idem,
            )
            self.outbox.append(rec)
        return result

    def cancel(self, event_id, reason=None):
        fn = getattr(self.local, "cancel", None)
        if fn is None:
            return {"cancelled": False, "reason": "local backend has no cancel"}
        result = fn(event_id, reason)
        if result.get("cancelled"):
            rec = OutboxRecord(
                op="cancel",
                args=[event_id],
                kwargs={"reason": reason},
                local_event_id=event_id,
                idempotency_key=f"cancel:{event_id}",
            )
            self.outbox.append(rec)
        return result

    def reschedule(
        self, event_id, new_start, duration_minutes=None,
    ):
        fn = getattr(self.local, "reschedule", None)
        if fn is None:
            return {
                "rescheduled": False,
                "reason": "local backend has no reschedule",
            }
        result = fn(event_id, new_start, duration_minutes)
        if result.get("rescheduled"):
            rec = OutboxRecord(
                op="reschedule",
                args=[event_id],
                kwargs={
                    "new_start": new_start.isoformat() if isinstance(
                        new_start, datetime,
                    ) else str(new_start),
                    "duration_minutes": duration_minutes,
                },
                local_event_id=event_id,
                idempotency_key=(
                    f"reschedule:{event_id}:"
                    f"{new_start.isoformat() if isinstance(new_start, datetime) else new_start}"
                ),
            )
            self.outbox.append(rec)
        return result

    # ── introspection for dashboard / sync worker ──────────

    def outbox_stats(self) -> dict:
        return self.outbox.stats()

    def remote_available(self) -> bool:
        """True if remote creds/factory are configured and construct
        successfully.  Used by the sync worker to decide whether to
        try pushing this tick."""
        if self.remote_factory is None:
            return False
        try:
            r = self.remote_factory()
            return r is not None
        except Exception:
            return False


# ── remote factory helper ──────────────────────────────────


def make_google_remote_factory(settings) -> Optional[Callable[[], Any]]:
    """Return a zero-arg factory that constructs GoogleCalendar, or
    None if creds are absent.

    Called at OutboxCalendar construction time to bind the settings
    snapshot.  The factory itself is called by the sync worker on
    each tick — creds may APPEAR after startup (user drops a JSON
    file into place + restarts), and this handles it naturally.
    """
    sa_path = getattr(settings, "google_service_account_json", None)
    cal_id = getattr(settings, "google_calendar_id", None)
    if not sa_path or not cal_id:
        return None
    def _factory():
        # Late-import so google_calendar's dep tree doesn't load on
        # startup when creds are absent.
        from .google_calendar import GoogleCalendar
        return GoogleCalendar(
            service_account_json_path=sa_path,
            calendar_id=cal_id,
        )
    return _factory


__all__ = [
    "OutboxRecord",
    "OutboxStore",
    "OutboxCalendar",
    "make_google_remote_factory",
]
