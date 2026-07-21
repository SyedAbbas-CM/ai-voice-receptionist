"""Tests for emergency classifier — safety-critical.

Rule: false positives (over-escalating) are cheap; false negatives (missing a
real emergency) are legally catastrophic. Tests here overweight recall on
emergencies and tolerate false positives on ambiguous input.
"""
from __future__ import annotations

import pytest

from packages.core_agent.emergency_classifier import (
    EmergencyVerdict,
    classify_emergency,
    classify_emergency_full,
)


# ---- CLEAR EMERGENCIES — MUST be flagged ----

@pytest.mark.parametrize("text,expected_category", [
    # Cardiac
    ("I have crushing chest pain and my arm is numb.", "cardiac"),
    ("My chest hurts really bad.", "cardiac"),
    ("I think I'm having a heart attack.", "cardiac"),
    ("My arm is numb and I can't feel it.", "cardiac"),
    # Respiratory
    ("My son isn't breathing right.", "respiratory"),
    ("I can't breathe.", "respiratory"),
    ("She's not breathing!", "respiratory"),
    ("He's choking on something.", "respiratory"),
    # Bleeding
    ("I'm bleeding heavily and it won't stop.", "bleeding_trauma"),
    ("There's blood everywhere, it won't stop bleeding.", "bleeding_trauma"),
    # Neurological
    ("I think my father is having a stroke.", "neurological"),
    ("My daughter is having a seizure right now.", "neurological"),
    ("He passed out and I can't wake him up.", "neurological"),
    ("She's unconscious.", "neurological"),
    # Overdose
    ("My roommate took too many pills, overdose.", "overdose_poisoning"),
    ("I think she was poisoned.", "overdose_poisoning"),
    # Self-harm (highest sensitivity)
    ("I want to kill myself.", "self_harm"),
    ("I'm going to end my life tonight.", "self_harm"),
    ("I'm having suicidal thoughts.", "self_harm"),
    ("I've been hurting myself.", "self_harm"),
    # Anaphylaxis
    ("She's having a severe allergic reaction to peanuts.", "anaphylaxis"),
    ("I'm going into anaphylaxis.", "anaphylaxis"),
    # Self-declared
    ("This is an emergency, please help!", "self_declared"),
    ("Please call 911 for me, I need help.", "self_declared"),
])
def test_flags_clear_emergencies(text: str, expected_category: str):
    v = classify_emergency(text)
    assert v.is_emergency, f"MISSED emergency: {text!r}"
    assert v.category == expected_category, (
        f"wrong category for {text!r}: got {v.category!r}, expected {expected_category!r}"
    )
    assert v.matched_text, "matched_text should not be empty on positive verdict"
    assert v.reason, "reason should not be empty on positive verdict"


# ---- NORMAL SPEECH — MUST NOT trigger (false-positive check) ----

@pytest.mark.parametrize("text", [
    "Hi, I'd like to book an appointment for my back pain tomorrow.",
    "What time do you close on Thursday?",
    "I have chronic knee pain from running.",
    "My dad has heart disease, need to book his follow-up.",   # 'heart' but not 'heart attack'
    "Can I get a prescription refill?",
    "I've been feeling stressed at work.",
    "My asthma is acting up but nothing urgent.",              # tricky — could be respiratory
    "I have a small cut on my finger.",                        # 'bleeding' avoided
    "Nose bleeds happen sometimes, no big deal.",              # 'bleed' but qualified
    "",
    "   ",
    "Hello?",
    "Just calling about a bill.",
])
def test_normal_speech_not_flagged(text: str):
    v = classify_emergency(text)
    assert not v.is_emergency, f"FALSE POSITIVE on: {text!r} (matched {v.matched_text!r})"


# ---- Escalation message content ----

def test_general_emergency_message_says_911():
    v = EmergencyVerdict(is_emergency=True, category="cardiac", matched_text="chest pain")
    msg = v.escalation_message
    assert "nine one one" in msg.lower(), "must say 911 in spoken form"
    assert "hang up" in msg.lower() or "call" in msg.lower()


def test_self_harm_message_includes_988_hotline():
    """Self-harm gets a specialized message with the mental health hotline."""
    v = EmergencyVerdict(is_emergency=True, category="self_harm", matched_text="kill myself")
    msg = v.escalation_message
    assert "nine eight eight" in msg.lower(), "must include 988 for self-harm"
    assert "help" in msg.lower() or "listen" in msg.lower()


def test_non_emergency_has_empty_escalation_message():
    v = EmergencyVerdict(is_emergency=False)
    assert v.escalation_message == ""


# ---- Empty verdict ----

def test_empty_input_returns_not_emergency():
    v = classify_emergency("")
    assert not v.is_emergency
    assert v.category == ""


def test_whitespace_only_returns_not_emergency():
    v = classify_emergency("     ")
    assert not v.is_emergency


# ---- Full entry point without LLM ----

@pytest.mark.asyncio
async def test_full_entry_point_regex_only():
    """When use_llm_fallback=False, should behave identically to classify_emergency."""
    v = await classify_emergency_full(None, "chest pain", use_llm_fallback=False)
    assert v.is_emergency
    assert v.category == "cardiac"


@pytest.mark.asyncio
async def test_full_entry_point_no_llm_no_fallback_when_disabled():
    """LLM must NOT be called when use_llm_fallback=False, even if provided."""
    class ExplodingLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("should not be called")

    v = await classify_emergency_full(ExplodingLLM(), "just a normal appointment please",
                                       use_llm_fallback=False)
    assert not v.is_emergency


@pytest.mark.asyncio
async def test_llm_fallback_flags_ambiguous_emergency():
    """When regex misses and LLM fallback is enabled, LLM verdict wins."""
    class YesEmergencyLLM:
        name = "test"
        model = "test"
        async def complete(self, messages, tools=None, temperature=0.0, max_tokens=8):
            from apps.api.app.providers.base import LLMResponse
            return LLMResponse(text="EMERGENCY")

    v = await classify_emergency_full(YesEmergencyLLM(),
                                       "something feels really wrong with my body",
                                       use_llm_fallback=True)
    assert v.is_emergency
    assert v.category == "llm_flagged"


@pytest.mark.asyncio
async def test_llm_fallback_fails_safe_on_llm_error():
    """LLM raises → we treat as NOT emergency (fail open, not closed) so a
    hosted-LLM outage doesn't reject every caller."""
    class BrokenLLM:
        name = "test"; model = "test"
        async def complete(self, *args, **kwargs):
            raise RuntimeError("upstream timeout")

    v = await classify_emergency_full(BrokenLLM(),
                                       "vague ambiguous complaint no emergency keywords",
                                       use_llm_fallback=True)
    assert not v.is_emergency, "LLM error must NOT auto-escalate (would DDoS 911)"


# ---- Regression: exact reproduction of documented catastrophic-failure #2 ----
# (from docs/rnd-2026-07/05-nightmare-callers.md — #2 catastrophic failure)

def test_prevents_documented_catastrophic_failure_2():
    """Category 10 from research: 'Miss a medical emergency and book instead
    of escalating 911' — top-cited liability failure. Guard rail lives here."""
    heart_attack_scripts = [
        "I have crushing chest pain and my arm is numb.",
        "My son isn't breathing right.",
        "I'm bleeding and can't stop it.",
    ]
    for s in heart_attack_scripts:
        v = classify_emergency(s)
        assert v.is_emergency, f"CATASTROPHIC FAILURE PATH — regex missed: {s!r}"
        assert v.escalation_message, "emergency verdicts must produce escalation message"
