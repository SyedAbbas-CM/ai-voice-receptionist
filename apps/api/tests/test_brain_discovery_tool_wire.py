"""End-to-end brain integration for task #151.

When LLM calls answer_context_task during a discovery turn, brain
intercepts + advances orchestrator BEFORE the normal tool_handler
dispatch runs.
"""
from __future__ import annotations

from typing import Any

import pytest


class _ScriptedLLM:
    """Returns a scripted sequence of tool calls + final text.

    Each response is either a plain-text response or a tool call.
    """
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list):
        self.script = list(script)
        self.calls_made = []
        self.tools_seen = []

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        self.calls_made.append(site)
        self.tools_seen.append(
            [t.name for t in (tools or [])]
        )
        from apps.api.app.providers.base import LLMResponse
        from packages.schemas import ToolCall
        if not self.script:
            return LLMResponse(text="ok", tool_calls=[],
                                finish_reason="stop", raw={})
        item = self.script.pop(0)
        if isinstance(item, dict) and "tool" in item:
            tc = ToolCall(
                id=f"call_{len(self.calls_made)}",
                name=item["tool"],
                arguments=item.get("args", {}),
            )
            return LLMResponse(
                text="", tool_calls=[tc],
                finish_reason="tool_calls", raw={},
            )
        # Plain text
        return LLMResponse(
            text=item if isinstance(item, str) else str(item),
            tool_calls=[], finish_reason="stop", raw={},
        )


async def _null_tool_handler(call):
    from packages.schemas import ToolResult
    return ToolResult(
        tool_call_id=call.id, name=call.name,
        result={"stub": True},
    )


def _brain(llm):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import (
        BusinessProfile, BusinessHours, ServiceOffering,
    )
    business = BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="",
            ),
        ],
    )
    return ReceptionistBrain(
        llm=llm, business=business,
        tools=build_clinic_tools(),
        tool_handler=_null_tool_handler,
        extractor_llm=llm,
    )


@pytest.mark.asyncio
async def test_discovery_tools_present_in_llm_call(monkeypatch):
    """When discovery is active, the LLM should see the discovery
    tools alongside base tools."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _ScriptedLLM(script=["Sure - a follow-up to what?"])
    brain = _brain(llm)
    state = CallState(session_id="CAdisc_tools", business_id="biz1")
    await brain.handle_user_turn(state, "book me a follow-up")
    # Orchestrator should be attached now.
    assert state._context_discovery is not None
    # The tools list passed to the LLM should include answer_context_task.
    assert any(
        "answer_context_task" in seen for seen in llm.tools_seen
    )


@pytest.mark.asyncio
async def test_answer_tool_call_advances_orchestrator(monkeypatch):
    """LLM calls answer_context_task → brain intercepts → orchestrator
    advances → next turn asks the next question."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    # Two-step script: first call = answer_context_task, second = plain text.
    llm = _ScriptedLLM(script=[
        {"tool": "answer_context_task", "args": {"answer": "filling"}},
        "Got it, filling — and who did the work?",
    ])
    brain = _brain(llm)
    state = CallState(
        session_id="CAdisc_answer", business_id="biz1",
    )
    # Turn 1: caller says 'follow-up' → orchestrator opens, agent asks.
    await brain.handle_user_turn(state, "book me a follow-up for a filling")
    # Assert orchestrator advanced past task 1 (via the tool call
    # LLM made in the same handle_user_turn — the tool loop runs
    # to completion before returning).
    assert state._context_discovery is not None
    assert state._context_discovery.current_task().task_id == (
        "original_provider"
    )
    assert state._context_discovery.tasks[
        "original_procedure"
    ].result == "filling"


@pytest.mark.asyncio
async def test_answer_receipt_recorded_in_transcript(monkeypatch):
    """The tool call + synthetic receipt should show in state.transcript
    so the trace view can render it."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState, TurnRole
    llm = _ScriptedLLM(script=[
        {"tool": "answer_context_task", "args": {"answer": "filling"}},
        "Perfect.",
    ])
    brain = _brain(llm)
    state = CallState(session_id="CAdisc_rec", business_id="biz1")
    await brain.handle_user_turn(state, "follow-up for a filling")
    tool_turns = [t for t in state.transcript if t.role == TurnRole.TOOL]
    assert any(
        t.tool_name == "answer_context_task" for t in tool_turns
    )
    # The tool_result should contain ok=True + the task_id.
    ans_turn = next(
        t for t in tool_turns if t.tool_name == "answer_context_task"
    )
    assert ans_turn.tool_result["result"]["ok"] is True
    assert ans_turn.tool_result["result"]["task_id"] == (
        "original_procedure"
    )


@pytest.mark.asyncio
async def test_normal_tool_still_dispatched_when_discovery_active(
    monkeypatch,
):
    """LLM calling a non-discovery tool (e.g. lookup_faq) during
    discovery should still hit tool_handler normally — the intercept
    only fires on discovery tool names."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState

    handler_calls = []

    async def _tracking_handler(call):
        from packages.schemas import ToolResult
        handler_calls.append(call.name)
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={"stub": True},
        )

    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import (
        BusinessProfile, BusinessHours, ServiceOffering,
    )
    business = BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="",
            ),
        ],
    )
    llm = _ScriptedLLM(script=[
        {"tool": "lookup_faq", "args": {"topic": "hours"}},
        "We're open 9-5.",
    ])
    brain = ReceptionistBrain(
        llm=llm, business=business,
        tools=build_clinic_tools(),
        tool_handler=_tracking_handler,
        extractor_llm=llm,
    )
    state = CallState(session_id="CAdisc_norm", business_id="biz1")
    await brain.handle_user_turn(state, "book me a follow-up")
    # lookup_faq WAS dispatched to normal tool_handler.
    assert "lookup_faq" in handler_calls
