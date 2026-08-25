"""Vertical → (tools, handler) mapping.

Called by session_manager once at startup. Adding a new vertical is:
  1. Add sample-data/<vertical>/business.json
  2. Write a <vertical>_tools.py with build_<vertical>_tools() + <Vertical>ToolHandler
  3. Add an if-branch here

Every vertical automatically gets a `lookup_answer` RAG tool composed on top
of its vertical-specific tools. If no KB has been ingested for the business,
the tool returns "no_match" gracefully — no crash, brain moves on.
"""
from __future__ import annotations

from typing import Optional

from packages.schemas import BusinessProfile, ToolDefinition

from .clinic_tools import ClinicToolHandler, build_clinic_tools
from .fake_calendar import FakeCalendar
from .real_estate_tools import RealEstateToolHandler, build_real_estate_tools
from .rag_tool import ComposeHandler, LookupAnswerHandler, build_lookup_answer_tool
from .restaurant_tools import RestaurantToolHandler, build_restaurant_tools
from .wholesaler_tools import WholesalerToolHandler, build_wholesaler_tools


# 2026-08-18: default accepted regions when the tenant hasn't overridden.
# Was ["US"] only, which rejected valid PK/GB/etc numbers on the phone
# precondition with `too_long` and put the LLM into a "please give me a
# 10-digit number" loop against real international callers.  This list
# is a permissive default; a tenant should still narrow it via the
# BusinessProfile once phone_accepted_regions lands there.
_DEFAULT_ACCEPTED_PHONE_REGIONS = ["US", "PK", "GB", "CA", "AU", "IN", "AE", "SG"]


def _phone_region_config(business: BusinessProfile) -> tuple[str, list[str]]:
    """Pick (default_region, accepted_regions) for a business.  Reads
    optional attrs off the BusinessProfile if present so the schema can
    grow to expose these without touching call sites."""
    default_region = getattr(business, "phone_default_region", None) or "US"
    accepted = getattr(business, "phone_accepted_regions", None)
    if not accepted:
        accepted = list(_DEFAULT_ACCEPTED_PHONE_REGIONS)
    # Ensure default is included so parse_phone tries it first.
    if default_region not in accepted:
        accepted = [default_region] + accepted
    return default_region, accepted


def build_tools_for_vertical(
    business: BusinessProfile,
    calendar,
    retriever=None,
    shaper_llm=None,
    confidence_threshold: float = 0.7,
) -> tuple[list[ToolDefinition], object]:
    """Returns (tool_definitions, tool_handler) for the business.vertical value.
    Falls back to clinic tools if the vertical is unknown.

    Args:
        retriever: Optional Retriever. If provided, the `lookup_answer` tool
            is added to every vertical and RAG queries route to it.
        shaper_llm: Optional LLMProvider for the voice-shaper pass. Required
            when retriever is given.
        confidence_threshold: Below this, lookup_answer returns "no answer"
            so the brain escalates rather than speaking a low-confidence hit.
    """
    vertical = (business.vertical or "clinic").lower()
    default_phone_region, accepted_phone_regions = _phone_region_config(business)

    if vertical == "clinic":
        tools = build_clinic_tools()
        handler = ClinicToolHandler(
            business, calendar,
            default_phone_region=default_phone_region,
            accepted_phone_regions=accepted_phone_regions,
        )
    elif vertical == "restaurant":
        tools, handler = build_restaurant_tools(), RestaurantToolHandler(business, calendar)
    elif vertical in ("real_estate", "real-estate", "realestate"):
        tools, handler = build_real_estate_tools(), RealEstateToolHandler(business, calendar)
    elif vertical in ("wholesaler_outbound", "wholesaler", "subto", "subject_to"):
        tools, handler = build_wholesaler_tools(), WholesalerToolHandler(business, calendar)
    else:
        tools = build_clinic_tools()
        handler = ClinicToolHandler(
            business, calendar,
            default_phone_region=default_phone_region,
            accepted_phone_regions=accepted_phone_regions,
        )

    # Compose RAG on top when a retriever is provided
    if retriever is not None and shaper_llm is not None:
        tools = tools + [build_lookup_answer_tool()]
        rag_handler = LookupAnswerHandler(
            business_id=business.id,
            retriever=retriever,
            shaper_llm=shaper_llm,
            confidence_threshold=confidence_threshold,
        )
        # Route lookup_answer to the RAG handler; everything else to the vertical handler
        handler = ComposeHandler([rag_handler, handler])

    return tools, handler
