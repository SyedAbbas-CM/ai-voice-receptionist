"""Tests for the barge-in classifier — the mhm-vs-stop decision that
makes voice agents feel human. Any test here reflects a real UX bug we
want to prevent (agent stopping on 'mhm', agent talking over 'wait!')."""
from __future__ import annotations

import pytest

from packages.voice import BargeAction, classify_barge, should_interrupt


# ---- backchannels (KEEP TALKING) ----

@pytest.mark.parametrize("text", [
    "mhm", "mm-hm", "uh-huh", "yeah", "yea", "yep", "yup", "yes", "sure",
    "okay", "ok", "gotcha", "got it", "right", "true", "totally",
    "yeah.", "OK", "Right.", "Sure.",
    "mm", "hmm", "ah", "oh", "aha",
    "yeah yeah", "mhm mhm",
])
def test_backchannel_returns_continue(text):
    assert classify_barge(text) is BargeAction.CONTINUE, f"{text!r} should be CONTINUE"
    assert should_interrupt(text) is False


# ---- real interrupts (STOP AND LISTEN) ----

@pytest.mark.parametrize("text", [
    "Stop.",
    "Wait, hold on.",
    "Hang on a second.",
    "Actually, can we do 3pm instead?",
    "Let me change that.",
    "Never mind, cancel it.",
    "Nevermind.",
    "No wait, different time.",
    "You're wrong.",
    "That's not right.",
    "That's incorrect.",
    "Speak louder please.",
    "Can you repeat that?",
    "Say again?",
    "No.",
    "Nope.",
    "Nah.",
    # Long-ish utterance — anything > 4 words = interrupt
    "I actually want a different time.",
    "That's not what I asked for.",
])
def test_real_interrupts_return_interrupt(text):
    assert classify_barge(text) is BargeAction.INTERRUPT, f"{text!r} should be INTERRUPT"
    assert should_interrupt(text) is True


# ---- edge cases ----

@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_returns_ignore(text):
    assert classify_barge(text or "") is BargeAction.IGNORE
    assert should_interrupt(text or "") is False


def test_short_specific_utterance_treated_as_interrupt():
    """3-4 word specific utterances like 'different time please' are
    real interrupts, not backchannel."""
    assert classify_barge("different time please") is BargeAction.INTERRUPT


def test_ignores_solo_punctuation_and_capitalization():
    """Whisper often returns 'Yeah.' or 'yeah' — both should behave the same."""
    assert classify_barge("Yeah.") is BargeAction.CONTINUE
    assert classify_barge("yeah") is BargeAction.CONTINUE
    assert classify_barge("YEAH!") is BargeAction.CONTINUE
