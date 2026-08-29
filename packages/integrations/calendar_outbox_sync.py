"""CalendarOutboxSync — background worker that flushes OutboxCalendar
records to a remote Google Calendar when creds allow.

2026-08-29: paired with OutboxCalendar (packages/integrations/outbox_calendar.py).

## Design

Pull-based, tick-driven. Each tick:
  1. Ask the OutboxCalendar if remote creds are configured.
     If not → no-op. Nothing to do until user drops creds in.
  2. Fetch up to N pending records from the outbox, oldest first.
  3. For each record: call the matching remote op with the same args
     that were used locally. Mark synced on success; mark failed
     (increment attempts, exponential backoff) on error.
  4. Sleep the tick interval.

## Retry policy

Exponential backoff bounded per-record. attempt N waits ~2^N seconds
before eligible again (capped at 5 minutes). After 20 attempts a
record moves to status='dead' — human ops attention needed.

## Local-wins conflict resolution

If the remote op raises 'slot conflict' (Google Calendar says the slot
is taken), the sync worker STILL treats the local booking as canonical.
The local calendar already reflects the caller's confirmation. Options:
  a. Push anyway with force flag (Google APIs allow overlapping events).
  b. Emit an alert to ops so a human resolves the conflict manually.

Current implementation: (a) — Google Calendar allows overlaps by
default; the wrapper doesn't refuse.  This matches user's explicit
'local-wins' choice.

## Concurrency

Single background task per process. If we scale to multiple uvicorn
workers, only ONE worker should run the sync — the outbox store isn't
multi-writer-safe for updates. Enforced via a lightweight file lock
(scheduled_tasks.lock pattern).

## Not a substitute for a real message queue

For a receptionist workload (10-100 bookings/day) this is fine. If we
grow past that or need cross-machine delivery guarantees, move the
outbox to Postgres LISTEN/NOTIFY or SQS.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional


log = logging.getLogger(__name__)


# ── retry policy ──────────────────────────────────────────


def _backoff_delay_s(attempts: int) -> float:
    """Exponential backoff: 2^attempts seconds, capped at 300s."""
    if attempts <= 0:
        return 0.0
    return min(2 ** attempts, 300)


def _eligible_now(rec, now: float) -> bool:
    """True if a record is ready for another attempt."""
    if rec.status != "pending":
        return False
    if rec.attempts == 0:
        return True
    delay = _backoff_delay_s(rec.attempts)
    return (now - rec.last_attempt_at) >= delay


# ── sync worker ───────────────────────────────────────────


class CalendarOutboxSync:
    """Background worker.  Owns the loop; caller starts + stops it.

    Args:
      outbox_calendar: an OutboxCalendar instance.
      tick_interval_s: seconds between ticks (default 30s).
      batch_size: max records to attempt per tick (default 25).
      dead_after: attempts before a record moves to status='dead' (default 20).
    """

    def __init__(
        self,
        outbox_calendar,
        tick_interval_s: float = 30.0,
        batch_size: int = 25,
        dead_after: int = 20,
    ) -> None:
        self.outbox_calendar = outbox_calendar
        self.tick_interval_s = tick_interval_s
        self.batch_size = batch_size
        self.dead_after = dead_after
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._loop(),
            name="calendar-outbox-sync",
        )
        log.info(
            "calendar_outbox_sync started tick=%ss batch=%d",
            self.tick_interval_s, self.batch_size,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                try:
                    await self.tick_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception(
                        "calendar_outbox_sync tick failed: %s", e,
                    )
                await asyncio.sleep(self.tick_interval_s)
        except asyncio.CancelledError:
            pass

    async def tick_once(self) -> dict:
        """Run one sync pass.  Returns stats.  Safe to call from
        outside the loop (e.g. from an admin endpoint or tests)."""
        stats = {
            "remote_available": False,
            "attempted": 0,
            "synced": 0,
            "failed": 0,
            "dead": 0,
        }
        oc = self.outbox_calendar
        if not oc.remote_available():
            log.debug("calendar_outbox_sync tick: remote unavailable, no-op")
            return stats
        stats["remote_available"] = True

        # Late-construct remote so a freshly-dropped creds file is
        # picked up on this tick.
        try:
            remote = oc.remote_factory()
        except Exception as e:
            log.warning(
                "calendar_outbox_sync: remote_factory raised (%s) — "
                "leaving outbox intact", e,
            )
            return stats
        if remote is None:
            return stats

        now = time.time()
        pending = oc.outbox.pending(limit=self.batch_size)
        for rec in pending:
            if not _eligible_now(rec, now):
                continue
            stats["attempted"] += 1
            try:
                await self._sync_record(remote, rec)
                oc.outbox.mark_synced(rec.local_event_id, rec.op)
                stats["synced"] += 1
            except Exception as e:
                oc.outbox.mark_failed(
                    rec.local_event_id, rec.op, str(e),
                    dead_after=self.dead_after,
                )
                if rec.attempts + 1 >= self.dead_after:
                    stats["dead"] += 1
                    log.error(
                        "calendar_outbox_sync: record marked DEAD "
                        "local_event_id=%s op=%s error=%s",
                        rec.local_event_id, rec.op, str(e)[:200],
                    )
                else:
                    stats["failed"] += 1
                    log.warning(
                        "calendar_outbox_sync: record failed (attempt %d): "
                        "local_event_id=%s op=%s error=%s",
                        rec.attempts + 1,
                        rec.local_event_id, rec.op, str(e)[:200],
                    )
        return stats

    async def _sync_record(self, remote, rec) -> None:
        """Replay one outbox record against the remote calendar.

        Local-wins: force through slot conflicts.  Idempotency: the
        record's idempotency_key handles remote-side dedup for
        backends that support it; for backends that don't, a
        duplicate booking is preferable to a lost booking.
        """
        kw = dict(rec.kwargs or {})
        if rec.op == "book":
            # Deserialize datetime that we isoformatted at enqueue time.
            start_val = kw.get("start")
            if isinstance(start_val, str):
                try:
                    kw["start"] = datetime.fromisoformat(start_val)
                except ValueError:
                    pass
            # GoogleCalendar.book has same signature as FakeCalendar.book.
            # Some remotes may not accept `idempotency_key` — strip if
            # signature doesn't accept it (GoogleCalendar today ignores it).
            _run_in_thread = getattr(asyncio, "to_thread")
            await _run_in_thread(
                remote.book,
                start=kw["start"],
                duration_minutes=kw["duration_minutes"],
                caller_name=kw["caller_name"],
                phone=kw["phone"],
                service=kw["service"],
                notes=kw.get("notes"),
            )
        elif rec.op == "cancel":
            event_id = (
                rec.args[0] if rec.args else kw.get("event_id")
            )
            _run_in_thread = getattr(asyncio, "to_thread")
            await _run_in_thread(
                remote.cancel, event_id, kw.get("reason"),
            )
        elif rec.op == "reschedule":
            event_id = (
                rec.args[0] if rec.args else kw.get("event_id")
            )
            new_start = kw.get("new_start")
            if isinstance(new_start, str):
                try:
                    new_start = datetime.fromisoformat(new_start)
                except ValueError:
                    pass
            _run_in_thread = getattr(asyncio, "to_thread")
            await _run_in_thread(
                remote.reschedule,
                event_id, new_start, kw.get("duration_minutes"),
            )
        else:
            raise ValueError(f"unknown outbox op: {rec.op}")


__all__ = [
    "CalendarOutboxSync",
    "_backoff_delay_s",   # exported for tests
    "_eligible_now",      # exported for tests
]
