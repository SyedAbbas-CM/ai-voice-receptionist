from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from packages.integrations import (
    FakeCalendar,
    RealEstateToolHandler,
    RestaurantToolHandler,
    WholesalerToolHandler,
    build_real_estate_tools,
    build_restaurant_tools,
    build_tools_for_vertical,
    build_wholesaler_tools,
)
from packages.schemas import BusinessProfile, ToolCall


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(vertical_folder: str) -> BusinessProfile:
    data = json.loads((REPO_ROOT / "sample-data" / vertical_folder / "business.json").read_text())
    return BusinessProfile(**data)


@pytest.fixture
def restaurant_business():
    return _load("restaurant")


@pytest.fixture
def real_estate_business():
    return _load("real-estate")


@pytest.fixture
def calendar(tmp_path):
    return FakeCalendar(tmp_path / "cal.json")


def test_restaurant_business_json_loads(restaurant_business):
    assert restaurant_business.vertical == "restaurant"
    assert any(s.name.startswith("Table for") for s in restaurant_business.services)


def test_real_estate_business_json_loads(real_estate_business):
    assert real_estate_business.vertical == "real_estate"
    assert any(s.name == "Viewing" for s in real_estate_business.services)


def test_vertical_factory_picks_right_handler(restaurant_business, real_estate_business, calendar):
    r_tools, r_handler = build_tools_for_vertical(restaurant_business, calendar)
    tool_names = {t.name for t in r_tools}
    assert "book_reservation" in tool_names
    assert isinstance(r_handler, RestaurantToolHandler)

    re_tools, re_handler = build_tools_for_vertical(real_estate_business, calendar)
    re_tool_names = {t.name for t in re_tools}
    assert "book_viewing" in re_tool_names
    assert "qualify_lead" in re_tool_names
    assert isinstance(re_handler, RealEstateToolHandler)


@pytest.mark.asyncio
async def test_restaurant_book_reservation_flow(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)
    tomorrow = (datetime.utcnow() + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)

    check = await handler(ToolCall(
        id="c1", name="check_availability",
        arguments={"party_size": 4, "date": tomorrow.date().isoformat()},
    ))
    assert check.error is None
    assert "open_slots" in check.result

    book = await handler(ToolCall(
        id="c2", name="book_reservation",
        arguments={
            "caller_name": "Jane Doe",
            "phone": "5551234567",
            "party_size": 4,
            "start_iso": tomorrow.isoformat(),
            "notes": "birthday, one vegetarian",
        },
    ))
    assert book.error is None
    assert book.result["booked"] is True
    assert "party of 4" in book.result["event"]["service"]


@pytest.mark.asyncio
async def test_real_estate_qualify_and_book(real_estate_business, calendar):
    handler = RealEstateToolHandler(real_estate_business, calendar)
    tomorrow = (datetime.utcnow() + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)

    qualify = await handler(ToolCall(
        id="c1", name="qualify_lead",
        arguments={
            "caller_name": "John Buyer",
            "phone": "5551239876",
            "intent": "buy",
            "budget_max_usd": 750000,
            "timeline": "this month",
            "financing_status": "pre_approved",
            "areas": ["Downtown", "Riverside"],
            "notes": "wants 3-bed minimum",
        },
    ))
    assert qualify.error is None
    assert qualify.result["qualified"] is True
    assert qualify.result["lead_score"] >= 60  # buy + pre-approved + this-month + $750k

    book = await handler(ToolCall(
        id="c2", name="book_viewing",
        arguments={
            "caller_name": "John Buyer",
            "phone": "5551239876",
            "property_ref": "412 Maple Street",
            "start_iso": tomorrow.isoformat(),
        },
    ))
    assert book.error is None
    assert book.result["booked"] is True


def test_real_estate_lead_scoring_ranges(real_estate_business, calendar):
    handler = RealEstateToolHandler(real_estate_business, calendar)
    # tire kicker
    from packages.integrations.real_estate_tools import _score_lead
    assert _score_lead("other", 0, "just browsing", "unknown") < 30
    # hot lead
    assert _score_lead("buy", 1_200_000, "this week", "pre_approved") >= 80


@pytest.mark.asyncio
async def test_restaurant_faq_lookup(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)
    res = await handler(ToolCall(id="c1", name="lookup_faq", arguments={"topic": "corkage"}))
    assert res.error is None
    assert any("corkage" in k.lower() for k in res.result.keys())


@pytest.mark.asyncio
async def test_restaurant_menu_and_allergen(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)

    all_menu = await handler(ToolCall(id="m1", name="get_menu", arguments={}))
    assert all_menu.error is None
    assert all_menu.result["count"] >= 8

    mains = await handler(ToolCall(id="m2", name="get_menu", arguments={"category": "main"}))
    assert mains.error is None
    assert all(i["category"] == "main" for i in mains.result["items"])

    shellfish = await handler(ToolCall(id="a1", name="check_allergen", arguments={"allergen": "shellfish"}))
    assert shellfish.error is None
    assert "Dungeness crab tostada" in shellfish.result["items_containing"]
    assert "shared" in shellfish.result["shared_kitchen_warning"].lower()


@pytest.mark.asyncio
async def test_restaurant_build_order_and_eta(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)

    order = await handler(ToolCall(
        id="o1", name="build_order",
        arguments={"items": ["Corvina crudo", "Whole roasted branzino", "not on menu"], "fulfillment": "pickup"},
    ))
    assert order.error is None
    assert len(order.result["line_items"]) == 2
    assert order.result["unmatched"] == ["not on menu"]
    assert order.result["total_usd"] == 64.00   # 18 + 46

    eta = await handler(ToolCall(id="e1", name="quote_delivery_eta", arguments={"fulfillment": "pickup"}))
    assert eta.error is None
    assert 20 <= eta.result["eta_minutes"] <= 40

    no_delivery = await handler(ToolCall(id="e2", name="quote_delivery_eta", arguments={"fulfillment": "delivery"}))
    assert no_delivery.error is None
    assert no_delivery.result["eta_minutes"] is None


