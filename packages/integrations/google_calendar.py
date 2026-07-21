"""Google Calendar adapter — same interface as FakeCalendar so the brain doesn't care.

Auth: service account. Share your target calendar with the service account
email so it can read/write.

Deps (lazy-imported to keep them optional):
    pip install google-api-python-client google-auth
"""
from __future__ import annotations

from datetime import datetime, timedelta
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
