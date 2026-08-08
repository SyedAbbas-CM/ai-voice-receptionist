"""Real-estate lead qualification tools.

Different shape from clinic/restaurant — the primary output isn't a booking,
it's a qualified lead. Booking a viewing is one branch.
"""
from __future__ import annotations

from datetime import datetime

from packages.schemas import BusinessProfile, ToolCall, ToolDefinition, ToolResult

from .fake_calendar import FakeCalendar


def build_real_estate_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_viewing_availability",
            description="Check open viewing slots on a given date. Use this before offering a viewing time.",
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        ),
        ToolDefinition(
            name="book_viewing",
            description="Book a property viewing. Only call after collecting name, phone, budget, and confirming a specific available slot.",
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "property_ref": {"type": "string", "description": "Address, listing id, or description of the property"},
                    "start_iso": {"type": "string", "description": "ISO datetime YYYY-MM-DDTHH:MM"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "property_ref", "start_iso"],
            },
        ),
        ToolDefinition(
            name="qualify_lead",
            description="Record a qualified lead. Call this once you have enough info to hand to an agent — budget, buying/renting/selling, timeline, financing status.",
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "intent": {"type": "string", "enum": ["buy", "rent", "sell", "invest", "other"]},
                    "budget_max_usd": {"type": "integer", "description": "Max budget in whole dollars; 0 if not disclosed"},
                    "timeline": {"type": "string", "description": "e.g. '30 days', 'this quarter', 'just browsing'"},
                    "financing_status": {"type": "string", "enum": ["pre_approved", "shopping", "cash", "unknown"]},
                    "areas": {"type": "array", "items": {"type": "string"}, "description": "Neighborhoods of interest"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "intent"],
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
            description="Hand off to a human agent — high-value leads, complaints, legal questions, or repeated confusion.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
    ]


def _score_lead(intent: str, budget: int, timeline: str, financing: str) -> int:
    """Cheap heuristic lead score 0-100."""
    score = 20
    intent_bonus = {"buy": 30, "sell": 30, "rent": 15, "invest": 25, "other": 0}
    score += intent_bonus.get(intent, 0)
    if budget >= 1_000_000:
        score += 25
    elif budget >= 500_000:
        score += 15
    elif budget >= 200_000:
        score += 8
    timeline_lower = (timeline or "").lower()
    if any(k in timeline_lower for k in ("today", "week", "asap", "immediately")):
        score += 15
    elif any(k in timeline_lower for k in ("month", "30 day", "60 day", "quarter")):
        score += 10
    if financing == "pre_approved" or financing == "cash":
        score += 15
    elif financing == "shopping":
        score += 5
    return min(100, score)


class RealEstateToolHandler:
    # Audit-3 fix (2026-08-04): explicit tool-name set for ComposeHandler.
    TOOL_NAMES = frozenset({
        "check_viewing_availability", "book_viewing", "qualify_lead",
        "lookup_faq", "escalate_to_human",
    })

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    def _viewing_duration(self) -> int:
        for s in self.business.services:
            if s.name.lower() == "viewing":
                return s.duration_minutes
        return 45

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "check_viewing_availability":
                day = datetime.fromisoformat(call.arguments["date"])
                duration = self._viewing_duration()
                slots = self.calendar.list_slots(day, duration, open_hhmm="09:00", close_hhmm="19:00")
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"date": call.arguments["date"], "open_slots": slots[:10]},
                )

            if call.name == "book_viewing":
                start = datetime.fromisoformat(call.arguments["start_iso"])
                duration = self._viewing_duration()
                outcome = self.calendar.book(
                    start=start,
                    duration_minutes=duration,
                    caller_name=call.arguments["caller_name"],
                    phone=call.arguments["phone"],
                    service=f"Viewing: {call.arguments['property_ref']}",
                    notes=call.arguments.get("notes"),
                )
                return ToolResult(tool_call_id=call.id, name=call.name, result=outcome)

            if call.name == "qualify_lead":
                score = _score_lead(
                    intent=call.arguments.get("intent", "other"),
                    budget=int(call.arguments.get("budget_max_usd") or 0),
                    timeline=call.arguments.get("timeline", ""),
                    financing=call.arguments.get("financing_status", "unknown"),
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "qualified": True,
                        "lead_score": score,
                        "caller_name": call.arguments.get("caller_name"),
                        "phone": call.arguments.get("phone"),
                        "intent": call.arguments.get("intent"),
                        "areas": call.arguments.get("areas") or [],
                        "notes": call.arguments.get("notes"),
                    },
                )

            if call.name == "lookup_faq":
                topic = (call.arguments.get("topic") or "").lower()
                hits = {q: a for q, a in self.business.faqs.items() if topic in q.lower()}
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=hits or {"_no_match": "no FAQ entry found"},
                )

            if call.name == "escalate_to_human":
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
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
