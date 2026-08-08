"""Sprint 9e: PerformancePlanner tests.

The planner's contract: ALWAYS returns a PerformancePlan, never raises.
We test each fallback path + the happy path.

Test fakes:
  * FakeLLM — configurable text response, optional delay + raise
  * All tests use a real Groq-shaped LLMResponse (packages.schemas import)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from packages.core_agent.planners import PerformancePlan, PerformancePlanner
from packages.voice.vpl import DeliveryStyle, SpeechAct


class FakeLLMResponse:
    """Duck-typed LLMResponse — .text is what the planner reads."""
    def __init__(self, text: str) -> None:
        self.text = text
        self.tool_calls = []
        self.finish_reason = "stop"
        self.raw = None


class FakeLLM:
    """Configurable LLM that returns / delays / raises deterministically."""

    def __init__(
        self,
        text: str = "{}",
        delay_ms: int = 0,
        raise_error: Exception | None = None,
    ) -> None:
        self._text = text
        self._delay = delay_ms / 1000.0
        self._raise = raise_error
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise self._raise
        return FakeLLMResponse(self._text)


# ── happy path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_json_returns_populated_delivery():
    payload = json.dumps({
        "style": "warm",
        "intensity": 0.4,
        "rate": 0.95,
        "pause_before_ms": 0,
        "pause_after_ms": 200,
    })
    llm = FakeLLM(text=payload)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("Hi there!", SpeechAct.GREETING, "Corvina")
    assert not plan.used_fallback
    assert plan.error is None
    assert plan.delivery.style == DeliveryStyle.WARM
    assert plan.delivery.intensity == 0.4
    assert plan.delivery.pause_after_ms == 200
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_partial_json_fills_missing_from_defaults():
    """Missing fields should inherit from default_delivery_for(speech_act)
    — the planner accepts partial payloads."""
    payload = json.dumps({"style": "reassuring"})
    llm = FakeLLM(text=payload)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("We don't have that time.",
                              SpeechAct.DELIVER_BAD_NEWS, "Corvina")
    assert not plan.used_fallback
    assert plan.delivery.style == DeliveryStyle.REASSURING
    # rate should come from the bad-news default (0.9), not the schema
    # default (1.0)
    assert plan.delivery.rate == 0.9


# ── fallback paths ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_returns_fallback():
    llm = FakeLLM(text="{}", delay_ms=500)  # LLM slower than budget
    planner = PerformancePlanner(llm=llm, timeout_ms=50)
    plan = await planner.plan("hi", SpeechAct.GREETING, "test")
    assert plan.used_fallback is True
    assert "timeout" in (plan.error or "").lower() or plan.error is not None


@pytest.mark.asyncio
async def test_llm_error_returns_fallback():
    llm = FakeLLM(raise_error=RuntimeError("groq down"))
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.GREETING, "test")
    assert plan.used_fallback is True
    assert "groq down" in (plan.error or "")


@pytest.mark.asyncio
async def test_empty_response_returns_fallback():
    llm = FakeLLM(text="")
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.NEUTRAL, "test")
    assert plan.used_fallback is True


@pytest.mark.asyncio
async def test_malformed_json_returns_fallback():
    llm = FakeLLM(text="this is not JSON at all just some prose")
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.NEUTRAL, "test")
    assert plan.used_fallback is True


@pytest.mark.asyncio
async def test_json_with_surrounding_prose_still_parses():
    """LLMs sometimes wrap JSON in ```json``` or prefix with 'Here you go:'
    — regex extraction should catch the object."""
    text = 'Here is your delivery: {"style": "warm", "intensity": 0.4}\nHope that works!'
    llm = FakeLLM(text=text)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.GREETING, "test")
    assert not plan.used_fallback
    assert plan.delivery.style == DeliveryStyle.WARM


# ── safety envelope enforced via validator repair ───────────────────

@pytest.mark.asyncio
async def test_high_intensity_repaired_not_rejected():
    """LLM returns intensity=0.9 for an emergency (which caps at 0.4).
    Validator repair clamps rather than falling back."""
    payload = json.dumps({"style": "urgent", "intensity": 0.9})
    llm = FakeLLM(text=payload)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan(
        "Please stay on the line — connecting emergency services.",
        SpeechAct.EMERGENCY, "clinic",
    )
    assert not plan.used_fallback   # repair succeeded
    assert plan.delivery.intensity <= 0.4  # emergency policy cap


# ── invalid style enum falls back to default style, not fallback ────

@pytest.mark.asyncio
async def test_unknown_style_uses_speech_act_default():
    payload = json.dumps({"style": "singsong_dramatic"})
    llm = FakeLLM(text=payload)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.GREETING, "test")
    # Whole call succeeds (fallback=False), but unknown style silently
    # inherits from the greeting default (WARM).
    assert not plan.used_fallback
    assert plan.delivery.style == DeliveryStyle.WARM


# ── latency measurement ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_latency_ms_populated_on_success():
    llm = FakeLLM(text='{"style": "warm"}', delay_ms=20)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.GREETING, "test")
    assert plan.latency_ms >= 15
    assert plan.latency_ms < 500


@pytest.mark.asyncio
async def test_latency_ms_populated_on_fallback():
    llm = FakeLLM(text="", delay_ms=10)
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.NEUTRAL, "test")
    assert plan.used_fallback
    assert plan.latency_ms >= 5


# ── delivery returned is always fully populated ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("speech_act", list(SpeechAct))
async def test_fallback_delivery_valid_for_every_speech_act(speech_act):
    """Every speech act's default_delivery must survive validation +
    end up in a returned PerformancePlan."""
    llm = FakeLLM(raise_error=RuntimeError("provider down"))
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("test text", speech_act, "biz")
    assert plan.used_fallback is True
    assert plan.delivery is not None
    # Delivery is a Pydantic model — pass through construction to
    # verify no invariants broke.
    assert plan.delivery.intensity >= 0
