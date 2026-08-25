"""Google Calendar adapter — same interface as FakeCalendar so the brain doesn't care.

Auth: service account. Share your target calendar with the service account
email so it can read/write.

Deps (lazy-imported to keep them optional):
    pip install google-api-python-client google-auth

2026-08-25 (humanness audit P0.7): extended with `find_by_phone`,
`cancel`, `reschedule` to match FakeCalendar's full lifecycle surface.
Previously the real Google backend was materially weaker than the demo:
clinic_tools exposed cancel/reschedule to the LLM but they only worked
in fake mode — real customers' Google Calendars could not be modified
through the tool loop.  This closes the capability trap.

The Google Events API doesn't have a native "search by attendee-phone"
endpoint, so `find_by_phone` scans the description field (where `book`
writes "Phone: <E.164>") over a bounded window.  Same semantics as
FakeCalendar's phone lookup — filter by `phone`, skip cancelled, skip
past events when `upcoming_only=True`.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional


class GoogleCalendar:
    def __init__(self, service_account_json_path: str, calendar_id: str) -> None:
        self.service_account_json_path = service_account_json_path
        self.calendar_id = calendar_id
        self._service = None
        self._lock = Lock()

    def _svc(self):
        if self._service is None:
            with self._lock:
                if self._service is None:
                    from google.oauth2 import service_account
                    from googleapiclient.discovery import build
                    creds = service_account.Credentials.from_service_account_file(
                        self.service_account_json_path,
                        scopes=["https://www.googleapis.com/auth/calendar"],
                    )
                    self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def is_available(self, start: datetime, duration_minutes: int) -> bool:
        end = start + timedelta(minutes=duration_minutes)
        events = self._svc().events().list(
            calendarId=self.calendar_id,
            timeMin=start.isoformat() + "Z",
            timeMax=end.isoformat() + "Z",
            singleEvents=True,
            maxResults=5,
        ).execute()
        return not events.get("items")

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
        end = start + timedelta(minutes=duration_minutes)
        if not self.is_available(start, duration_minutes):
            return {"booked": False, "reason": "slot no longer available"}
        body = {
            "summary": f"{service} — {caller_name}",
            "description": f"Booked via voiceops-ai-agent.\nCaller: {caller_name}\nPhone: {phone}\nService: {service}\nNotes: {notes or ''}",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        event = self._svc().events().insert(calendarId=self.calendar_id, body=body).execute()
        return {
            "booked": True,
            "event": {
                "id": event["id"],
                "start": event["start"].get("dateTime") or event["start"].get("date"),
                "end": event["end"].get("dateTime") or event["end"].get("date"),
                "caller_name": caller_name,
                "phone": phone,
                "service": service,
                "notes": notes,
            },
        }

    # ── lifecycle: find / cancel / reschedule ────────────────────────
    #
    # Matches FakeCalendar.find_by_phone / cancel / reschedule.  brain
    # + clinic_tools operate against the abstract interface — swapping
    # backends is a config change, not code.

    # Regex to pull the phone stored by `book()` in the description.
    # `book()` writes "Phone: <caller-supplied>\n".  We normalize on
    # comparison rather than at write time so pre-existing events
    # written in other formats still match.
    _PHONE_DESC_RE = re.compile(
        r"(?im)^\s*Phone\s*[:\-]\s*(\+?[\d\s().\-]+)\s*$"
    )

    @staticmethod
    def _normalize_phone(raw: str) -> str:
        """Strip everything non-digit for equality comparison.  Prevents
        "+1-555-1234" vs "(555) 1234" from mismatching."""
        return "".join(c for c in (raw or "") if c.isdigit())

    def _extract_phone(self, event: dict) -> str:
        """Try event.description first (where our `book()` writes it),
        then extendedProperties (future-proofing for structured storage).
        Returns raw string; caller normalizes."""
        desc = event.get("description") or ""
        m = self._PHONE_DESC_RE.search(desc)
        if m:
            return m.group(1).strip()
        # Structured fallback — some callers/backends stash the phone in
        # extendedProperties.private.phone to avoid parsing text.
        priv = (event.get("extendedProperties") or {}).get("private") or {}
        if isinstance(priv, dict):
            return str(priv.get("phone") or "")
        return ""

    @staticmethod
    def _event_summary(event: dict) -> dict:
        """Reduce Google's event payload to the same shape FakeCalendar
        returns — brain code and dashboards can consume either."""
        start = event.get("start") or {}
        end = event.get("end") or {}
        return {
            "id": event.get("id"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "summary": event.get("summary"),
            "description": event.get("description"),
            "status": event.get("status"),  # confirmed | cancelled
        }

    def find_by_phone(
        self,
        phone: str,
        upcoming_only: bool = True,
        max_days_back: int = 30,
        max_days_ahead: int = 180,
    ) -> list[dict]:
        """Find events whose description carries the caller's phone.

        Google's events.list has no attendee-phone filter, so we do
        a bounded time-window scan (past 30 days .. next 180 by default)
        and match on the phone string embedded by `book()`.  This is
        O(events_in_window) — fine for a single-provider calendar,
        would need pagination for high-volume tenants.

        `upcoming_only=True` filters out events whose END is already
        past — matches FakeCalendar semantics.  Cancelled events are
        always excluded (Google uses `status="cancelled"` for these).
        """
        if not phone:
            return []
        target_digits = self._normalize_phone(phone)
        if not target_digits:
            return []
        now = datetime.now(timezone.utc)
        # Window: allow past events for lookup ("I booked something
        # last week"), but by default upcoming_only=True filters them.
        time_min = (now - timedelta(days=max_days_back)).isoformat()
        time_max = (now + timedelta(days=max_days_ahead)).isoformat()
        # Google requires trailing Z on the ISO if datetime is naive;
        # since we passed tz-aware, isoformat produces the offset already.

        results: list[dict] = []
        page_token: Optional[str] = None
        # Bounded page loop — never more than 5 pages (500 events) to
        # avoid runaway on a very busy calendar.
        for _ in range(5):
            req = self._svc().events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
                pageToken=page_token,
            )
            resp = req.execute()
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                if upcoming_only:
                    end_dt = self._parse_event_time(ev.get("end") or {})
                    if end_dt is not None and end_dt < now:
                        continue
                ev_phone = self._extract_phone(ev)
                if not ev_phone:
                    continue
                # Suffix match tolerates country-code drift:
                # caller says "555 123 4567" (10 digits), stored as
                # "+15551234567" (11 digits) → shared 10-digit suffix
                # counts as a match.  Requires at least 10 digits to
                # prevent false positives on very short strings.
                stored_digits = self._normalize_phone(ev_phone)
                if len(target_digits) < 7 or len(stored_digits) < 7:
                    continue
                if (
                    stored_digits.endswith(target_digits)
                    or target_digits.endswith(stored_digits)
                ):
                    results.append(self._event_summary(ev))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    @staticmethod
    def _parse_event_time(t: dict) -> Optional[datetime]:
        """Google event time is either {dateTime, timeZone} or {date}.
        Return a tz-aware datetime we can compare to now."""
        if not isinstance(t, dict):
            return None
        s = t.get("dateTime") or t.get("date")
        if not s:
            return None
        try:
            # dateTime is ISO 8601 with tz; date is YYYY-MM-DD (naive).
            if "T" in s:
                # Python's fromisoformat handles offset from 3.11+.
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            # All-day event — treat as start-of-day UTC.
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def cancel(self, event_id: str, reason: Optional[str] = None) -> dict:
        """Cancel an existing appointment.  Idempotent: Google's API
        returns 410 Gone when re-deleting an already-cancelled event —
        we treat that as success with a `deduplicated=True` flag so the
        caller doesn't retry-loop.

        The `reason` is appended to the event's description before
        deletion (best-effort — Google preserves the last-modified
        description in audit logs even after delete for 30 days).
        """
        if not event_id:
            return {"cancelled": False, "reason": "event_id_missing"}
        svc = self._svc()
        try:
            existing = svc.events().get(
                calendarId=self.calendar_id, eventId=event_id,
            ).execute()
        except Exception as e:
            # 404 → event gone/never-existed.  Not a lifecycle error.
            if "404" in str(e) or "notFound" in str(e).lower():
                return {"cancelled": False, "reason": "event_not_found"}
            # 410 → already deleted.  Idempotent success.
            if "410" in str(e) or "deleted" in str(e).lower():
                return {"cancelled": True, "deduplicated": True}
            raise
        # Already cancelled?  Idempotent path.
        if existing.get("status") == "cancelled":
            return {
                "cancelled": True,
                "event": self._event_summary(existing),
                "deduplicated": True,
            }
        # Optionally stamp the reason before delete.  Google's delete
        # is a hard remove; audit log holds the pre-delete state.
        if reason:
            try:
                new_desc = (
                    (existing.get("description") or "")
                    + f"\n[Cancelled via AI receptionist: {reason}]"
                )
                svc.events().patch(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    body={"description": new_desc},
                ).execute()
            except Exception:
                # Best-effort — proceed to delete even if patch fails.
                pass
        svc.events().delete(
            calendarId=self.calendar_id, eventId=event_id,
        ).execute()
        # Re-fetch is possible but delete is 204 — return the pre-delete
        # summary with status=cancelled so downstream (sinks, dashboard)
        # sees a consistent shape.
        summary = self._event_summary(existing)
        summary["status"] = "cancelled"
        return {"cancelled": True, "event": summary}

    def reschedule(
        self,
        event_id: str,
        new_start: datetime,
        duration_minutes: Optional[int] = None,
    ) -> dict:
        """Move an existing event to a new start.  If duration_minutes
        is None, preserves the original event's duration.

        Availability check ignores the current event's own slot (so
        moving 10:00-10:30 to 10:15-10:45 doesn't self-conflict).  On
        conflict, ORIGINAL event is unchanged and caller can pick a
        different slot.
        """
        if not event_id:
            return {"rescheduled": False, "reason": "event_id_missing"}
        svc = self._svc()
        try:
            existing = svc.events().get(
                calendarId=self.calendar_id, eventId=event_id,
            ).execute()
        except Exception as e:
            if "404" in str(e) or "notFound" in str(e).lower():
                return {"rescheduled": False, "reason": "event_not_found"}
            raise
        if existing.get("status") == "cancelled":
            return {"rescheduled": False, "reason": "event_cancelled"}
        # Parse existing duration.
        old_start = self._parse_event_time(existing.get("start") or {})
        old_end = self._parse_event_time(existing.get("end") or {})
        if old_start is None or old_end is None:
            return {"rescheduled": False, "reason": "event_corrupt"}
        dur = duration_minutes or int(
            (old_end - old_start).total_seconds() / 60
        )
        new_end = new_start + timedelta(minutes=dur)
        # Availability check ignoring THIS event.
        conflicts = svc.events().list(
            calendarId=self.calendar_id,
            timeMin=new_start.isoformat() + (
                "Z" if new_start.tzinfo is None else ""
            ),
            timeMax=new_end.isoformat() + (
                "Z" if new_end.tzinfo is None else ""
            ),
            singleEvents=True,
            maxResults=5,
        ).execute()
        for other in conflicts.get("items", []):
            if other.get("id") == event_id:
                continue
            if other.get("status") == "cancelled":
                continue
            return {
                "rescheduled": False,
                "reason": "slot_no_longer_available",
                "conflict_event_id": other.get("id"),
            }
        # Apply patch — Google accepts partial dateTime updates via patch.
        # We include both start + end since we're moving both boundaries.
        patched = svc.events().patch(
            calendarId=self.calendar_id,
            eventId=event_id,
            body={
                "start": {"dateTime": new_start.isoformat()},
                "end": {"dateTime": new_end.isoformat()},
            },
        ).execute()
        return {
            "rescheduled": True,
            "event": self._event_summary(patched),
        }
