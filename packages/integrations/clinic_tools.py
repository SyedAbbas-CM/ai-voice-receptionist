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
    ]


class ClinicToolHandler:
    """Routes tool calls from the brain to the calendar and business profile.
    One instance per session so handlers can capture per-call state if needed."""

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

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
                day = datetime.fromisoformat(date_str)
                duration = self._service_duration(service)
                slots = self.calendar.list_slots(day, duration)
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result={"date": date_str, "service": service, "open_slots": slots[:8]},
                )

            if call.name == "book_appointment":
                start = datetime.fromisoformat(call.arguments["start_iso"])
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

            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                result=None,
                error=f"unknown tool: {call.name}",
            )
        except Exception as e:
            return ToolResult(tool_call_id=call.id, name=call.name, result=None, error=str(e))
