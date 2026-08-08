from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from packages.schemas.business import BusinessHours


_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


class FakeCalendar:
    """JSON-file backed calendar for local demos. Same interface a Google
    Calendar adapter would expose, so the brain code doesn't change when
    a client wires up a real calendar.

    Audit-3 fix (2026-08-04): accepts an optional `hours: BusinessHours`
    so list_slots honours the business profile.  Previously hardcoded to
    Mon-Fri 09:00-17:00, which contradicted profiles like Smile Dental
    (later Thursday, weekend hours, etc) — the agent would tell callers
    the clinic was closed while the availability tool offered slots."""

    def __init__(
        self,
        path: Path | str,
        hours: Optional["BusinessHours"] = None,
    ) -> None:
        self.path = Path(path)
        self.hours = hours
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write(self, events: list[dict]) -> None:
        self.path.write_text(json.dumps(events, indent=2, default=str))

    def is_available(self, start: datetime, duration_minutes: int) -> bool:
        end = start + timedelta(minutes=duration_minutes)
        with self._lock:
            for ev in self._read():
                ev_start = datetime.fromisoformat(ev["start"])
                ev_end = datetime.fromisoformat(ev["end"])
                if start < ev_end and end > ev_start:
                    return False
        return True

    def _hours_for_day(self, day: datetime) -> Optional[tuple[str, str]]:
        """Look up (open_hhmm, close_hhmm) for the given day from
        self.hours if provided.  Returns None if the business is closed
        that weekday or the hours string is malformed."""
        if self.hours is None:
            return None
        day_name = _WEEKDAY_NAMES[day.weekday()]
        window = getattr(self.hours, day_name, None)
        if not window:
            return None
        try:
            start, end = window.split("-")
            return start.strip(), end.strip()
        except ValueError:
            return None

    def list_slots(
        self,
        day: datetime,
        duration_minutes: int,
        open_hhmm: str = "09:00",
        close_hhmm: str = "17:00",
    ) -> list[str]:
        """List open slots for the requested day.

        Audit-3 fix (2026-08-04): if the FakeCalendar was constructed
        with a BusinessHours object, that overrides the 9-5 default —
        the availability tool now agrees with what the spoken policy
        says about the clinic's hours.  Explicit args to this method
        still win over the profile (used only when the caller supplies
        a non-standard window)."""
        window = self._hours_for_day(day)
        if window is not None:
            open_hhmm, close_hhmm = window
        else:
            # No profile hours for this day (or business closed).
            # Only fall back to the 9-5 default when NO hours were
            # provided at all; if hours were provided and this day is
            # empty (business closed), return no slots.
            if self.hours is not None:
                return []

        try:
            open_h, open_m = (int(x) for x in open_hhmm.split(":"))
            close_h, close_m = (int(x) for x in close_hhmm.split(":"))
        except (ValueError, AttributeError):
            return []
        cursor = day.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        end_of_day = day.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        slots: list[str] = []
        while cursor + timedelta(minutes=duration_minutes) <= end_of_day:
            if self.is_available(cursor, duration_minutes):
                slots.append(cursor.strftime("%H:%M"))
            cursor += timedelta(minutes=duration_minutes)
        return slots

    def book(
        self,
        start: datetime,
        duration_minutes: int,
        caller_name: str,
        phone: str,
        service: str,
        notes: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Book an appointment.

        Sprint 10 Track B2: `idempotency_key` (optional).  When set, a
        second call with the same key returns the ORIGINAL event
        instead of creating a duplicate.  This is what the
        CommitCoordinator's dedup relies on."""
        with self._lock:
            events = self._read()
            if idempotency_key:
                for ev in events:
                    if ev.get("idempotency_key") == idempotency_key:
                        return {"booked": True, "event": ev, "deduplicated": True}
            if not self.is_available(start, duration_minutes):
                return {"booked": False, "reason": "slot no longer available"}
            event = {
                "id": f"evt_{int(start.timestamp())}",
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=duration_minutes)).isoformat(),
                "caller_name": caller_name,
                "phone": phone,
                "service": service,
                "notes": notes,
                "status": "confirmed",
            }
            if idempotency_key:
                event["idempotency_key"] = idempotency_key
            events.append(event)
            self._write(events)
            return {"booked": True, "event": event}

    # ── Sprint 10 Track B2: appointment lifecycle ────────────────────

    def find_by_phone(self, phone: str, upcoming_only: bool = True) -> list[dict]:
        """Return events for the given caller phone.

        `upcoming_only=True` filters out past events — the normal
        lookup during a caller wanting to change something."""
        now = datetime.now()
        results = []
        with self._lock:
            for ev in self._read():
                if ev.get("phone") != phone:
                    continue
                if ev.get("status") == "cancelled":
                    continue
                if upcoming_only:
                    try:
                        if datetime.fromisoformat(ev["end"]) < now:
                            continue
                    except (ValueError, KeyError):
                        continue
                results.append(ev)
        return results

    def find_by_id(self, event_id: str) -> Optional[dict]:
        with self._lock:
            for ev in self._read():
                if ev.get("id") == event_id:
                    return ev
        return None

    def cancel(self, event_id: str, reason: Optional[str] = None) -> dict:
        """Cancel an appointment.  Idempotent: cancelling an already-
        cancelled event returns success with a dedup flag."""
        with self._lock:
            events = self._read()
            for ev in events:
                if ev.get("id") == event_id:
                    if ev.get("status") == "cancelled":
                        return {"cancelled": True, "event": ev, "deduplicated": True}
                    ev["status"] = "cancelled"
                    if reason:
                        ev["cancel_reason"] = reason
                    self._write(events)
                    return {"cancelled": True, "event": ev}
            return {"cancelled": False, "reason": "event_not_found"}

    def reschedule(
        self,
        event_id: str,
        new_start: datetime,
        duration_minutes: Optional[int] = None,
    ) -> dict:
        """Move an existing appointment to a new start time.

        Uses same conflict check as book().  Atomic under the lock:
        checks new slot free, mutates event.  On conflict, ORIGINAL
        event is unchanged and the caller can pick another slot."""
        with self._lock:
            events = self._read()
            target = next((e for e in events if e.get("id") == event_id), None)
            if target is None:
                return {"rescheduled": False, "reason": "event_not_found"}
            if target.get("status") == "cancelled":
                return {"rescheduled": False, "reason": "event_cancelled"}
            try:
                old_start = datetime.fromisoformat(target["start"])
                old_end = datetime.fromisoformat(target["end"])
            except (ValueError, KeyError):
                return {"rescheduled": False, "reason": "event_corrupt"}
            dur = duration_minutes or int((old_end - old_start).total_seconds() / 60)
            new_end = new_start + timedelta(minutes=dur)
            # Availability check must ignore the current event's slot
            for other in events:
                if other.get("id") == event_id or other.get("status") == "cancelled":
                    continue
                try:
                    o_start = datetime.fromisoformat(other["start"])
                    o_end = datetime.fromisoformat(other["end"])
                except (ValueError, KeyError):
                    continue
                if new_start < o_end and new_end > o_start:
                    return {"rescheduled": False, "reason": "slot no longer available"}
            target["start"] = new_start.isoformat()
            target["end"] = new_end.isoformat()
            target["previous_start"] = old_start.isoformat()
            self._write(events)
            return {"rescheduled": True, "event": target}
