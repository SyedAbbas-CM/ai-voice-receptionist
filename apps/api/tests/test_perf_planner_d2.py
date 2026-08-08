"""Sprint 10 D2: enriched perf planner input tests.

Coverage:
  * extract_critical_spans finds dates, times, phones, prices, weekdays
  * caller_state hint injected into prompt when supplied
  * critical_spans hint injected when text has recognizable patterns
  * plan() back-compat: old (text, speech_act, business_name) still works
  * auto-extract critical spans when caller doesn't pass any
"""
from __future__ import annotations

import pytest

from packages.core_agent.planners.performance import (
    PERFORMANCE_PROMPT,
    PerformancePlanner,
    extract_critical_spans,
)
from packages.voice.vpl import SpeechAct


class _FakeResp:
    def __init__(self, text: str):
        self.text = text
        self.tool_calls = []
        self.finish_reason = "stop"
        self.raw = None


class _RecordingLLM:
    """Captures the exact prompt the perf planner emitted."""
    def __init__(self, response: str = '{"style":"warm","intensity":0.4}'):
        self._response = response
        self.last_messages = None

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.last_messages = messages
        return _FakeResp(self._response)


# ── extract_critical_spans ─────────────────────────────────────────

def test_extract_date_and_time():
    spans = extract_critical_spans(
        "I have you booked for Thursday, August 6th at 10:30 AM."
    )
    kinds = {s["kind"] for s in spans}
    assert "time" in kinds
    assert "weekday" in kinds


def test_extract_phone_us():
    spans = extract_critical_spans("Your callback number is 555-010-1234.")
    assert any(s["kind"] == "phone" for s in spans)


def test_extract_money():
    spans = extract_critical_spans("The deposit is $25.")
    assert any(s["kind"] == "money" and "$25" in s["text"] for s in spans)


def test_extract_iso_datetime():
    spans = extract_critical_spans("Booked for 2026-08-06T10:00.")
    assert any(s["kind"] == "datetime" for s in spans)


def test_no_overlap_when_patterns_collide():
    """Weekday+date shouldn't produce two overlapping spans."""
    spans = extract_critical_spans("See you Thursday, August 6th.")
    ranges = [(s["start"], s["end"]) for s in spans]
    for i, (s1, e1) in enumerate(ranges):
        for s2, e2 in ranges[i+1:]:
            assert not (s1 < e2 and e1 > s2), f"overlap: {(s1,e1)} vs {(s2,e2)}"


def test_no_spans_in_plain_text():
    spans = extract_critical_spans("Sure, one moment please.")
    assert spans == []


# ── perf planner prompt enrichment ─────────────────────────────────

@pytest.mark.asyncio
async def test_caller_state_hint_in_prompt():
    llm = _RecordingLLM()
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    await planner.plan(
        "Sorry, we don't have anything Tuesday.",
        SpeechAct.DELIVER_BAD_NEWS, "Test Clinic",
        caller_state={"frustration": 0.6, "urgency": 0.5, "speaking_rate": "fast"},
    )
    prompt = llm.last_messages[0]["content"]
    assert "caller_state" in prompt
    assert "frustration_high" in prompt
    assert "urgency_high" in prompt


@pytest.mark.asyncio
async def test_no_caller_state_block_when_signals_low():
    """Frustration+urgency below threshold shouldn't clutter the prompt."""
    llm = _RecordingLLM()
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    await planner.plan(
        "How can I help you?", SpeechAct.GREETING, "Test",
        caller_state={"frustration": 0.1, "urgency": 0.15},
    )
    prompt = llm.last_messages[0]["content"]
    assert "caller_state" not in prompt


@pytest.mark.asyncio
async def test_critical_spans_auto_extracted():
    """If the caller doesn't pass critical_spans, planner extracts them."""
    llm = _RecordingLLM()
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    await planner.plan(
        "Booked for Thursday, August 6th at 10:30 AM.",
        SpeechAct.CONFIRM, "Test",
    )
    prompt = llm.last_messages[0]["content"]
    assert "critical_spans" in prompt
    assert "weekday" in prompt or "time" in prompt


@pytest.mark.asyncio
async def test_critical_spans_passed_explicit_wins_over_auto():
    """If caller supplies critical_spans, planner uses them verbatim."""
    llm = _RecordingLLM()
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    await planner.plan(
        "Booked for Thursday.", SpeechAct.CONFIRM, "Test",
        critical_spans=[
            {"kind": "custom", "text": "MY_MARKER", "start": 0, "end": 9},
        ],
    )
    prompt = llm.last_messages[0]["content"]
    assert "MY_MARKER" in prompt


@pytest.mark.asyncio
async def test_backcompat_old_signature_still_works():
    """The pre-D2 call site (text, speech_act, business_name) must
    still work without any keyword args."""
    llm = _RecordingLLM()
    planner = PerformancePlanner(llm=llm, timeout_ms=500)
    plan = await planner.plan("hi", SpeechAct.GREETING, "Test")
    assert not plan.used_fallback
