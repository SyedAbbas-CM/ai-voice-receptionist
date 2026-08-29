"""Tests for BUG-CHR-01: LLM empty-completion watchdog.

2026-08-29 (task new): Christiaan (CA2fa1fef2, Netherlands +31 caller)
hit 5 empty LLM completions in one call.  Caller sat through 8+
seconds of dead air per stall, then hung up.  Content-triggered (bare
noun 'A follow-up', Dutch phone digit string).

This test suite pins the fix: brain.py detects response with neither
text nor tool_calls, retries ONCE with a rescue system-note, and if
retry ALSO empty, speaks a deterministic 'Sorry, could you say that
again?' fallback.  Never dead air.

Tests use a ScriptedLLM that queues responses in order so we can
control exactly what the first attempt returns vs the rescue attempt.
"""
from __future__ import annotations

from typing import Optional

import pytest

from packages.core_agent import ReceptionistBrain
from packages.integrations.fake_calendar import FakeCalendar
from packages.integrations.clinic_tools import (
    ClinicToolHandler,
    build_clinic_tools,
)
from packages.schemas import (
    BusinessProfile,
    BusinessHours,
    CallState,
    ToolCall,
    ToolResult,
)


class _ScriptedLLM:
    """LLM stub that returns pre-queued responses one at a time.

    Each `complete()` pops one response.  Response is a dict with
    'text' (str) and optional 'tool_calls' (list[dict]).  Set
    `record_sites=True` and call_sites gets populated with each
    site= kwarg the brain passes so tests can assert the rescue path
    fired.
    """

    name = "scripted"
    model = "scripted-model"

    def __init__(self, responses: list[dict]) -> None:
        self._queue = list(responses)
        self.call_sites: list[str] = []
        self.call_count = 0

    async def complete(
        self,
        messages,
        *,
        tools=None,
        temperature: float = 0.3,
        max_tokens: int = 200,
        site: str = "",
    ):
        self.call_count += 1
        self.call_sites.append(site)
        if not self._queue:
            raise AssertionError(
                f"ScriptedLLM exhausted at call #{self.call_count} "
                f"(site={site!r})"
            )
        spec = self._queue.pop(0)
        from apps.api.app.providers.base import LLMResponse
        tcs = []
        for tc_spec in spec.get("tool_calls", []) or []:
            tcs.append(ToolCall(
                id=tc_spec.get("id", "call_1"),
                name=tc_spec["name"],
                arguments=tc_spec.get("arguments", {}),
            ))
        return LLMResponse(
            text=spec.get("text", "") or "",
            tool_calls=tcs,
            finish_reason=(
                "tool_calls" if tcs else "stop"
            ),
            raw={},
        )


def _business():
    return BusinessProfile(
        id="biz1", name="Smile Dental", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
    )


async def _null_tool_handler(call: ToolCall) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id, name=call.name, result={"stub": True},
    )


def _brain(llm, tool_handler=None):
    tools = build_clinic_tools()
    business = _business()
    return ReceptionistBrain(
        llm=llm,
        business=business,
        tools=tools,
        tool_handler=tool_handler or _null_tool_handler,
        extractor_llm=llm,
    )


# ── the good path — one non-empty completion, no rescue ─────────


