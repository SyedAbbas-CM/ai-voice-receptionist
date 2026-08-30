"""Audit Gap 5 tests: sub-type Follow-up visit by original procedure.

Container services (Follow-up visit is canonical) need duration
sub-typing based on WHAT the follow-up is for.  Post-implant ≠
post-crown ≠ post-antibiotic ≠ post-filling.

Two layers:
  1. ServiceOffering.duration_by_original_procedure map (fixture)
  2. _service_duration(name, original_procedure=) reads the map
  3. Brain augmenter injects state._discovery_answers['original_procedure']
     into book_appointment + check_availability args before tool dispatch

Backward-compat: legacy callers passing only `name` still work.
"""
from __future__ import annotations

import pytest

from packages.integrations.clinic_tools import ClinicToolHandler
from packages.integrations.fake_calendar import FakeCalendar
from packages.schemas import (
    BusinessHours, BusinessProfile, ServiceOffering,
)


def _business_with_followup_map():
    return BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit",
                duration_minutes=30,
                description="",
                duration_by_original_procedure={
                    "implant":    30,
                    "root canal": 45,
                    "crown":      60,
                    "extraction": 15,
                    "antibiotic": 15,
                    "filling":    20,
                },
            ),
            ServiceOffering(
                name="Adult cleaning", duration_minutes=45,
                description="",
            ),
        ],
    )


@pytest.fixture
def handler(tmp_path):
    return ClinicToolHandler(
        business=_business_with_followup_map(),
        calendar=FakeCalendar(path=tmp_path / "cal.json"),
    )


# ── schema + fixture ─────────────────────────────────


def test_service_offering_has_duration_by_original_procedure_field():
    """ServiceOffering must accept the new field without breaking
    existing constructions."""
    s = ServiceOffering(name="X", duration_minutes=30)
    assert s.duration_by_original_procedure == {}


def test_service_offering_populates_map():
    s = ServiceOffering(
        name="Follow-up visit",
        duration_minutes=30,
        duration_by_original_procedure={"implant": 30},
    )
    assert s.duration_by_original_procedure["implant"] == 30


def test_fixture_clinic_follow_up_has_populated_map():
    """The shipped clinic fixture should have the map populated on
    Follow-up visit."""
    import json
    from pathlib import Path
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "sample-data" / "clinic" / "business.json"
    )
    if not fixture_path.exists():
        pytest.skip("clinic fixture not present")
    data = json.loads(fixture_path.read_text())
    follow_up = next(
        s for s in data["services"] if s["name"] == "Follow-up visit"
    )
    assert "duration_by_original_procedure" in follow_up
    assert follow_up["duration_by_original_procedure"]["implant"] == 30
    assert follow_up["duration_by_original_procedure"]["crown"] == 60


# ── _service_duration override logic ────────────────


def test_service_duration_ignores_map_when_no_original_procedure(handler):
    """Legacy signature: name only → returns base duration_minutes."""
    d = handler._service_duration("Follow-up visit")
    assert d == 30


def test_service_duration_uses_map_when_procedure_matches(handler):
    """'implant' → 30min from map."""
    d = handler._service_duration(
        "Follow-up visit", original_procedure="implant"
    )
    assert d == 30


def test_service_duration_crown_gets_60min(handler):
    """Post-crown seat is a real 60min visit, not a 30min recheck."""
    d = handler._service_duration(
        "Follow-up visit", original_procedure="crown"
    )
    assert d == 60


def test_service_duration_antibiotic_gets_15min(handler):
    d = handler._service_duration(
        "Follow-up visit", original_procedure="antibiotic recheck"
    )
    assert d == 15


def test_service_duration_substring_match_works(handler):
    """'post-implant osseointegration check' contains 'implant' →
    30min match."""
    d = handler._service_duration(
        "Follow-up visit",
        original_procedure="post-implant osseointegration check",
    )
    assert d == 30


def test_service_duration_unknown_procedure_falls_back(handler):
    """Procedure not in the map → base duration_minutes."""
    d = handler._service_duration(
        "Follow-up visit",
        original_procedure="something random",
    )
    assert d == 30


def test_service_duration_case_insensitive(handler):
    """'CROWN' → matches 'crown' key."""
    d = handler._service_duration(
        "Follow-up visit", original_procedure="CROWN placement",
    )
    assert d == 60


def test_service_duration_regular_service_unaffected(handler):
    """Adult cleaning has no map — original_procedure is ignored."""
    d = handler._service_duration(
        "Adult cleaning", original_procedure="crown",
    )
    assert d == 45


def test_service_duration_unknown_service_returns_default(handler):
    """Unknown service name → 30 fallback (existing behavior
    preserved)."""
    d = handler._service_duration(
        "Nonexistent service", original_procedure="implant",
    )
    assert d == 30


# ── tool schema ──────────────────────────────────────


def test_book_appointment_schema_has_original_procedure():
    from packages.integrations.clinic_tools import build_clinic_tools
    tools = build_clinic_tools()
    book = next(t for t in tools if t.name == "book_appointment")
    assert "original_procedure" in book.parameters["properties"]
    # NOT in required — auto-populated by augmenter or omitted.
    assert "original_procedure" not in book.parameters["required"]


def test_check_availability_schema_has_original_procedure():
    from packages.integrations.clinic_tools import build_clinic_tools
    tools = build_clinic_tools()
    ca = next(t for t in tools if t.name == "check_availability")
    assert "original_procedure" in ca.parameters["properties"]


