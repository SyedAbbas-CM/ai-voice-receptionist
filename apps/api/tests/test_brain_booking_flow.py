from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.core_agent import ReceptionistBrain
from packages.integrations import FakeCalendar, build_clinic_tools, ClinicToolHandler
from packages.schemas import (
    BusinessProfile,
    CallState,
    CallStatus,
    ToolCall,
    ToolDefinition,
)


class ScriptedLLM(LLMProvider):
    """Returns a queue of canned responses in order. Lets us test the brain
    without hitting any network."""

    name = "scripted"

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    async def complete(
        self,
        messages: list[dict],
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.calls.append(messages)
        if not self.script:
            return LLMResponse(text="(no more scripted replies)")
        return self.script.pop(0)


@pytest.fixture
def business() -> BusinessProfile:
    repo_root = Path(__file__).resolve().parents[3]
    data = json.loads((repo_root / "sample-data" / "clinic" / "business.json").read_text())
    return BusinessProfile(**data)


@pytest.fixture
def calendar(tmp_path: Path) -> FakeCalendar:
    return FakeCalendar(tmp_path / "cal.json")


@pytest.mark.asyncio
async def test_greeting_does_not_call_llm(business, calendar):
    llm = ScriptedLLM([])
    brain = ReceptionistBrain(
        llm=llm,
        business=business,
        tools=build_clinic_tools(),
        tool_handler=ClinicToolHandler(business, calendar),
        extractor_llm=llm,
    )
    state = CallState(session_id="t1", business_id=business.id)
    result = await brain.greet(state)
    assert business.name in result.reply
    assert llm.calls == [], "greet() must not hit the LLM"
    assert len(state.transcript) == 1


@pytest.mark.asyncio
async def test_booking_tool_loop(business, calendar):
    target_day = (datetime.utcnow() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    check_args = {"service": "General consultation", "date": target_day.date().isoformat()}
    book_args = {
        "caller_name": "John Carter",
        "phone": "5550104432",
        "service": "General consultation",
        "start_iso": target_day.isoformat(),
    }

    extraction_reply = LLMResponse(text=json.dumps({
        "caller_name": "John Carter",
        "phone": "5550104432",
        "intent": "book_appointment",
        "service": "General consultation",
        "preferred_date": target_day.date().isoformat(),
        "preferred_time": "10:00",
        "urgency": "low",
        "lead_score": 80,
        "summary": "Booked consultation",
    }))

    script = [
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="check_availability", arguments=check_args)]),
        LLMResponse(text="", tool_calls=[ToolCall(id="c2", name="book_appointment", arguments=book_args)]),
        # write_guard checks book_appointment via extractor_llm (same scripted llm)
        LLMResponse(text="APPROVE"),
        LLMResponse(text="You're booked for 10am tomorrow. See you then."),
        extraction_reply,
    ]
    llm = ScriptedLLM(script)
    brain = ReceptionistBrain(
        llm=llm,
        business=business,
        tools=build_clinic_tools(),
        tool_handler=ClinicToolHandler(business, calendar),
        extractor_llm=llm,
    )
    state = CallState(session_id="t2", business_id=business.id)
    await brain.greet(state)
    result = await brain.handle_user_turn(state, "Book me a consultation tomorrow at 10am, John Carter, 5550104432")

    assert "booked" in result.reply.lower() or "10" in result.reply
    tool_names = [tr["name"] for tr in result.tool_results]
    assert tool_names == ["check_availability", "book_appointment"]
    assert result.tool_results[1]["result"]["booked"] is True
    assert state.extracted.intent.value == "book_appointment"
    assert state.extracted.caller_name == "John Carter"


@pytest.mark.asyncio
async def test_escalation_path(business, calendar):
    """As of Sprint 1 (2026-07-18) chest pain is intercepted by the emergency
    classifier BEFORE the LLM ever runs — that's the safety design, not a
    regression. The LLM is never asked and the extractor won't run either."""
    script = [
        # These responses are set up but should NEVER be consumed —
        # the emergency intercept short-circuits the whole loop.
        LLMResponse(text="Should not be called — emergency intercept fires first."),
    ]
    llm = ScriptedLLM(script)
    brain = ReceptionistBrain(
        llm=llm,
        business=business,
        tools=build_clinic_tools(),
        tool_handler=ClinicToolHandler(business, calendar),
        extractor_llm=llm,
    )
    state = CallState(session_id="t3", business_id=business.id)
    result = await brain.handle_user_turn(state, "I'm having chest pain")
    assert result.escalated is True
    assert state.status == CallStatus.ESCALATED
    # Reply should be the canned emergency escalation, mentioning 911 in spoken form
    assert "nine one one" in result.reply.lower()
    # Brain's main tool-loop LLM must NOT run — that's the safety guarantee.
    # (The extractor runs afterward for record-keeping — that's fine and expected.)
    # Distinguish: the main-loop system prompt is the receptionist prompt;
    # the extractor prompt starts with "You read a receptionist transcript".
    main_loop_calls = [
        c for c in llm.calls
        if not any("You read a receptionist transcript" in m.get("content", "")
                    for m in c if m.get("role") == "system")
    ]
    assert len(main_loop_calls) == 0, (
        f"Main-loop LLM must not run when emergency intercept fires; got {len(main_loop_calls)} main calls"
    )
