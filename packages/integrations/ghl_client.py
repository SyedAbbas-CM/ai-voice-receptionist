"""GoHighLevel API v2 client.

Uses a Private Integration token — sub-account owner generates one under
Settings -> Private Integrations. No OAuth flow needed.

Docs: https://highlevel.stoplight.io/docs/integrations/
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import httpx


GHL_BASE = "https://services.leadconnectorhq.com"


class GHLError(Exception):
    pass


class GoHighLevelClient:
    def __init__(
        self,
        api_token: str,
        location_id: str,
        api_version: str = "2021-07-28",
        default_calendar_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_token:
            raise GHLError("GHL_API_TOKEN not set")
        if not location_id:
            raise GHLError("GHL_LOCATION_ID not set")
        self.api_token = api_token
        self.location_id = location_id
        self.api_version = api_version
        self.default_calendar_id = default_calendar_id
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, f"{GHL_BASE}{path}", headers=self._headers, **kwargs)
        if resp.status_code >= 400:
            raise GHLError(f"GHL {method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else {}

    async def upsert_contact(
        self,
        phone: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source: str = "voiceops-ai-agent",
    ) -> dict:
        payload = {
            "locationId": self.location_id,
            "phone": phone,
            "source": source,
        }
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if email:
            payload["email"] = email
        if tags:
            payload["tags"] = tags
        data = await self._request("POST", "/contacts/upsert", json=payload)
        return data.get("contact") or data

    async def add_note(self, contact_id: str, body: str) -> dict:
        payload = {"body": body, "userId": ""}
        return await self._request("POST", f"/contacts/{contact_id}/notes", json=payload)

    async def create_opportunity(
        self,
        contact_id: str,
        pipeline_id: str,
        pipeline_stage_id: str,
        name: str,
        monetary_value: Optional[float] = None,
        status: str = "open",
    ) -> dict:
        payload = {
            "locationId": self.location_id,
            "contactId": contact_id,
            "pipelineId": pipeline_id,
            "pipelineStageId": pipeline_stage_id,
            "name": name,
            "status": status,
        }
        if monetary_value is not None:
            payload["monetaryValue"] = monetary_value
        return await self._request("POST", "/opportunities/", json=payload)

    async def list_free_slots(
        self,
        calendar_id: Optional[str],
        start: datetime,
        end: datetime,
        timezone: str = "America/New_York",
    ) -> list[str]:
        cal = calendar_id or self.default_calendar_id
        if not cal:
            raise GHLError("no calendar_id provided and GHL_CALENDAR_ID not set")
        params = {
            "startDate": int(start.timestamp() * 1000),
            "endDate": int(end.timestamp() * 1000),
            "timezone": timezone,
        }
        data = await self._request("GET", f"/calendars/{cal}/free-slots", params=params)
        slots: list[str] = []
        for day_key, day_val in data.items():
            for s in (day_val.get("slots") or []) if isinstance(day_val, dict) else []:
                slots.append(s)
        return slots

    async def send_sms(
        self,
        contact_id: str,
        body: str,
    ) -> dict:
        """Send an SMS to a contact via GHL Conversations.

        2026-08-31 (GHL-SMS wave 1): the "wow" moment on demos —
        caller hangs up, phone buzzes 3 seconds later with the
        confirmation text. Requires the Private Integration token
        to have `conversations/message.write` scope.

        Body should be under 160 chars to avoid multi-part SMS
        billing. GHL will route via whichever phone number is
        configured as the Location's outbound SMS number.

        Fails loudly (raises GHLError) on non-2xx so the sink can
        log + swallow — never blocks booking flow.
        """
        payload = {
            "type": "SMS",
            "contactId": contact_id,
            "message": body,
        }
        return await self._request(
            "POST", "/conversations/messages", json=payload,
        )

    async def book_appointment(
        self,
        contact_id: str,
        start: datetime,
        duration_minutes: int,
        title: str,
        calendar_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        cal = calendar_id or self.default_calendar_id
        if not cal:
            raise GHLError("no calendar_id provided and GHL_CALENDAR_ID not set")
        payload = {
            "calendarId": cal,
            "locationId": self.location_id,
            "contactId": contact_id,
            "startTime": start.isoformat(),
            "endTime": (start + timedelta(minutes=duration_minutes)).isoformat(),
            "title": title,
            "appointmentStatus": "confirmed",
        }
        if notes:
            payload["notes"] = notes
        return await self._request("POST", "/calendars/events/appointments", json=payload)
