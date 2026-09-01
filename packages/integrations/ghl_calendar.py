"""GoHighLevel calendar backend.

2026-09-01 GHL-wave-2 (part C): before now, the ghl calendar
backend raised NotImplementedError. This activates it so a tenant
can set `integrations.calendar_backend = 'ghl'` and have the agent
read availability from GHL AND write bookings back to GHL — no
local calendar / no sync worker needed.

Uses the existing `GoHighLevelClient.list_free_slots()` and
`.book_appointment()` methods (see packages/integrations/ghl_client.py).

Interface parity with FakeCalendar / GoogleCalendar:
  * `list_slots(day, duration_minutes, ...) -> list[str]`
  * `book(start, duration_minutes, caller_name, phone, service, notes) -> dict`
  * `cancel(event_id, reason) -> dict`
  * `is_available(start, duration_minutes) -> bool`

Notes:
  - GHL calendars have their OWN duration setting per-appointment-type.
    Passing `duration_minutes` here is the DESIRED length; GHL may
    enforce its own slot length. When GHL's returned slots don't
    match `duration_minutes`, we still return them — the caller
    should pick a returned slot.
  - Booking requires a `contact_id`. The brain gives us caller_name +
    phone; we upsert the contact first, then book with its id.
  - GHL calendar API returns slots as ISO strings; we surface them
    as "HH:MM" to match FakeCalendar's shape.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional


log = logging.getLogger(__name__)


class GHLCalendar:
    """GHL calendar adapter that mirrors FakeCalendar / GoogleCalendar."""

    name = "ghl"

    def __init__(
        self,
        client,          # GoHighLevelClient
        calendar_id: Optional[str] = None,
        timezone: str = "America/New_York",
    ) -> None:
        self.client = client
        # Calendar ID falls back to whatever the client has as default
        self.calendar_id = calendar_id or client.default_calendar_id
        if not self.calendar_id:
            raise ValueError(
                "GHLCalendar needs a calendar_id — set "
                "integrations.ghl_calendar_id on the tenant"
            )
        self.timezone = timezone

    # ─── Sync bridge helpers ────────────────────────────────────────────
    # The client is async; brain callers are sync (fake_calendar/
    # google_calendar are sync). Run the async client method on a
    # dedicated event loop when called from sync code.

    def _run_sync(self, coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context but got called sync —
                # dispatch through the running loop.  This happens in
                # tests / any await'd code path.
                return asyncio.run_coroutine_threadsafe(coro, loop).result()
        except RuntimeError:
            pass
        # No running loop → new one just for this call.
        return asyncio.run(coro)

    # ─── Interface: availability ────────────────────────────────────────

    def list_slots(
        self,
        day: datetime,
        duration_minutes: int,
        open_hhmm: str = "09:00",
        close_hhmm: str = "17:00",
    ) -> list[str]:
        """Return HH:MM strings of open slots on `day`, respecting
        open/close hours as a client-side filter over GHL's slot list."""
        start = day.replace(
            hour=int(open_hhmm.split(":")[0]),
            minute=int(open_hhmm.split(":")[1]),
            second=0, microsecond=0,
        )
        end = day.replace(
            hour=int(close_hhmm.split(":")[0]),
            minute=int(close_hhmm.split(":")[1]),
            second=0, microsecond=0,
        )
        try:
            slots = self._run_sync(self.client.list_free_slots(
                calendar_id=self.calendar_id,
                start=start, end=end,
                timezone=self.timezone,
            ))
        except Exception as e:
            log.warning("GHLCalendar.list_slots failed: %s", e)
            return []

        # Slots come back as ISO datetime strings — surface HH:MM only.
        out: list[str] = []
        for s in slots:
            if isinstance(s, str):
                # Try ISO parse; fall back to raw string
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    out.append(dt.strftime("%H:%M"))
                except Exception:
                    out.append(s[:5] if len(s) >= 5 else s)
            elif isinstance(s, dict):
                # Some responses wrap the time in a dict
                t = s.get("time") or s.get("startTime")
                if t:
                    try:
                        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                        out.append(dt.strftime("%H:%M"))
                    except Exception:
                        pass
        # De-dup while preserving order (GHL may return the same slot
        # for multiple providers)
        seen = set()
        unique = []
        for h in out:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        return unique

    def is_available(
        self, start: datetime, duration_minutes: int,
    ) -> bool:
        """Quick check by listing slots for the specific day + matching."""
        slots = self.list_slots(
            start,
            duration_minutes,
            open_hhmm=start.strftime("%H:%M"),
            close_hhmm=(start + timedelta(minutes=duration_minutes + 1)).strftime("%H:%M"),
        )
        return start.strftime("%H:%M") in slots

    # ─── Interface: book ────────────────────────────────────────────────

    def book(
        self,
        start: datetime,
        duration_minutes: int,
        caller_name: str,
        phone: str,
        service: str,
        notes: Optional[str] = None,
    ) -> dict:
        """Upsert the caller as a GHL contact, then create the
        appointment on the GHL calendar."""
        try:
            # 1. Contact upsert (client method is async)
            first, last = self._split_name(caller_name)
            contact = self._run_sync(self.client.upsert_contact(
                phone=phone,
                first_name=first,
                last_name=last,
                source="voiceops-ai-agent",
            ))
            contact_id = contact.get("id") or (
                (contact.get("contact") or {}).get("id")
            )
            if not contact_id:
                log.warning(
                    "GHLCalendar.book: upsert returned no contact id (%r)",
                    contact,
                )
                return {"booked": False, "reason": "GHL contact upsert failed"}

            # 2. Book the appointment
            event = self._run_sync(self.client.book_appointment(
                contact_id=contact_id,
                start=start,
                duration_minutes=duration_minutes,
                title=f"{service} — {caller_name}",
                calendar_id=self.calendar_id,
                notes=notes,
            ))
            return {
                "booked": True,
                "event": {
                    "id": event.get("id") or event.get("appointmentId"),
                    "start": event.get("startTime")
                    or event.get("startTimeIso")
                    or start.isoformat(),
                    "end": event.get("endTime")
                    or (start + timedelta(minutes=duration_minutes)).isoformat(),
                    "caller_name": caller_name,
                    "phone": phone,
                    "service": service,
                    "notes": notes,
                    "contact_id": contact_id,
                },
            }
        except Exception as e:
            log.warning("GHLCalendar.book failed: %s", e)
            return {"booked": False, "reason": f"GHL book_appointment: {e}"}

    # ─── Interface: cancel (best-effort) ────────────────────────────────

    def cancel(self, event_id: str, reason: Optional[str] = None) -> dict:
        """GHL supports appointment cancellation via PUT /calendars/events/appointments/{id}.

        Our GoHighLevelClient doesn't wrap this yet — surface a
        not-implemented result so the brain can fall back to a
        callback / escalation rather than raising. Add the client
        method when cancellations become common.
        """
        log.info(
            "GHLCalendar.cancel not implemented — surface manual "
            "cancellation for event=%s reason=%s",
            event_id, reason,
        )
        return {
            "cancelled": False,
            "reason": "GHL cancellation not wired — please cancel in GHL directly",
        }

    # ─── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _split_name(full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not full:
            return None, None
        parts = full.strip().split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None
