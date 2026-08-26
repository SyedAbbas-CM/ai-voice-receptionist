"""Real-estate lead qualification tools.

2026-08-25 (EU demo pass): rebuilt on top of the Ribeira Prime fixture
in `sample-data/real-estate/business.json`.  Real-estate's primary
output isn't a booking — it's a qualified lead, with viewing/valuation
as follow-ups.  The tools reflect that:

  qualify_buyer_lead    — the flagship buyer qualification flow (budget,
                          financing, timeline, areas, must-haves, non-
                          resident status)
  qualify_seller_lead   — for sellers requesting a valuation.  Takes
                          address, sqm, condition, timeline, reason.
                          Never quotes a valuation on the phone.
  qualify_rental_lead   — rental applicants: budget, move-in, contract
                          length, employment status, occupants, pets.
  book_viewing          — with property_ref + already-agreed slot.
                          Requires a lead first.
  book_valuation_visit  — seller-side home visit.
  book_virtual_tour     — WhatsApp/Zoom variant for buyers abroad.
  lookup_faq            — FAQ topic search against business profile.
  take_message          — off-hours or when caller doesn't want a call
                          back right now (job brief edge case #3).
  escalate_to_human     — warm-transfer for the always_transfer_on
                          triggers from human_transfer_rules.

Design constraints from the fixture persona:
  * Never invent property details / prices / availability
  * Never quote a valuation on the phone — always requires a home visit
  * Always transfer on complaint / offer_over_500k / legal / specific-
    agent-by-name
  * Non-residents get NIF + Portuguese bank + timeline info by default
  * Currency is EUR, not USD (was a bug in the old scaffold)
"""
from __future__ import annotations

from datetime import datetime

from packages.schemas import BusinessProfile, ToolCall, ToolDefinition, ToolResult

from .fake_calendar import FakeCalendar