@pytest.mark.asyncio
async def test_restaurant_loyalty_and_deposit(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)

    loyalty = await handler(ToolCall(id="l1", name="apply_loyalty", arguments={"phone": "5035550199"}))
    assert loyalty.error is None
    assert loyalty.result["phone_last4"] == "0199"

    # deposit rejected for small party
    small = await handler(ToolCall(
        id="d1", name="capture_deposit_link",
        arguments={"phone": "5035550199", "party_size": 4, "reservation_iso": "2026-08-01T19:00"},
    ))
    assert small.error is not None

    # deposit accepted for large party
    big = await handler(ToolCall(
        id="d2", name="capture_deposit_link",
        arguments={"phone": "5035550199", "party_size": 12, "reservation_iso": "2026-08-01T19:00"},
    ))
    assert big.error is None
    assert big.result["sent"] is True
    assert big.result["amount_usd"] == 200


@pytest.mark.asyncio
async def test_restaurant_large_party_flags_deposit(restaurant_business, calendar):
    handler = RestaurantToolHandler(restaurant_business, calendar)
    when = (datetime.utcnow() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    book = await handler(ToolCall(
        id="b1", name="book_reservation",
        arguments={
            "caller_name": "Marisol Party", "phone": "5035550199",
            "party_size": 10, "start_iso": when.isoformat(),
        },
    ))
    assert book.error is None
    assert book.result.get("requires_deposit") is True
    assert book.result.get("deposit_usd") == 200


def test_restaurant_registers_all_nine_tools(restaurant_business, calendar):
    tools = build_restaurant_tools()
    names = {t.name for t in tools}
    assert names == {
        "check_availability", "book_reservation", "get_menu", "check_allergen",
        "build_order", "quote_delivery_eta", "apply_loyalty", "capture_deposit_link",
        "escalate_to_human", "lookup_faq",
    }


def test_unknown_vertical_falls_back_to_clinic(calendar):
    biz = BusinessProfile(id="x", name="X", vertical="widget_shop")
    tools, handler = build_tools_for_vertical(biz, calendar)
    tool_names = {t.name for t in tools}
    assert "check_availability" in tool_names  # clinic default
    assert "book_appointment" in tool_names


# ---------------- wholesaler / SubtoDealz vertical ----------------

@pytest.fixture
def wholesaler_business():
    return _load("subtodealz")


def test_wholesaler_business_json_loads(wholesaler_business):
    assert wholesaler_business.vertical == "wholesaler_outbound"
    assert "what is seller financing" in wholesaler_business.faqs
    assert "lender" in wholesaler_business.faqs["what is seller financing"].lower()


def test_wholesaler_vertical_wires_correct_handler(wholesaler_business, calendar):
    tools, handler = build_tools_for_vertical(wholesaler_business, calendar)
    tool_names = {t.name for t in tools}
    assert tool_names == {
        "capture_disposition", "record_rent_update", "lookup_faq", "escalate_to_human",
    }
    assert isinstance(handler, WholesalerToolHandler)


@pytest.mark.asyncio
async def test_wholesaler_capture_disposition_flow(wholesaler_business, calendar):
    handler = WholesalerToolHandler(wholesaler_business, calendar)
    from packages.schemas import ToolCall

    result = await handler(ToolCall(
        id="c1",
        name="capture_disposition",
        arguments={
            "disposition": "HOT_LEAD",
            "notes": "Asked how seller financing would work; wants a followup call",
        },
    ))
    assert result.error is None
    assert result.result["recorded"] is True
    assert handler.captured_disposition["disposition"] == "HOT_LEAD"


@pytest.mark.asyncio
async def test_wholesaler_rejects_invalid_disposition(wholesaler_business, calendar):
    handler = WholesalerToolHandler(wholesaler_business, calendar)
    from packages.schemas import ToolCall

    result = await handler(ToolCall(
        id="c1", name="capture_disposition",
        arguments={"disposition": "MAYBE_LATER"},  # not in the enum
    ))
    assert result.error is not None
    assert "invalid disposition" in result.error


@pytest.mark.asyncio
async def test_wholesaler_records_rent_update(wholesaler_business, calendar):
    handler = WholesalerToolHandler(wholesaler_business, calendar)
    from packages.schemas import ToolCall

    result = await handler(ToolCall(
        id="c1", name="record_rent_update",
        arguments={"new_rent_amount": 1850, "confidence": "confirmed"},
    ))
    assert result.error is None
    assert result.result["new_rent_amount"] == 1850
    assert handler.rent_update["new_rent_amount"] == 1850


@pytest.mark.asyncio
async def test_wholesaler_rent_update_rejects_non_integer(wholesaler_business, calendar):
    handler = WholesalerToolHandler(wholesaler_business, calendar)
    from packages.schemas import ToolCall

    result = await handler(ToolCall(
        id="c1", name="record_rent_update",
        arguments={"new_rent_amount": "twenty bucks"},
    ))
    assert result.error is not None
    assert "integer" in result.error.lower()


@pytest.mark.asyncio
async def test_wholesaler_faq_lookup(wholesaler_business, calendar):
    handler = WholesalerToolHandler(wholesaler_business, calendar)
    from packages.schemas import ToolCall
    result = await handler(ToolCall(
        id="c1", name="lookup_faq",
        arguments={"topic": "seller financing"},
    ))
    assert result.error is None
    assert any("financing" in q.lower() for q in result.result.keys())
