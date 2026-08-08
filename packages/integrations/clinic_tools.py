from __future__ import annotations

from datetime import datetime, timedelta
from typing import Awaitable, Callable

from packages.schemas import (
    BusinessProfile,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

from .fake_calendar import FakeCalendar


def build_clinic_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_availability",
            description="Check open appointment slots for a given service on a given date. Always call this before confirming a time.",
            parameters={
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service or appointment type"},
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                },
                "required": ["service", "date"],
            },
        ),
        ToolDefinition(
            name="book_appointment",
            description="Book a confirmed appointment. Only call this after the caller has agreed to a specific available slot and you have their name and phone.",
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "service": {"type": "string"},
                    "start_iso": {"type": "string", "description": "ISO datetime YYYY-MM-DDTHH:MM"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "service", "start_iso"],
            },
        ),
        ToolDefinition(
            name="lookup_faq",
            description="Look up a frequently-asked question from the business profile by topic keyword.",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        ),
        ToolDefinition(
            name="escalate_to_human",
            description="Hand off to a human teammate. Call this for emergencies, complaints, or when the caller insists on speaking to a person.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
        # Sprint 10 Track B2: appointment lifecycle.  A credible
        # receptionist has to handle changes, not just first bookings.
        ToolDefinition(
            name="find_existing_appointment",
            description="Find an existing appointment by caller phone number. Use when a caller wants to look up, cancel, or reschedule an existing booking.",
            parameters={
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Caller's phone number."},
                    "upcoming_only": {
                        "type": "boolean",
                        "description": "True (default) returns only future appointments.",
                    },
                },
                "required": ["phone"],
            },
        ),
        ToolDefinition(
            name="cancel_appointment",
            description="Cancel an existing appointment by its id.  Idempotent: cancelling twice is safe.",
            parameters={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        ),
        ToolDefinition(
            name="reschedule_appointment",
            description="Move an existing appointment to a new time.  Fails if the new time is already booked.",
            parameters={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "new_start_iso": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                },
                "required": ["appointment_id", "new_start_iso"],
            },
        ),
    ]