def build_real_estate_tools() -> list[ToolDefinition]:
    return [
        # ── qualification (the primary flows) ───────────────────────
        ToolDefinition(
            name="qualify_buyer_lead",
            description=(
                "Record a buyer lead once you have enough to hand to an "
                "agent.  Minimum: name + phone + budget range + timeline. "
                "Ask for financing status (pre-approved / shopping / cash) "
                "and areas of interest before calling.  Non-residents "
                "additionally get financing_country, has_portuguese_nif, "
                "has_portuguese_bank."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string", "description": "E.164 preferred"},
                    "email": {"type": "string"},
                    "budget_min_eur": {"type": "integer",
                                        "description": "Min budget EUR; 0 if only max given"},
                    "budget_max_eur": {"type": "integer",
                                        "description": "Max budget EUR; 0 if not disclosed"},
                    "financing_status": {"type": "string",
                                          "enum": ["pre_approved", "shopping",
                                                    "cash", "unknown"]},
                    "timeline_months": {"type": "integer",
                                         "description": "Months until intended purchase"},
                    "property_type": {"type": "string",
                                       "enum": ["apartment", "house", "townhouse",
                                                 "loft", "penthouse", "any"]},
                    "bedrooms_min": {"type": "integer"},
                    "preferred_areas": {"type": "array",
                                         "items": {"type": "string"},
                                         "description": "Neighbourhoods of interest"},
                    "must_haves": {"type": "array",
                                    "items": {"type": "string"},
                                    "description": "e.g. 'terrace', 'parking', 'lift'"},
                    "is_non_resident": {"type": "boolean"},
                    "financing_country": {"type": "string",
                                           "description": "Country of financing origin, "
                                                          "for non-residents"},
                    "has_portuguese_nif": {"type": "boolean"},
                    "has_portuguese_bank": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone"],
            },
        ),
        ToolDefinition(
            name="qualify_seller_lead",
            description=(
                "Record a seller lead requesting a valuation.  Take address, "
                "property type, floor area (sqm), condition, timeline, and "
                "reason for selling.  DO NOT quote a valuation on the phone — "
                "book_valuation_visit is the correct follow-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "address": {"type": "string"},
                    "property_type": {"type": "string"},
                    "sqm": {"type": "integer",
                             "description": "Total floor area in square metres"},
                    "bedrooms": {"type": "integer"},
                    "bathrooms": {"type": "integer"},
                    "condition": {"type": "string",
                                   "enum": ["renovated", "good", "needs_work",
                                             "shell", "unknown"]},
                    "timeline_months": {"type": "integer",
                                         "description": "Months until intended sale"},
                    "reason_selling": {"type": "string",
                                        "description": "Free-form; e.g. moving, "
                                                        "downsizing, inheritance"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "address"],
            },
        ),
        ToolDefinition(
            name="qualify_rental_lead",
            description=(
                "Record a rental applicant lead.  Long-term rental only "
                "(short-term / AL is a different flow — use "
                "qualify_investor_lead and note AL intent)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "budget_month_eur": {"type": "integer",
                                          "description": "Monthly rent budget EUR"},
                    "move_in_date": {"type": "string",
                                       "description": "ISO YYYY-MM-DD or 'flexible'"},
                    "contract_months": {"type": "integer",
                                         "description": "Preferred contract length"},
                    "employment_status": {"type": "string",
                                            "enum": ["employed_pt", "employed_foreign",
                                                     "self_employed", "student",
                                                     "retired", "other"]},
                    "occupants_count": {"type": "integer"},
                    "has_pets": {"type": "boolean"},
                    "preferred_areas": {"type": "array",
                                         "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone"],
            },
        ),
        ToolDefinition(
            name="qualify_investor_lead",
            description=(
                "Record an investor lead (buy-to-let, short-term rental / "
                "Alojamento Local).  Books a consultation as the follow-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "capital_available_eur": {"type": "integer"},
                    "target_yield_percent": {"type": "number",
                                              "description": "Target gross yield %"},
                    "target_areas": {"type": "array",
                                      "items": {"type": "string"}},
                    "wants_al_licence": {"type": "boolean",
                                          "description": "Short-term rental / "
                                                          "Alojamento Local"},
                    "financing_status": {"type": "string",
                                          "enum": ["pre_approved", "shopping",
                                                    "cash", "unknown"]},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone"],
            },
        ),

        # ── booking flows (after qualification) ──────────────────────
        ToolDefinition(
            name="check_viewing_availability",
            description=(
                "Check open slots on a given date for viewings, virtual "
                "tours, or valuation visits.  Always call before offering "
                "a specific time.  Slots come back in HH:MM local time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string",
                              "description": "ISO date YYYY-MM-DD"},
                    "duration_minutes": {"type": "integer",
                                          "description": "45 for viewing, "
                                                          "30 for virtual, "
                                                          "60 for valuation"},
                },
                "required": ["date"],
            },
        ),
        ToolDefinition(
            name="book_viewing",
            description=(
                "Book an in-person property viewing.  Requires a "
                "property_ref (address, listing id, or clear description).  "
                "Only call after qualify_buyer_lead has run OR the caller "
                "clearly identifies a property they want to view."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "property_ref": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "viewers_count": {"type": "integer", "default": 1},
                    "working_with_other_agent": {"type": "boolean",
                                                    "default": False},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "property_ref", "start_iso"],
            },
        ),
        ToolDefinition(
            name="book_virtual_tour",
            description=(
                "Book a virtual walkthrough (WhatsApp video or Zoom).  Good "
                "for buyers abroad or on tight schedule.  30 minutes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "property_ref": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "platform": {"type": "string",
                                  "enum": ["whatsapp", "zoom", "either"]},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "property_ref", "start_iso"],
            },
        ),
        ToolDefinition(
            name="book_valuation_visit",
            description=(
                "Book a seller-side home valuation visit.  60 minutes.  "
                "Requires qualify_seller_lead first so the agent visits "
                "with context.  DO NOT use this to schedule a phone-based "
                "valuation — those don't exist here."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "address": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "address", "start_iso"],
            },
        ),
        ToolDefinition(
            name="book_investor_consultation",
            description=(
                "Book an investor consultation.  45 minutes.  Discusses "
                "yield targets, target neighbourhoods, AL licensing status, "
                "financing structure.  Requires qualify_investor_lead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "format": {"type": "string",
                                "enum": ["in_person", "video", "phone"]},
                    "notes": {"type": "string"},
                },
                "required": ["caller_name", "phone", "start_iso"],
            },
        ),

        # ── utility ──────────────────────────────────────────────────
        ToolDefinition(
            name="lookup_faq",
            description=(
                "Look up an FAQ topic (commission, financing, non-resident, "
                "golden visa, CPCV, IMT, AL, closing timeline, school "
                "district, language, areas served).  Never invent — if "
                "no_match, offer to take a message."
            ),
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        ),
        ToolDefinition(
            name="take_message",
            description=(
                "Take a message for a specific agent or department when the "
                "caller does not want a return call right now, OR when out "
                "of hours per human_transfer_rules.outside_hours_action."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caller_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "recipient": {"type": "string",
                                    "description": "Agent name or department"},
                    "subject": {"type": "string"},
                    "message": {"type": "string"},
                    "priority": {"type": "string",
                                   "enum": ["normal", "urgent"]},
                    "preferred_callback_time": {"type": "string"},
                },
                "required": ["caller_name", "phone", "message"],
            },
        ),
        ToolDefinition(
            name="escalate_to_human",
            description=(
                "Warm-transfer to a human agent.  Fire for the "
                "always_transfer_on triggers (complaint, offer > 500k, "
                "legal question, asks_for_specific_agent_by_name) OR "
                "when the caller explicitly requests a person."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "agent_name": {"type": "string",
                                    "description": "If caller asked for a "
                                                    "specific agent by name"},
                    "urgency": {"type": "string",
                                 "enum": ["normal", "high", "urgent"]},
                },
                "required": ["reason"],
            },
        ),
    ]


