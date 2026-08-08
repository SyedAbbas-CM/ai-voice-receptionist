"""Restaurant reservation + order + FOH tools.

Nine callable tools plus lookup_faq. Same shape as clinic_tools:
build tool definitions + handler class. Reuses FakeCalendar for slot
management — a reservation is a timed calendar entry with party size.

Tools:
  check_availability    - open slots for a party on a date
  book_reservation      - book a table (registered under the canonical name
                          used by the write-guard / booking dispatch)
  get_menu              - return current menu items with price + category filter
  check_allergen        - which menu items contain a given allergen
  build_order           - assemble a pickup / delivery order + total
  quote_delivery_eta    - realistic pickup or delivery time estimate
  apply_loyalty         - lookup loyalty account, quote points balance / discount
  capture_deposit_link  - send an SMS Stripe deposit link for large parties
  escalate_to_human     - hand off to on-shift manager (canonical escalation name)
  lookup_faq            - FAQ lookup

All handlers return stub-but-realistic data so the brain can be
demoed and the adversarial harness can exercise every branch without
a real POS / Stripe / Twilio integration.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from packages.schemas import BusinessProfile, ToolCall, ToolDefinition, ToolResult

from .fake_calendar import FakeCalendar


# Stub menu — kept here rather than in business.json to keep the
# business profile schema stable across verticals. Real deployments
# would pull this live from Toast / Square.
CORVINA_MENU = [
    # (name, category, price_usd, allergens)
    ("Corvina crudo",              "starter",  18.00, ["fish"]),
    ("Dungeness crab tostada",     "starter",  22.00, ["shellfish", "wheat", "sesame"]),
    ("Roasted beet salad",         "starter",  16.00, ["milk", "tree nuts"]),
    ("Whole roasted branzino",     "main",     46.00, ["fish"]),
    ("Coho salmon a la plancha",   "main",     38.00, ["fish"]),
    ("Peruvian shrimp aji",        "main",     34.00, ["shellfish", "milk"]),
    ("Wild mushroom risotto",      "main",     28.00, ["milk"]),
    ("Kids grilled fish",          "kids",     14.00, ["fish"]),
    ("Kids buttered noodles",      "kids",     10.00, ["wheat", "milk", "egg"]),
    ("Tres leches",                "dessert",  12.00, ["milk", "egg"]),
]


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
            description="Book a confirmed reservation. Only call after the caller has agreed to a specific available time and given you a name and phone number.",
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
            name="get_menu",
            description="Return the current menu. Optionally filter by category (starter, main, dessert, kids) or by a text search.",
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional: starter, main, dessert, kids"},
                    "search": {"type": "string", "description": "Optional: match name substring"},
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="check_allergen",
            description="Return which menu items contain a given allergen. Use FDA nine major allergens: milk, egg, fish, shellfish, tree nuts, peanuts, wheat, soybeans, sesame.",
            parameters={
                "type": "object",
                "properties": {
                    "allergen": {"type": "string", "description": "One of the nine major allergens"},
                },
                "required": ["allergen"],
            },
        ),
        ToolDefinition(
            name="build_order",
            description="Assemble a pickup or delivery order. Returns line items with prices plus subtotal, tax, and total. Call this before confirming a pickup order to read prices back to the caller.",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "List of menu item names to order",
                        "items": {"type": "string"},
                    },
                    "fulfillment": {"type": "string", "description": "pickup or delivery"},
                },
                "required": ["items", "fulfillment"],
            },
        ),
        ToolDefinition(
            name="quote_delivery_eta",
            description="Give a realistic pickup or delivery ETA in minutes based on current kitchen load. Use before quoting a time to the caller.",
            parameters={
                "type": "object",
                "properties": {
                    "fulfillment": {"type": "string", "description": "pickup or delivery"},
                    "party_size_or_items": {"type": "integer", "description": "Number of items (delivery) or guests (pickup)"},
                },
                "required": ["fulfillment"],
            },
        ),
        ToolDefinition(
            name="apply_loyalty",
            description="Look up the caller's loyalty account by phone. Returns points balance and any active reward. Only call if the caller mentions a loyalty account or asks about points.",
            parameters={
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Caller phone in E.164 or 10-digit US"},
                },
                "required": ["phone"],
            },
        ),
        ToolDefinition(
            name="capture_deposit_link",
            description="Send an SMS Stripe deposit link for a large-party reservation (nine or more). Deposit is $200 per the house policy. Only call after the reservation is booked and the caller confirms they want the link now.",
            parameters={
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "reservation_iso": {"type": "string", "description": "ISO datetime the deposit is holding"},
                },
                "required": ["phone", "party_size", "reservation_iso"],
            },
        ),
        ToolDefinition(
            name="escalate_to_human",
            description="Hand off to the on-shift manager. Use for private events, complaints, allergen questions beyond menu labels, refund requests, or repeated caller confusion.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
        ToolDefinition(
            name="lookup_faq",
            description="Look up an FAQ topic from the business profile — hours, corkage, parking, dress code, dietary policy, etc.",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        ),
    ]


def _service_for_party(party_size: int, business: BusinessProfile) -> str:
    """Pick the closest matching Table service by party size."""
    best_match = None
    for s in business.services:
        if s.name.lower().startswith("table for "):
            try:
                n = int(s.name.split()[-1])
                if best_match is None or abs(n - party_size) < abs(best_match[0] - party_size):
                    best_match = (n, s)
            except ValueError:
                continue
    return best_match[1].name if best_match else "Reservation"


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _match_menu_item(name: str) -> tuple[str, str, float, list[str]] | None:
    """Case-insensitive substring match on the stub menu."""
    q = (name or "").strip().lower()
    if not q:
        return None
    # Exact-ish first, then substring
    for item in CORVINA_MENU:
        if item[0].lower() == q:
            return item
    for item in CORVINA_MENU:
        if q in item[0].lower():
            return item
    return None


class RestaurantToolHandler:
    # Audit-3 fix (2026-08-04): explicit tool-name set for ComposeHandler
    # routing.  Prevents the RAG-dispatcher-style silent drop.
    TOOL_NAMES = frozenset({
        "check_availability", "book_reservation", "get_menu",
        "check_allergen", "build_order", "quote_delivery_eta",
        "capture_deposit_link", "apply_loyalty",
        "lookup_faq", "escalate_to_human",
    })

    def __init__(self, business: BusinessProfile, calendar: FakeCalendar) -> None:
        self.business = business
        self.calendar = calendar

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

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
                # Flag large parties so the brain knows to offer the deposit link
                if party >= 9:
                    outcome = {**outcome, "requires_deposit": True, "deposit_usd": 200}
                return ToolResult(tool_call_id=call.id, name=call.name, result=outcome)

            if call.name == "get_menu":
                cat = (call.arguments.get("category") or "").strip().lower()
                search = (call.arguments.get("search") or "").strip().lower()
                items = [
                    {"name": n, "category": c, "price_usd": p, "allergens": a}
                    for (n, c, p, a) in CORVINA_MENU
                    if (not cat or c == cat) and (not search or search in n.lower())
                ]
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"count": len(items), "items": items},
                )

            if call.name == "check_allergen":
                allergen = (call.arguments.get("allergen") or "").strip().lower()
                hits = [n for (n, _c, _p, a) in CORVINA_MENU if allergen in a]
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "allergen": allergen,
                        "items_containing": hits,
                        "shared_kitchen_warning": "All items prepared in a shared kitchen — trace contact possible.",
                    },
                )

            if call.name == "build_order":
                fulfillment = (call.arguments.get("fulfillment") or "pickup").strip().lower()
                if fulfillment not in ("pickup", "delivery"):
                    fulfillment = "pickup"
                line_items = []
                unmatched = []
                subtotal = 0.0
                for name in call.arguments.get("items", []) or []:
                    match = _match_menu_item(name)
                    if match is None:
                        unmatched.append(name)
                        continue
                    line_items.append({"name": match[0], "price_usd": match[2]})
                    subtotal += match[2]
                # Portland OR = no sales tax on prepared food. Keep 0 so numbers
                # match reality; if we open in a taxed jurisdiction, patch here.
                tax = 0.0
                total = round(subtotal + tax, 2)
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "fulfillment": fulfillment,
                        "line_items": line_items,
                        "unmatched": unmatched,
                        "subtotal_usd": round(subtotal, 2),
                        "tax_usd": tax,
                        "total_usd": total,
                    },
                )

            if call.name == "quote_delivery_eta":
                fulfillment = (call.arguments.get("fulfillment") or "pickup").strip().lower()
                # Deterministic pseudo-load based on current 10-minute bucket so
                # repeated calls in a demo give a stable answer without RNG.
                now = datetime.now()
                bucket = f"{now.hour}:{now.minute // 10}"
                load = int(hashlib.md5(bucket.encode()).hexdigest(), 16) % 5   # 0..4
                if fulfillment == "delivery":
                    # We don't do 3rd-party delivery — should be caught upstream
                    # by lookup_faq('delivery'), but return a hint if the LLM asks.
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={
                            "fulfillment": "delivery",
                            "eta_minutes": None,
                            "note": "Corvina does not offer delivery. Pickup only.",
                        },
                    )
                eta = 20 + load * 5   # 20..40 min pickup
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={"fulfillment": "pickup", "eta_minutes": eta},
                )

            if call.name == "apply_loyalty":
                phone = _normalize_phone(call.arguments.get("phone") or "")
                if not phone:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, result=None,
                        error="phone required",
                    )
                # Deterministic stub: hash the phone to a points balance so
                # the same caller "owns" the same account across the demo.
                h = int(hashlib.md5(phone.encode()).hexdigest(), 16)
                points = (h % 900) + 100     # 100..999
                enrolled = (h % 3) != 0      # ~66% enrolled
                if not enrolled:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name,
                        result={"phone_last4": phone[-4:], "enrolled": False},
                    )
                reward = "Free dessert at 500 points" if points >= 500 else None
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "phone_last4": phone[-4:], "enrolled": True,
                        "points_balance": points, "available_reward": reward,
                    },
                )

            if call.name == "capture_deposit_link":
                party = int(call.arguments["party_size"])
                if party < 9:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, result=None,
                        error="Deposits only required for parties of 9 or more.",
                    )
                phone = _normalize_phone(call.arguments["phone"])
                iso = call.arguments["reservation_iso"]
                # Stub link — real impl would POST to Stripe Payment Links + Twilio.
                token = hashlib.sha256(f"{phone}:{iso}".encode()).hexdigest()[:12]
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result={
                        "sent": True,
                        "phone_last4": phone[-4:],
                        "amount_usd": 200,
                        "expires_in_hours": 24,
                        "link_id": token,
                    },
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

            if call.name == "lookup_faq":
                topic = (call.arguments.get("topic") or "").lower()
                hits = {q: a for q, a in self.business.faqs.items() if topic in q.lower()}
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=hits or {"_no_match": "no FAQ entry found"},
                )

            return ToolResult(
                tool_call_id=call.id, name=call.name, result=None,
                error=f"unknown tool: {call.name}",
            )
        except Exception as e:
            return ToolResult(tool_call_id=call.id, name=call.name, result=None, error=str(e))
