"""Real estate wholesaler outbound tools.

Different shape from clinic/restaurant/real_estate:
- Primary output is a call disposition (HOT_LEAD / COLD_LEAD / etc), not a booking
- No calendar tools — this is one-shot outbound, not appointment-based
- Includes a `capture_disposition` tool the assistant calls at end of call
- Includes `record_rent_update` when the owner mentions a different rent
- Includes `escalate_to_human` for hostile / DNC / lost callers
"""
from __future__ import annotations

from datetime import datetime

from packages.schemas import BusinessProfile, ToolCall, ToolDefinition, ToolResult

from .fake_calendar import FakeCalendar


DISPOSITIONS = ["HOT_LEAD", "COLD_LEAD", "PROPERTY_UNAVAILABLE", "NO_ANSWER", "CALLBACK_REQUESTED", "DO_NOT_CALL"]


def build_wholesaler_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="capture_disposition",
            description=(
                "Record the outcome of the call. ALWAYS call this exactly once "
                "before hanging up. Pick one disposition from the list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "disposition": {"type": "string", "enum": DISPOSITIONS},
                    "notes": {"type": "string", "description": "1-2 sentences summarizing what happened"},
                    "callback_time": {
                        "type": "string",
                        "description": "If CALLBACK_REQUESTED, when they asked to be called back (natural language OK)",
                    },
                },
                "required": ["disposition"],
            },
        ),
        ToolDefinition(
            name="record_rent_update",
            description=(
                "Call this ONLY if the property owner told you a rent amount different from "
                "the one you mentioned. Do not call on silence or unclear responses."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "new_rent_amount": {"type": "integer", "description": "Whole dollars, no symbols"},
                    "confidence": {"type": "string", "enum": ["confirmed", "approximate"]},
                },
                "required": ["new_rent_amount"],
            },
        ),
        ToolDefinition(
            name="lookup_faq",
            description="Look up an FAQ topic (who is this, how did you get my number, what is seller financing, etc).",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        ),
        ToolDefinition(
            name="escalate_to_human",
            description="Escalate — hostile caller, legal question, or asked to speak to a human.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
    ]


class WholesalerToolHandler:
    """One instance per call. Buffers the disposition + rent update so the
    session manager can write them back to the source sheet on call end."""

    # Audit-3 fix (2026-08-04): explicit tool-name set for ComposeHandler.
    TOOL_NAMES = frozenset({
        "capture_disposition", "record_rent_update",
        "lookup_faq", "escalate_to_human",
    })

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar  # unused; kept for interface parity
        self.captured_disposition: dict | None = None
        self.rent_update: dict | None = None

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "capture_disposition":
                disp = call.arguments.get("disposition", "").upper()
                if disp not in DISPOSITIONS:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, result=None,
                        error=f"invalid disposition: {disp}; must be one of {DISPOSITIONS}",
                    )
                self.captured_disposition = {
                    "disposition": disp,
                    "notes": call.arguments.get("notes", ""),
                    "callback_time": call.arguments.get("callback_time"),
                    "recorded_at": datetime.utcnow().isoformat(),
                }
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"recorded": True, **self.captured_disposition},
                )

            if call.name == "record_rent_update":
                try:
                    new_rent = int(call.arguments["new_rent_amount"])
                except (KeyError, TypeError, ValueError):
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, result=None,
                        error="new_rent_amount must be an integer (whole dollars)",
                    )
                self.rent_update = {
                    "new_rent_amount": new_rent,
                    "confidence": call.arguments.get("confidence", "confirmed"),
                    "recorded_at": datetime.utcnow().isoformat(),
                }
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"recorded": True, **self.rent_update},
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
