from __future__ import annotations

from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

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

    def __init__(
        self,
        business: BusinessProfile,
        calendar: FakeCalendar,
        default_phone_region: str = "US",
        accepted_phone_regions: Optional[list[str]] = None,
    ) -> None:
        self.business = business
        self.calendar = calendar
        # R3 P4 (task #371, slim v1): tenant phone-region config for
        # the pre-write validator.  Defaults keep back-compat; wired
        # from BusinessProfile / tenant config once that lands.
        self.default_phone_region = default_phone_region
        # If accepted_regions is not supplied, allow just the default.
        self.accepted_phone_regions = accepted_phone_regions or [default_phone_region]

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

    def _resolve_service_or_error(
        self, spoken: str, call: ToolCall,
    ):
        """BUG-CHR-03 fix (2026-08-29): canonicalize a caller-spoken
        service name to a real tenant service BEFORE we compute
        duration or write the calendar.

        Returns:
          str — canonical service name (from BusinessProfile.services).
                Call proceeds with that name.
          ToolResult — structured error the LLM sees when the input is
                       AMBIGUOUS or UNKNOWN.  Prompt has an explicit
                       rule for each shape so no more empty completions.

        MATCH_FUZZY passes through (we accept the fuzzy pick; the LLM
        is instructed by prompt to confirm with the caller
        conversationally on a first booking).  If we ever want to
        force an explicit confirm on FUZZY, this is where to add it.
        """
        # Import lazily so unit tests that don't touch this path don't
        # pull the alias table.
        from packages.integrations.service_aliases import (
            resolve_service, ServiceMatchKind,
        )
        from packages.observability.humanness_events import (
            ServiceResolutionEvent, emit_humanness_event,
        )
        match = resolve_service(spoken, self.business.services)
        # Emit trace event for every resolution — dashboard/incident
        # tools query by kind="service_resolution".
        try:
            emit_humanness_event(ServiceResolutionEvent(
                call_id=getattr(self, "_call_id", "?"),
                tenant_id=getattr(
                    self.business, "id", getattr(
                        self.business, "tenant_id", "default"
                    ),
                ),
                session_id=getattr(self, "_session_id", "?"),
                spoken=str(spoken or ""),
                kind=match.kind.value,
                canonical_name=match.canonical_name,
                candidates=list(match.candidates),
                confidence=match.confidence,
                reason=match.reason,
            ))
        except Exception:
            # Observability failures must never break the tool path.
            pass
        if match.kind in (
            ServiceMatchKind.MATCH_EXACT, ServiceMatchKind.MATCH_FUZZY,
        ):
            return match.canonical_name
        if match.kind == ServiceMatchKind.AMBIGUOUS:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "service_ambiguous": True,
                    "service_input": spoken,
                    "candidates": list(match.candidates),
                    "reason": (
                        "The caller-said service could mean any of the "
                        "candidates. Ask which one they want before "
                        "booking."
                    ),
                },
            )
        # UNKNOWN
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={
                "service_unknown": True,
                "service_input": spoken,
                "available_services": [
                    s.name for s in self.business.services
                ][:6],
                "reason": (
                    "The caller-said service isn't in our list. Ask "
                    "what they need in their own words, then match to "
                    "one of the available services."
                ),
            },
        )

    def _validate_phone_or_error(
        self, raw_phone: str, call: ToolCall,
    ):
        """R3 P4 slim v1: pre-write phone validation.

        Returns:
          str  — canonical E.164 (VALID or POSSIBLE) — proceed to book.
          ToolResult — structured error the LLM sees + re-asks the
                       caller.  Do NOT hit the calendar.

        Design notes:
          - We accept POSSIBLE (right shape, not verified in the
            allocation tables) alongside VALID.  Refusing POSSIBLE
            would reject genuine new numbers.  PHASE2's workflow
            controller will drive the explicit caller-confirm loop
            for POSSIBLE; this slim v1 accepts it.
          - INVALID / TOO_LONG / EMPTY → structured error.  The
            `reason` field is written to be actionable when the LLM
            reads it (short, imperative — "ask the caller to repeat
            the number").
        """
        # Local import to avoid front-loading libphonenumber during
        # tool-def enumeration.
        from packages.slot_parsers import (
            normalize_spoken_digits, parse_phone, PhoneStatus,
        )

        if not raw_phone:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "phone_missing": True,
                    "reason": "The caller has not given a phone yet. "
                              "Ask them for the full number before booking.",
                },
            )
        # Layer A: normalize whatever shape the LLM sent (may be spoken
        # digits copied from transcript, may be already-canonical).
        canonical = normalize_spoken_digits(raw_phone) or raw_phone
        # Layer B: libphonenumber wrap.
        r = parse_phone(
            canonical,
            default_region=self.default_phone_region,
            accepted_regions=self.accepted_phone_regions,
        )
        if r.status in (PhoneStatus.COMPLETE, PhoneStatus.POSSIBLE):
            return r.value  # canonical E.164
        # INVALID / PARTIAL / TOO_LONG / EMPTY
        if r.status == PhoneStatus.PARTIAL:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "phone_partial": True,
                    "phone_input": raw_phone,
                    "reason": "The phone number is too short. Ask the "
                              "caller to say the FULL number again.",
                },
            )
        if r.status == PhoneStatus.TOO_LONG:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "phone_too_long": True,
                    "phone_input": raw_phone,
                    "reason": "The phone number is too long — it may "
                              "include an extra digit or two numbers "
                              "glued together. Ask the caller to say "
                              "just their number.",
                },
            )
        # INVALID / EMPTY / other
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={
                "phone_invalid": True,
                "phone_input": raw_phone,
                "reason": "The phone number does not look valid. Ask "
                          "the caller to repeat their number slowly.",
            },
        )

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "check_availability":
                date_str = call.arguments["date"]
                # BUG-CHR-03 wire (2026-08-29): resolve the spoken
                # service name to the tenant's canonical service list
                # BEFORE looking up its duration.  A stray "A follow-up"
                # from the caller was previously handed straight to
                # _service_duration, which fell back to 30min and
                # silently mis-classified the appointment.
                _spoken_service = call.arguments["service"]
                _service_result = self._resolve_service_or_error(
                    _spoken_service, call,
                )
                if isinstance(_service_result, ToolResult):
                    return _service_result
                service = _service_result
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
                # BUG-CHR-03 wire (2026-08-29): resolve spoken service
                # BEFORE the phone precondition + calendar write.
                _spoken_service = call.arguments["service"]
                _service_result = self._resolve_service_or_error(
                    _spoken_service, call,
                )
                if isinstance(_service_result, ToolResult):
                    return _service_result
                service = _service_result
                duration = self._service_duration(service)
                # R3 P4 (task #371, slim v1): PHONE PRECONDITION.
                # Run the LLM-supplied phone through libphonenumber
                # BEFORE hitting the calendar.  Two purposes:
                #   1. Catch hallucinated / mis-heard digits (LLM
                #      loves inventing "555" numbers when it hasn't
                #      actually heard one).  INVALID → structured
                #      error the LLM sees and re-asks.
                #   2. Normalize whatever shape landed (spoken,
                #      spaced, hyphenated) into canonical E.164 so
                #      the calendar record is stable + matchable.
                raw_phone = str(call.arguments.get("phone") or "").strip()
                validated_phone = self._validate_phone_or_error(
                    raw_phone, call,
                )
                if isinstance(validated_phone, ToolResult):
                    # Precondition failed — hand the LLM a structured
                    # error and skip the calendar write entirely.
                    return validated_phone
                outcome = self.calendar.book(
                    start=start,
                    duration_minutes=duration,
                    caller_name=call.arguments["caller_name"],
                    phone=validated_phone,  # canonical E.164
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