class ClinicToolHandler:
    """Routes tool calls from the brain to the calendar and business profile.
    One instance per session so handlers can capture per-call state if needed."""

    # Audit-3 fix (2026-08-04): explicit routing set — ComposeHandler
    # consults this before invoking, so a differently-worded error can't
    # silently drop a tool call the way it did in the RAG dispatcher bug.
    TOOL_NAMES = frozenset({
        "check_availability", "book_appointment",
        "lookup_faq", "escalate_to_human",
        # Sprint 10 Track B2: appointment lifecycle
        "find_existing_appointment", "cancel_appointment", "reschedule_appointment",
    })

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    def _parse_or_resolve_date(self, date_str: str):
        """Normalize a date argument.

        Returns:
          * datetime object if resolved (ISO date OR natural date)
          * dict with `ambiguous=True` if TemporalResolver flagged it
          * None if unresolvable — caller should re-ask.

        Order:
          1. Try datetime.fromisoformat directly (LLM sent ISO).
          2. Fall back to TemporalResolver for natural language.
        """
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str)
        except (TypeError, ValueError):
            pass
        # Natural-language path via kernel's TemporalResolver
        try:
            from packages.dialogue import TemporalContext, TemporalResolver, Resolution
            ctx = TemporalContext.now_in(
                self.business.timezone,
                business=type("B", (), {"hours": self.business.hours})(),
            )
            result = TemporalResolver().resolve(date_str, ctx)
            if result.resolution == Resolution.AMBIGUOUS_NEEDS_CONFIRM:
                return {
                    "ambiguous": True,
                    "reason": result.spoken_confirmation,
                    "candidates": [
                        r.range_start.isoformat() if r.range_start else None
                        for r in result.interpretations
                    ],
                }
            if result.range_start is not None:
                # Strip tzinfo so downstream fake calendar's naive
                # datetime math stays consistent.
                return result.range_start.replace(tzinfo=None)
        except Exception:
            pass
        return None

    def _service_duration(self, name: str) -> int:
        for s in self.business.services:
            if s.name.lower() == name.lower():
                return s.duration_minutes
        return 30

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "check_availability":
                date_str = call.arguments["date"]
                service = call.arguments["service"]
                # Sprint 10 WIRING: normalize natural date via
                # TemporalResolver before ISO parse.  Fixes the
                # intelligence-test failure where LLM sent "next
                # Friday" as the `date` arg and datetime.fromisoformat
                # crashed the tool.
                day = self._parse_or_resolve_date(date_str)
                if day is None:
                    # Return a STRUCTURED result the LLM can react to
                    # ("I don't recognize that date, could you say a
                    # specific one?") instead of crashing.
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={
                            "date_unparseable": True,
                            "date_input": date_str,
                            "reason": "I couldn't resolve that date. Ask the caller for a specific date (like Monday August 11).",
                        },
                    )
                if isinstance(day, dict) and day.get("ambiguous"):
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={
                            "date_ambiguous": True,
                            "date_input": date_str,
                            "reason": day["reason"],
                            "candidates": day["candidates"],
                        },
                    )
                duration = self._service_duration(service)
                slots = self.calendar.list_slots(day, duration)
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result={"date": date_str, "service": service, "open_slots": slots[:8]},
                )

            if call.name == "book_appointment":
                start_str = call.arguments["start_iso"]
                start = self._parse_or_resolve_date(start_str)
                if start is None:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={
                            "date_unparseable": True,
                            "start_iso_input": start_str,
                            "reason": "I couldn't parse that start time. Ask the caller to say a specific date and time.",
                        },
                    )
                if isinstance(start, dict) and start.get("ambiguous"):
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={
                            "date_ambiguous": True,
                            "start_iso_input": start_str,
                            "reason": start["reason"],
                            "candidates": start["candidates"],
                        },
                    )
                service = call.arguments["service"]
                duration = self._service_duration(service)
                outcome = self.calendar.book(
                    start=start,
                    duration_minutes=duration,
                    caller_name=call.arguments["caller_name"],
                    phone=call.arguments["phone"],
                    service=service,
                    notes=call.arguments.get("notes"),
                )
                return ToolResult(tool_call_id=call.id, name=call.name, result=outcome)

            if call.name == "lookup_faq":
                topic = (call.arguments.get("topic") or "").lower()
                hits = {q: a for q, a in self.business.faqs.items() if topic in q.lower()}
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result=hits or {"_no_match": "no FAQ entry found for topic"},
                )

            if call.name == "escalate_to_human":
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result={
                        "escalated": True,
                        "reason": call.arguments.get("reason"),
                        "callback_number": self.business.escalation_phone,
                    },
                )

            # Sprint 10 Track B2: appointment lifecycle
            if call.name == "find_existing_appointment":
                phone = call.arguments.get("phone") or ""
                upcoming = call.arguments.get("upcoming_only", True)
                events = self.calendar.find_by_phone(phone, upcoming_only=upcoming)
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"appointments": events, "count": len(events)},
                )

            if call.name == "cancel_appointment":
                event_id = call.arguments.get("appointment_id") or ""
                reason = call.arguments.get("reason")
                outcome = self.calendar.cancel(event_id, reason=reason)
                return ToolResult(
                    tool_call_id=call.id, name=call.name, result=outcome,
                )

            if call.name == "reschedule_appointment":
                event_id = call.arguments.get("appointment_id") or ""
                new_start = datetime.fromisoformat(call.arguments["new_start_iso"])
                dur = call.arguments.get("duration_minutes")
                outcome = self.calendar.reschedule(
                    event_id, new_start, duration_minutes=dur,
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name, result=outcome,
                )

            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                result=None,
                error=f"unknown tool: {call.name}",
            )
        except Exception as e:
            return ToolResult(tool_call_id=call.id, name=call.name, result=None, error=str(e))
