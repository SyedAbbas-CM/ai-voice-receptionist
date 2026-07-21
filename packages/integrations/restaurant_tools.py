"""Restaurant reservation tools.

Same shape as clinic_tools: build tool definitions + handler class.
Reuses FakeCalendar for slot management — a reservation is just a
timed calendar entry with party size.
"""
from __future__ import annotations

from datetime import datetime

from packages.schemas import BusinessProfile, ToolCall, ToolDefinition, ToolResult

from .fake_calendar import FakeCalendar


def build_restaurant_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_availability",
            description="Check open reservation slots for a party size on a given date. Always call this before offering a time.",
            parameters={
                "type": "object",
                "properties": {
                    "party_size": {"type": "integer", "description": "Number of guests"},
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                },
                "required": ["party_size", "date"],
            },
        ),
        ToolDefinition(
            name="book_reservation",
            description="Book a confirmed reservation. Only call after the caller has agreed to a specific available time and given you a name and phone.",
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "start_iso": {"type": "string", "description": "ISO datetime YYYY-MM-DDTHH:MM"},
                    "notes": {"type": "string", "description": "Dietary needs, occasion, seating preference"},
                },
                "required": ["caller_name", "phone", "party_size", "start_iso"],
            },
        ),
        ToolDefinition(
            name="lookup_faq",
            description="Look up an FAQ topic from the business profile.",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        ),
        ToolDefinition(
            name="escalate_to_human",
            description="Hand off to a human — private events, complaints, or repeated confusion.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
    ]


def _service_for_party(party_size: int, business: BusinessProfile) -> str:
    """Pick the closest matching Table service by party size."""
    best_match = None
    for s in business.services:
        # Match by name pattern: "Table for N"
        if s.name.lower().startswith("table for "):
            try:
                n = int(s.name.split()[-1])
                if best_match is None or abs(n - party_size) < abs(best_match[0] - party_size):
                    best_match = (n, s)
            except ValueError:
                continue
    return best_match[1].name if best_match else "Reservation"


class RestaurantToolHandler:
    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

    def _duration_for_party(self, party_size: int) -> int:
        service_name = _service_for_party(party_size, self.business)
        for s in self.business.services:
            if s.name == service_name:
                return s.duration_minutes
        return 90 if party_size <= 4 else 120

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "check_availability":
                party = int(call.arguments["party_size"])
                day = datetime.fromisoformat(call.arguments["date"])
                duration = self._duration_for_party(party)
                slots = self.calendar.list_slots(day, duration, open_hhmm="17:00", close_hhmm="22:00")
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result={"date": call.arguments["date"], "party_size": party, "open_slots": slots[:8]},
                )

            if call.name == "book_reservation":
                start = datetime.fromisoformat(call.arguments["start_iso"])
                party = int(call.arguments["party_size"])
                service = _service_for_party(party, self.business)
                duration = self._duration_for_party(party)
                outcome = self.calendar.book(
                    start=start,
                    duration_minutes=duration,
                    caller_name=call.arguments["caller_name"],
                    phone=call.arguments["phone"],
                    service=f"{service} (party of {party})",
                    notes=call.arguments.get("notes"),
                )
                return ToolResult(tool_call_id=call.id, name=call.name, result=outcome)

            if call.name == "lookup_faq":
                topic = (call.arguments.get("topic") or "").lower()
                hits = {q: a for q, a in self.business.faqs.items() if topic in q.lower()}
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    result=hits or {"_no_match": "no FAQ entry found"},
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
                tool_call_id=call.id, name=call.name, result=None,
                error=f"unknown tool: {call.name}",
            )
        except Exception as e:
            return ToolResult(tool_call_id=call.id, name=call.name, result=None, error=str(e))
