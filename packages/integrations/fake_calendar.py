from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Optional


class FakeCalendar:
    """JSON-file backed calendar for local demos. Same interface a Google
    Calendar adapter would expose, so the brain code doesn't change when
    a client wires up a real calendar."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
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

    def list_slots(self, day: datetime, duration_minutes: int, open_hhmm: str = "09:00", close_hhmm: str = "17:00") -> list[str]:
        open_h, open_m = (int(x) for x in open_hhmm.split(":"))
        close_h, close_m = (int(x) for x in close_hhmm.split(":"))
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
    ) -> dict:
        with self._lock:
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
            }
            events = self._read()
            events.append(event)
            self._write(events)
            return {"booked": True, "event": event}