# ── lead scoring ──────────────────────────────────────────────────
#
# Rebuilt for the real-estate + EUR context.  Old scoring used USD and
# didn't distinguish buyer / seller / rental / investor.  Different
# tools produce different score profiles.


def _score_buyer_lead(*, budget_max: int, timeline_months: int,
                       financing: str, has_areas: bool) -> int:
    score = 20  # baseline for any qualified lead
    # Budget bands calibrated to Lisbon central prices (Ribeira Prime
    # fixture — Baixa/Chiado apartments run €400k-€1.5M).
    if budget_max >= 1_000_000:
        score += 30
    elif budget_max >= 600_000:
        score += 22
    elif budget_max >= 350_000:
        score += 15
    elif budget_max >= 150_000:
        score += 8
    if 0 < timeline_months <= 3:
        score += 20
    elif 3 < timeline_months <= 6:
        score += 12
    elif 6 < timeline_months <= 12:
        score += 5
    # timeline_months == 0 (undisclosed) or > 12 → no bonus
    if financing in ("pre_approved", "cash"):
        score += 20
    elif financing == "shopping":
        score += 8
    if has_areas:
        score += 5
    return min(100, score)


def _score_seller_lead(*, sqm: int, timeline_months: int,
                        condition: str) -> int:
    score = 25
    # Larger property → higher commission → higher priority.
    if sqm >= 200:
        score += 25
    elif sqm >= 120:
        score += 18
    elif sqm >= 70:
        score += 10
    if 0 < timeline_months <= 3:
        score += 20
    elif 3 < timeline_months <= 6:
        score += 12
    elif 6 < timeline_months <= 12:
        score += 5
    # timeline_months == 0 (undisclosed) or > 12 → no bonus
    if condition == "renovated":
        score += 10
    elif condition == "good":
        score += 5
    return min(100, score)


