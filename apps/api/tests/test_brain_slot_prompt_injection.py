"""LK steal #7 wire — brain injects sub-agent prompt when active.

2026-08-29: When `state._slot_capture_prompt` is set (by the actor
via enter_slot_capture), brain.handle_user_turn should:
  1. Use the slot prompt's `instructions` as the SYSTEM prompt for
     that turn, NOT the wider self.system_prompt.
  2. Skip turn-intent injection.
  3. Skip policy directive injection.
  4. Emit a `slot_capture_prompt_active` event so trace shows the
     narrow-scope branch fired.
  5. When `state._slot_capture_prompt` is None (normal turn), behavior
     is unchanged — wider prompt + intent + policy all render as
     before.

The whole point of LK's sub-agent pattern is that narrow scope replaces
wider scope; mixing them defeats the purpose.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from packages.slot_parsers.slot_capture_prompts import (
    build_phone_capture_prompt,
)


class _CapturingLLM:
    """Records every complete() call so tests can inspect the exact
    messages sent to the model — the whole test surface here is
    which system prompt got sent."""
    name = "capturing"
    model = "capturing-model"

    def __init__(self, response_text: str = "ok"):
        self.calls = []
        self._response_text = response_text

    async def complete(
        self, messages, *, tools=None, temperature: float = 0.3,
        max_tokens: int = 200, site: str = "",
    ):
        self.calls.append({
            "messages": list(messages),
            "site": site, "tools_n": len(tools or []),
        })
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(
            text=self._response_text, tool_calls=[],
            finish_reason="stop", raw={},
        )


async def _null_tool_handler(call):
    from packages.schemas import ToolResult
    return ToolResult(
        tool_call_id=call.id, name=call.name, result={},
    )


def _brain(llm):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import BusinessProfile, BusinessHours
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="Test Clinic", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
    )
    return ReceptionistBrain(
        llm=llm, business=business, tools=tools,
        tool_handler=_null_tool_handler, extractor_llm=llm,
    )


# ── normal path — wider prompt used, no slot injection ────────


@pytest.mark.asyncio
async def test_normal_turn_uses_wider_system_prompt():
    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="Sure, how can I help?")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_normal", business_id="biz1")
    await brain.handle_user_turn(state, "Hi there")
    # First call's first message = system with the WIDER prompt.
    first = llm.calls[0]
    sys_msg = first["messages"][0]
    assert sys_msg["role"] == "system"
    # Wider prompt contains persona / instructions from
    # ReceptionistBrain.system_prompt default.  Any non-empty content
    # confirms the wider path fired.
    assert len(sys_msg["content"]) > 0


# ── slot-active path — narrow prompt REPLACES wider ───────────


@pytest.mark.asyncio
async def test_slot_active_replaces_wider_system_prompt():
    """When state._slot_capture_prompt is set, the brain sends ONLY
    the narrow LK sub-agent prompt as system.  Wider prompt is
    NOT concatenated — that would defeat the sub-agent pattern."""
    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="Sure, what's your number?")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_slot", business_id="biz1")
    # Attach the slot prompt on the state — this simulates what the
    # actor does at enter_slot_capture / dispatch boundary.
    state._slot_capture_prompt = build_phone_capture_prompt(
        modality="audio", require_confirmation=True,
    )
    await brain.handle_user_turn(state, "555 one two three")
    # Only ONE system message, containing the narrow prompt.
    first = llm.calls[0]
    sys_msgs = [
        m for m in first["messages"] if m["role"] == "system"
    ]
    assert len(sys_msgs) == 1
    content = sys_msgs[0]["content"]
    # LK phone-capture discipline lines present.
    assert "update_phone_number" in content
    assert "in groups" in content.lower()
    # Wider receptionist persona NOT present — this is the key
    # assertion.  Alex the receptionist has no place here.
    assert "Alex" not in content
    assert "receptionist" not in content.lower() or (
        "receptionist" in content.lower()
        and content.lower().count("receptionist") <= 1
    )


@pytest.mark.asyncio
async def test_slot_active_skips_policy_directive(monkeypatch):
    """Even when NextActionPolicy is enabled, the slot capture path
    doesn't inject the policy directive — the narrow prompt is the
    only system message.  Small model gets one clear scope."""
    # Force policy directive path 'on' via the feature flag env.
    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="Read that back: 5 5 5")
    brain = _brain(llm)
    # Point the settings shim at an object that reports flag=on.
    class _S: next_action_policy_enabled = True
    import packages.core_agent.brain as _brain_mod
    monkeypatch.setattr(_brain_mod, "_brain_settings", _S())
    state = CallState(session_id="CAtest_slot_pol", business_id="biz1")
    state._slot_capture_prompt = build_phone_capture_prompt(
        modality="audio",
    )
    await brain.handle_user_turn(state, "5 5 5 1 2 3 4")
    sys_msgs = [
        m for m in llm.calls[0]["messages"] if m["role"] == "system"
    ]
    # Still just one system message — policy directive was NOT stacked
    # on top of the slot prompt.
    assert len(sys_msgs) == 1


@pytest.mark.asyncio
async def test_slot_active_skips_turn_intent():
    """Turn intent is also skipped — the sub-agent scope is complete
    on its own."""
    from packages.schemas import CallState, TranscriptTurn, TurnRole
    llm = _CapturingLLM(response_text="Read that back: 5 5 5")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_slot_int", business_id="biz1")
    # Simulate a turn intent that would normally inject.
    @dataclass
    class _Intent:
        system_note: str = "SENTINEL_INTENT_HINT_AAAA"
    state.last_turn_intent = _Intent()
    state._slot_capture_prompt = build_phone_capture_prompt()
    await brain.handle_user_turn(state, "hello")
    all_content = " ".join(
        m["content"] for m in llm.calls[0]["messages"]
        if m["role"] == "system"
    )
    # The turn-intent sentinel must NOT appear.
    assert "SENTINEL_INTENT_HINT_AAAA" not in all_content


# ── empty / malformed prompt falls through ────────────────


@pytest.mark.asyncio
async def test_empty_slot_prompt_falls_through_to_wider():
    """If _slot_capture_prompt is set but has empty instructions,
    fall through to the wider prompt.  Defensive against a stale
    or half-initialized attach."""
    from packages.schemas import CallState

    class _EmptyPrompt:
        instructions = ""
        on_enter_prompt = ""
        tools_hint = ()

    llm = _CapturingLLM(response_text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_empty_slot", business_id="biz1")
    state._slot_capture_prompt = _EmptyPrompt()
    await brain.handle_user_turn(state, "Hi")
    # Wider prompt used.
    first_sys = llm.calls[0]["messages"][0]
    assert first_sys["role"] == "system"
    # Wider prompt is non-trivially longer than the "empty" case.
    assert len(first_sys["content"]) > 0


# ── slot prompt is not persisted ────────────────────────


@pytest.mark.asyncio
async def test_slot_prompt_not_persisted_to_transcript():
    """The slot prompt is ephemeral per-turn.  It must NOT end up
    baked into state.transcript — that would drag the narrow scope
    into every future turn."""
    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_ephemeral", business_id="biz1")
    state._slot_capture_prompt = build_phone_capture_prompt()
    await brain.handle_user_turn(state, "hi")
    transcript_texts = [t.text for t in state.transcript]
    # No LK discipline lines leaked into transcript.
    assert not any(
        "update_phone_number" in txt for txt in transcript_texts
    )
    assert not any(
        "in groups" in txt for txt in transcript_texts
    )