@pytest.mark.asyncio
async def test_normal_response_does_not_trigger_rescue():
    """Baseline: when the LLM returns real text on the first try,
    rescue MUST NOT fire.  Verifies we don't add latency to the
    happy path."""
    llm = _ScriptedLLM([
        {"text": "Yeah — Tuesday at 2:30 works. What service?"},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-normal", business_id="biz1")
    result = await brain.handle_user_turn(state, "Can I book Tuesday?")
    assert "Tuesday" in result.reply
    # Rescue site should NOT have fired.
    assert "brain.rescue_empty" not in llm.call_sites


@pytest.mark.asyncio
async def test_tool_call_response_does_not_trigger_rescue():
    """LLM returns a tool_call (no text) — that's normal, not empty."""
    llm = _ScriptedLLM([
        # First: emit a tool call (no text is fine when tool_calls present)
        {
            "text": "",
            "tool_calls": [{
                "id": "c1", "name": "check_availability",
                "arguments": {
                    "service": "cleaning",
                    "date": "2026-09-01",
                },
            }],
        },
        # Second: text reply after tool result comes back
        {"text": "Got it — 10am works. Should I book?"},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-tool", business_id="biz1")
    result = await brain.handle_user_turn(
        state, "Do you have Tuesday morning?",
    )
    assert result.reply  # not empty
    # Rescue MUST NOT fire — tool_calls present is legitimate.
    assert "brain.rescue_empty" not in llm.call_sites


# ── the rescue path — first response empty, retry succeeds ─────


@pytest.mark.asyncio
async def test_empty_first_completion_triggers_rescue():
    """Christiaan-class bug: first call returns chars=0 tools=0.
    Rescue should fire and produce a real reply."""
    llm = _ScriptedLLM([
        # First attempt: EMPTY (the Christiaan failure mode)
        {"text": "", "tool_calls": []},
        # Rescue attempt: real recovery reply
        {"text": "Sorry, could you tell me what day works for you?"},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-rescue", business_id="biz1")
    result = await brain.handle_user_turn(state, "A follow-up.")
    # Rescue site MUST have fired.
    assert "brain.rescue_empty" in llm.call_sites
    # Recovered text is used as the reply.
    assert "Sorry" in result.reply or "day" in result.reply


@pytest.mark.asyncio
async def test_empty_first_whitespace_only_also_triggers_rescue():
    """Text that's only whitespace counts as empty."""
    llm = _ScriptedLLM([
        {"text": "  \n\t  "},
        {"text": "Recovery reply."},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-whitespace", business_id="biz1")
    result = await brain.handle_user_turn(state, "hi")
    assert "brain.rescue_empty" in llm.call_sites
    assert "Recovery" in result.reply


# ── the deterministic fallback — both attempts empty ────────


@pytest.mark.asyncio
async def test_both_attempts_empty_yields_deterministic_fallback():
    """Worst case: both completions empty.  Caller MUST NOT hear dead air.
    A deterministic 'Sorry, I missed that' fallback speaks."""
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},
        {"text": "", "tool_calls": []},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-both-empty", business_id="biz1")
    result = await brain.handle_user_turn(state, "A follow-up.")
    # Rescue MUST have been attempted.
    assert "brain.rescue_empty" in llm.call_sites
    # Deterministic fallback text used.
    assert "missed" in result.reply.lower() or "say" in result.reply.lower()
    # Must be non-empty — the ENTIRE POINT.
    assert result.reply.strip() != ""


@pytest.mark.asyncio
async def test_rescue_raises_still_yields_fallback():
    """Rescue call itself raises → we still speak the deterministic fallback."""

    class _RaisingScript(_ScriptedLLM):
        async def complete(self, messages, **kwargs):
            self.call_count += 1
            self.call_sites.append(kwargs.get("site", ""))
            # First call: empty.  Second call (rescue): raise.
            if self.call_count == 1:
                from apps.api.app.providers.base import LLMResponse
                return LLMResponse(
                    text="", tool_calls=[], finish_reason="stop", raw={},
                )
            raise RuntimeError("provider down")

    llm = _RaisingScript([{}])
    brain = _brain(llm)
    state = CallState(session_id="t-rescue-raise", business_id="biz1")
    result = await brain.handle_user_turn(state, "test")
    # Rescue was attempted.
    assert "brain.rescue_empty" in llm.call_sites
    # Deterministic fallback text used — no crash, no dead air.
    assert result.reply.strip() != ""
    assert "missed" in result.reply.lower() or "say" in result.reply.lower()


@pytest.mark.asyncio
async def test_rescue_succeeds_with_tool_call_recovers():
    """Rescue can recover via a tool_call too (not just text) — e.g.
    LLM figures out on the retry that it should call check_availability."""
    llm = _ScriptedLLM([
        # First attempt: empty
        {"text": "", "tool_calls": []},
        # Rescue: tool call (recovered by calling a tool)
        {
            "text": "",
            "tool_calls": [{
                "id": "c1", "name": "lookup_faq",
                "arguments": {"topic": "hours"},
            }],
        },
        # After tool receipt: real text reply
        {"text": "We're open 9 to 5 weekdays."},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-rescue-tool", business_id="biz1")
    result = await brain.handle_user_turn(state, "when are you open")
    assert "brain.rescue_empty" in llm.call_sites
    assert "9 to 5" in result.reply or "weekdays" in result.reply.lower()


# ── rescue prompt includes user text ────────────────────────


@pytest.mark.asyncio
async def test_rescue_system_note_references_last_user_text():
    """The rescue system note must include the caller's last utterance
    so the LLM has enough context to unstick."""
    captured_messages = []

    class _CapturingScript(_ScriptedLLM):
        async def complete(self, messages, **kwargs):
            self.call_count += 1
            self.call_sites.append(kwargs.get("site", ""))
            if kwargs.get("site") == "brain.rescue_empty":
                captured_messages.extend(messages)
            from apps.api.app.providers.base import LLMResponse
            if self.call_count == 1:
                return LLMResponse(
                    text="", tool_calls=[], finish_reason="stop", raw={},
                )
            return LLMResponse(
                text="Recovery.", tool_calls=[],
                finish_reason="stop", raw={},
            )

    llm = _CapturingScript([{}])
    brain = _brain(llm)
    state = CallState(session_id="t-rescue-context", business_id="biz1")
    caller_text = "My unique caller phrase 7734"
    await brain.handle_user_turn(state, caller_text)
    # Some rescue message contained the caller text.
    concatenated = " ".join(
        m.get("content", "") for m in captured_messages
    )
    assert caller_text in concatenated


# ── logged EMPTY_LLM_COMPLETION line for observability ──────


@pytest.mark.asyncio
async def test_empty_completion_logs_warning(caplog):
    """Ops needs to see EMPTY_LLM_COMPLETION in the log so they can
    grep for how often this fires per tenant."""
    import logging as _l
    caplog.set_level(_l.WARNING)
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},
        {"text": "recovered"},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-logging", business_id="biz1")
    await brain.handle_user_turn(state, "hello")
    # WARN line for the empty detection.
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "EMPTY_LLM_COMPLETION" in all_msgs


@pytest.mark.asyncio
async def test_deterministic_fallback_logs_specifically(caplog):
    """When we fall to the deterministic path, ops needs to see it
    separately (this is the 'bad' path — signals real trouble)."""
    import logging as _l
    caplog.set_level(_l.WARNING)
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},
        {"text": "", "tool_calls": []},
    ])
    brain = _brain(llm)
    state = CallState(session_id="t-det-fallback", business_id="biz1")
    await brain.handle_user_turn(state, "hello")
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "EMPTY_LLM_DETERMINISTIC_FALLBACK" in all_msgs