# ── end-to-end tool call routing ────────────────────


@pytest.mark.asyncio
async def test_book_appointment_uses_subtyped_duration(handler):
    """When book_appointment receives original_procedure='crown',
    the calendar write should reserve 60min not the 30min base."""
    from packages.schemas import ToolCall
    from datetime import datetime, timedelta
    result = await handler(ToolCall(
        id="1", name="book_appointment",
        arguments={
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
            "original_procedure": "crown",
        },
    ))
    assert result.result.get("booked") is True
    event = result.result["event"]
    # Duration in event is end - start.
    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    assert (end - start) == timedelta(minutes=60)


@pytest.mark.asyncio
async def test_book_appointment_without_original_procedure_uses_base(
    handler,
):
    """No original_procedure arg → base 30min (backward compat)."""
    from packages.schemas import ToolCall
    from datetime import datetime, timedelta
    result = await handler(ToolCall(
        id="1", name="book_appointment",
        arguments={
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
        },
    ))
    assert result.result.get("booked") is True
    event = result.result["event"]
    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    assert (end - start) == timedelta(minutes=30)


# ── brain augmenter injection ───────────────────────


class _ScriptedLLM:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        from apps.api.app.providers.base import LLMResponse
        from packages.schemas import ToolCall
        if not self.script:
            return LLMResponse(text="ok", tool_calls=[],
                                finish_reason="stop", raw={})
        item = self.script.pop(0)
        if isinstance(item, dict) and "tool" in item:
            tc = ToolCall(
                id="call_1", name=item["tool"],
                arguments=item.get("args", {}),
            )
            return LLMResponse(
                text="", tool_calls=[tc],
                finish_reason="tool_calls", raw={},
            )
        return LLMResponse(
            text=item if isinstance(item, str) else str(item),
            tool_calls=[], finish_reason="stop", raw={},
        )


@pytest.mark.asyncio
async def test_brain_augmenter_injects_original_procedure_on_book(
    monkeypatch, tmp_path,
):
    """When state._discovery_answers has original_procedure,
    augmenter injects it into book_appointment args before dispatch."""
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import (
        ClinicToolHandler, build_clinic_tools,
    )
    from packages.schemas import CallState, ToolResult

    biz = _business_with_followup_map()
    inner_handler = ClinicToolHandler(
        business=biz, calendar=FakeCalendar(path=tmp_path / "cal.json"),
    )
    captured_args = []

    async def _capture_handler(call):
        captured_args.append(dict(call.arguments or {}))
        return await inner_handler(call)

    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-03T10:00",
        }},
        "You're booked.",
    ])
    brain = ReceptionistBrain(
        llm=llm, business=biz,
        tools=build_clinic_tools(),
        tool_handler=_capture_handler,
        extractor_llm=llm,
    )
    state = CallState(session_id="CAgap5", business_id="biz1")
    # Simulate a completed discovery orchestrator's teardown.
    state._discovery_answers = {
        "original_procedure": "root canal recheck",
        "original_provider": "Dr. Chen",
        "original_visit_date": "August 15th",
    }
    state._discovery_notes_prefix = ""   # cleared already by real code
    # Bypass write-guard (test scope — validate_write signature quirks).
    from packages.core_agent.classifiers import write_guard as _wg
    orig_v = _wg.validate_write
    async def _ok(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg.validate_write = _ok
    try:
        await brain.handle_user_turn(state, "book me a follow-up")
    finally:
        _wg.validate_write = orig_v

    booking_calls = [
        c for c in captured_args
        if "caller_name" in c   # book_appointment shape
    ]
    assert booking_calls
    assert booking_calls[0].get("original_procedure") == (
        "root canal recheck"
    )


@pytest.mark.asyncio
async def test_brain_augmenter_no_op_when_llm_already_passed(
    monkeypatch, tmp_path,
):
    """If LLM already passed original_procedure, augmenter must NOT
    overwrite it (LLM's explicit answer wins)."""
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import (
        ClinicToolHandler, build_clinic_tools,
    )
    from packages.schemas import CallState

    biz = _business_with_followup_map()
    inner_handler = ClinicToolHandler(
        business=biz, calendar=FakeCalendar(path=tmp_path / "cal.json"),
    )
    captured = []

    async def _cap_handler(call):
        captured.append(dict(call.arguments or {}))
        return await inner_handler(call)

    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "A",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-04T10:00",
            "original_procedure": "LLM_EXPLICIT_ANSWER",
        }},
        "Done.",
    ])
    brain = ReceptionistBrain(
        llm=llm, business=biz,
        tools=build_clinic_tools(),
        tool_handler=_cap_handler,
        extractor_llm=llm,
    )
    state = CallState(session_id="CAgap5b", business_id="biz1")
    state._discovery_answers = {
        "original_procedure": "DISCOVERY_ANSWER",
    }
    from packages.core_agent.classifiers import write_guard as _wg2
    ov = _wg2.validate_write
    async def _ok2(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg2.validate_write = _ok2
    try:
        await brain.handle_user_turn(state, "book")
    finally:
        _wg2.validate_write = ov

    booking = [c for c in captured if "caller_name" in c][0]
    assert booking["original_procedure"] == "LLM_EXPLICIT_ANSWER"