def _score_rental_lead(*, budget_month: int, employment: str,
                        move_in_iso: str) -> int:
    score = 15
    if budget_month >= 3000:
        score += 25
    elif budget_month >= 2000:
        score += 18
    elif budget_month >= 1200:
        score += 10
    elif budget_month >= 700:
        score += 5
    if employment in ("employed_pt", "employed_foreign"):
        score += 15
    elif employment == "self_employed":
        score += 8
    # Sooner move-in → more urgent lead.
    if move_in_iso:
        from datetime import date
        try:
            target = date.fromisoformat(move_in_iso[:10])
            days = (target - date.today()).days
            if days <= 30:
                score += 15
            elif days <= 60:
                score += 10
            elif days <= 120:
                score += 5
        except (ValueError, TypeError):
            pass
    return min(100, score)


def _score_investor_lead(*, capital: int, financing: str) -> int:
    score = 25
    if capital >= 750_000:
        score += 30
    elif capital >= 350_000:
        score += 20
    elif capital >= 150_000:
        score += 10
    if financing in ("pre_approved", "cash"):
        score += 20
    elif financing == "shopping":
        score += 8
    return min(100, score)


# ── handler ───────────────────────────────────────────────────────


class RealEstateToolHandler:
    """Dispatches tool calls for the real-estate vertical.

    2026-08-25 (EU demo): rebuilt against the extended tool set +
    Ribeira Prime fixture.  Handler is transport-agnostic (calendar
    backend abstracted) so tests can inject FakeCalendar and prod can
    use GoogleCalendar with the same semantics.
    """

    TOOL_NAMES = frozenset({
        "qualify_buyer_lead",
        "qualify_seller_lead",
        "qualify_rental_lead",
        "qualify_investor_lead",
        "check_viewing_availability",
        "book_viewing",
        "book_virtual_tour",
        "book_valuation_visit",
        "book_investor_consultation",
        "lookup_faq",
        "take_message",
        "escalate_to_human",
    })

    # Duration lookup by tool name — falls back to sensible defaults.
    _DEFAULT_DURATIONS = {
        "book_viewing": 45,
        "book_virtual_tour": 30,
        "book_valuation_visit": 60,
        "book_investor_consultation": 45,
    }

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    def _service_duration(self, service_name: str) -> int:
        for s in getattr(self.business, "services", []) or []:
            if s.name.lower() == service_name.lower():
                return s.duration_minutes
        return 45  # safe default

    async def __call__(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "check_viewing_availability":
                day = datetime.fromisoformat(call.arguments["date"])
                duration = int(
                    call.arguments.get("duration_minutes")
                    or self._service_duration("Property viewing")
                    or 45
                )
                slots = self.calendar.list_slots(
                    day, duration, open_hhmm="09:00", close_hhmm="19:00",
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "date": call.arguments["date"],
                        "duration_minutes": duration,
                        "open_slots": slots[:10],
                    },
                )

            if call.name in ("book_viewing", "book_virtual_tour",
                              "book_valuation_visit",
                              "book_investor_consultation"):
                return await self._book(call)

            if call.name == "qualify_buyer_lead":
                score = _score_buyer_lead(
                    budget_max=int(call.arguments.get("budget_max_eur") or 0),
                    timeline_months=int(call.arguments.get("timeline_months") or 0),
                    financing=call.arguments.get("financing_status", "unknown"),
                    has_areas=bool(call.arguments.get("preferred_areas")),
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=self._lead_result("buyer", score, call.arguments),
                )

            if call.name == "qualify_seller_lead":
                score = _score_seller_lead(
                    sqm=int(call.arguments.get("sqm") or 0),
                    timeline_months=int(
                        call.arguments.get("timeline_months") or 0
                    ),
                    condition=call.arguments.get("condition", "unknown"),
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=self._lead_result("seller", score, call.arguments),
                )

            if call.name == "qualify_rental_lead":
                score = _score_rental_lead(
                    budget_month=int(
                        call.arguments.get("budget_month_eur") or 0
                    ),
                    employment=call.arguments.get(
                        "employment_status", "other",
                    ),
                    move_in_iso=call.arguments.get("move_in_date", ""),
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=self._lead_result("rental", score, call.arguments),
                )

            if call.name == "qualify_investor_lead":
                score = _score_investor_lead(
                    capital=int(
                        call.arguments.get("capital_available_eur") or 0
                    ),
                    financing=call.arguments.get(
                        "financing_status", "unknown",
                    ),
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=self._lead_result("investor", score, call.arguments),
                )

            if call.name == "lookup_faq":
                topic = (call.arguments.get("topic") or "").lower()
                hits = {
                    q: a
                    for q, a in (getattr(self.business, "faqs", {}) or {}).items()
                    if topic in q.lower()
                }
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=hits or {"_no_match": "no FAQ entry found"},
                )

            if call.name == "take_message":
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "taken": True,
                        "caller_name": call.arguments.get("caller_name"),
                        "phone": call.arguments.get("phone"),
                        "recipient": call.arguments.get("recipient")
                                        or "on-duty agent",
                        "subject": call.arguments.get("subject"),
                        "message": call.arguments.get("message"),
                        "priority": call.arguments.get("priority", "normal"),
                        "preferred_callback_time":
                            call.arguments.get("preferred_callback_time"),
                    },
                )

            if call.name == "escalate_to_human":
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "escalated": True,
                        "reason": call.arguments.get("reason"),
                        "agent_name": call.arguments.get("agent_name"),
                        "urgency": call.arguments.get("urgency", "normal"),
                        "callback_number":
                            getattr(self.business, "escalation_phone", None),
                    },
                )

            return ToolResult(
                tool_call_id=call.id, name=call.name, result=None,
                error=f"unknown tool: {call.name}",
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result=None, error=str(e),
            )

    async def _book(self, call: ToolCall) -> ToolResult:
        """Shared booking path for viewing / virtual / valuation / investor."""
        start = datetime.fromisoformat(call.arguments["start_iso"])
        duration = self._DEFAULT_DURATIONS.get(call.name, 45)
        # Compose a descriptive service label so the calendar row makes
        # sense to the human agent viewing it later.
        if call.name == "book_viewing":
            label = f"Viewing: {call.arguments.get('property_ref', 'TBD')}"
        elif call.name == "book_virtual_tour":
            platform = call.arguments.get("platform", "either")
            label = (
                f"Virtual tour ({platform}): "
                f"{call.arguments.get('property_ref', 'TBD')}"
            )
        elif call.name == "book_valuation_visit":
            label = f"Valuation visit: {call.arguments.get('address', 'TBD')}"
        else:  # book_investor_consultation
            fmt = call.arguments.get("format", "in_person")
            label = f"Investor consultation ({fmt})"
        notes = call.arguments.get("notes") or ""
        extras = []
        if call.name == "book_viewing":
            if call.arguments.get("working_with_other_agent"):
                extras.append("[caller working with another agent]")
            vc = call.arguments.get("viewers_count")
            if vc and int(vc) > 1:
                extras.append(f"[{vc} viewers]")
        if extras:
            notes = (notes + " " + " ".join(extras)).strip()
        outcome = self.calendar.book(
            start=start,
            duration_minutes=duration,
            caller_name=call.arguments["caller_name"],
            phone=call.arguments["phone"],
            service=label,
            notes=notes or None,
        )
        # Attach book_type so downstream sinks + dashboard can filter.
        if isinstance(outcome, dict):
            outcome.setdefault("book_type", call.name)
        return ToolResult(
            tool_call_id=call.id, name=call.name, result=outcome,
        )

    @staticmethod
    def _lead_result(kind: str, score: int, args: dict) -> dict:
        """Uniform lead-result shape for CRM sinks + dashboard."""
        return {
            "qualified": True,
            "lead_kind": kind,
            "lead_score": score,
            "caller_name": args.get("caller_name"),
            "phone": args.get("phone"),
            "email": args.get("email"),
            "notes": args.get("notes"),
            # Vertical-specific fields the sink can flatten as needed.
            "detail": {k: v for k, v in args.items()
                        if k not in ("caller_name", "phone", "email", "notes")},
        }
