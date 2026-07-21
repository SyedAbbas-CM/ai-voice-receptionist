"""CRM sinks — write-side integrations that log call outcomes.

The brain and session manager fire two events:
  - on_booking(state, booking_payload)  after a successful book_appointment tool call
  - on_call_end(state)                  when the call ends (voluntary or hangup)

Each sink swallows its own errors so a broken CRM never crashes the call flow.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from packages.schemas import CallState


log = logging.getLogger(__name__)


class CRMSink(ABC):
    name: str = "base"

    @abstractmethod
    async def on_booking(self, state: CallState, booking: dict) -> None:
        ...

    @abstractmethod
    async def on_call_end(self, state: CallState) -> None:
        ...


class NoopSink(CRMSink):
    name = "none"

    async def on_booking(self, state: CallState, booking: dict) -> None:
        return None

    async def on_call_end(self, state: CallState) -> None:
        return None


class CompositeSink(CRMSink):
    """Fan out to multiple sinks. Each is best-effort."""

    name = "composite"

    def __init__(self, sinks: list[CRMSink]) -> None:
        self.sinks = sinks

    async def on_booking(self, state: CallState, booking: dict) -> None:
        for s in self.sinks:
            try:
                await s.on_booking(state, booking)
            except Exception as e:
                log.warning("sink %s on_booking failed: %s", s.name, e)

    async def on_call_end(self, state: CallState) -> None:
        for s in self.sinks:
            try:
                await s.on_call_end(state)
            except Exception as e:
                log.warning("sink %s on_call_end failed: %s", s.name, e)


class GHLSink(CRMSink):
    """Upsert contact + add note + book appointment on GHL calendar (if configured)."""

    name = "ghl"

    def __init__(self, client) -> None:
        self.client = client  # GoHighLevelClient

    def _split_name(self, full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not full:
            return None, None
        parts = full.strip().split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None

    async def on_booking(self, state: CallState, booking: dict) -> None:
        args = booking.get("arguments") or {}
        result = booking.get("result") or {}
        if not result.get("booked"):
            return
        phone = args.get("phone") or state.extracted.phone
        if not phone:
            return
        first, last = self._split_name(args.get("caller_name") or state.extracted.caller_name)
        contact = await self.client.upsert_contact(
            phone=phone,
            first_name=first,
            last_name=last,
            tags=["voiceops-ai-agent", state.extracted.intent.value if state.extracted else "unknown"],
        )
        contact_id = contact.get("id") or (contact.get("contact") or {}).get("id")
        if not contact_id:
            return
        summary = state.extracted.summary if state.extracted else ""
        await self.client.add_note(contact_id, f"Booked via AI receptionist.\n{summary}\nBooking: {args}")

        event = result.get("event") or {}
        start = event.get("start")
        if start and self.client.default_calendar_id:
            try:
                await self.client.book_appointment(
                    contact_id=contact_id,
                    start=datetime.fromisoformat(start),
                    duration_minutes=30,
                    title=f"{args.get('service', 'Appointment')} — {args.get('caller_name', '')}",
                    notes=args.get("notes"),
                )
            except Exception as e:
                log.warning("ghl book_appointment failed: %s", e)

    async def on_call_end(self, state: CallState) -> None:
        if not state.extracted or not state.extracted.phone:
            return
        first, last = self._split_name(state.extracted.caller_name)
        try:
            contact = await self.client.upsert_contact(
                phone=state.extracted.phone,
                first_name=first,
                last_name=last,
                tags=["voiceops-ai-agent"],
            )
            contact_id = contact.get("id") or (contact.get("contact") or {}).get("id")
            if contact_id:
                lines = [
                    f"Session: {state.session_id}",
                    f"Intent: {state.extracted.intent.value}",
                    f"Urgency: {state.extracted.urgency.value}",
                    f"Lead score: {state.extracted.lead_score}",
                    f"Summary: {state.extracted.summary}",
                    f"Status: {state.status.value if hasattr(state.status, 'value') else state.status}",
                ]
                await self.client.add_note(contact_id, "\n".join(lines))
        except Exception as e:
            log.warning("ghl on_call_end failed: %s", e)


class SheetsSink(CRMSink):
    """Append one row per completed call to a Google Sheet."""

    name = "sheets"

    def __init__(self, sheets) -> None:
        self.sheets = sheets  # GoogleSheets

    async def on_booking(self, state: CallState, booking: dict) -> None:
        return None  # we log at call-end so each call is one row

    async def on_call_end(self, state: CallState) -> None:
        extracted = state.extracted.model_dump() if state.extracted else {}
        status = state.status.value if hasattr(state.status, "value") else state.status
        escalated = status == "escalated"
        try:
            self.sheets.append_call(
                session_id=state.session_id,
                extracted=extracted,
                status=status,
                escalated=escalated,
            )
        except Exception as e:
            log.warning("sheets append_call failed: %s", e)


def build_sink_from_env(mode: str, settings) -> CRMSink:
    """Factory: 'none' | 'ghl' | 'sheets' | 'ghl+sheets'."""
    mode = (mode or "none").lower().strip()
    if mode == "none":
        return NoopSink()

    sinks: list[CRMSink] = []
    parts = {p.strip() for p in mode.split("+")}

    if "ghl" in parts:
        from .ghl_client import GoHighLevelClient
        client = GoHighLevelClient(
            api_token=settings.ghl_api_token or "",
            location_id=settings.ghl_location_id or "",
            api_version=settings.ghl_api_version,
            default_calendar_id=settings.ghl_calendar_id,
        )
        sinks.append(GHLSink(client))

    if "sheets" in parts:
        from .google_sheets import GoogleSheets
        if not settings.google_service_account_json or not settings.google_sheet_id:
            raise RuntimeError("sheets sink requires GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID")
        sheets = GoogleSheets(
            service_account_json_path=settings.google_service_account_json,
            sheet_id=settings.google_sheet_id,
            tab=settings.google_sheet_tab,
        )
        sinks.append(SheetsSink(sheets))

    if not sinks:
        return NoopSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)
