"""BargeInPolicy tests (LiveKit steal #5, 2026-08-29).

Two knobs suppress micro-interruptions:
  - min_interruption_words: floor on non-backchannel token count
  - min_interruption_duration_ms: floor on speech duration

Explicit interrupt cues ("stop", "wait", "hold on", "no") BYPASS the
floors — latency matters more than false positives on those.
"""
from __future__ import annotations

import pytest

from packages.voice.barge_in import (
    BargeAction,
    BargeInPolicy,
    DEFAULT_BARGE_POLICY,
)


# ── explicit cues bypass everything ─────────────────────────────


def test_explicit_stop_below_min_words_still_interrupts():
    """Solo 'stop' is 1 word — under default min=2 — but the
    explicit-cue bypass fires anyway."""
    p = BargeInPolicy(min_interruption_words=3)
    action, reason = p.evaluate("stop", duration_ms=200)
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"


def test_explicit_wait_below_min_duration_still_interrupts():
    p = BargeInPolicy(
        min_interruption_words=1, min_interruption_duration_ms=1000,
    )
    action, reason = p.evaluate("wait", duration_ms=100)
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"


def test_solo_no_still_interrupts():
    """Solo 'no' — the caller correcting us — must interrupt fast."""
    p = BargeInPolicy(min_interruption_words=3)
    action, _ = p.evaluate("no", duration_ms=200)
    assert action is BargeAction.INTERRUPT


# ── min_words floor blocks short non-cue speech ─────────────────


def test_one_word_non_cue_below_min_words_continues():
    """A single 'okay-y-y-y' style stumble under the min-word floor
    should NOT interrupt.  Old behaviour would.  This is the LiveKit-
    style micro-interruption suppression."""
    p = BargeInPolicy(min_interruption_words=2)
    action, reason = p.evaluate("umm", duration_ms=200)
    # "umm" is not a backchannel token in our list; base classify
    # would return INTERRUPT (short 1-4 word), policy floors it.
    assert action is BargeAction.CONTINUE
    assert reason.startswith("min_words_not_met")


def test_two_word_non_cue_meets_min_words():
    p = BargeInPolicy(min_interruption_words=2)
    action, reason = p.evaluate("different time", duration_ms=800)
    assert action is BargeAction.INTERRUPT
    assert reason == "policy_pass"


# ── min_duration floor blocks brief speech ──────────────────────


def test_short_duration_below_floor_continues():
    p = BargeInPolicy(
        min_interruption_words=1, min_interruption_duration_ms=500,
    )
    action, reason = p.evaluate("different time", duration_ms=200)
    assert action is BargeAction.CONTINUE
    assert reason.startswith("min_duration_not_met")


def test_duration_zero_does_not_reject():
    """duration_ms=0 = signal not reported.  Policy must not
    invent rejection there."""
    p = BargeInPolicy(min_interruption_duration_ms=1000)
    action, _ = p.evaluate("what about tuesday", duration_ms=0)
    assert action is BargeAction.INTERRUPT


# ── base actions pass through ─────────────────────────────────


def test_empty_still_ignored():
    p = BargeInPolicy()
    action, reason = p.evaluate("", duration_ms=500)
    assert action is BargeAction.IGNORE
    assert reason == "base:ignore"


def test_backchannel_still_continues():
    p = BargeInPolicy()
    action, reason = p.evaluate("mhm", duration_ms=1000)
    assert action is BargeAction.CONTINUE
    assert reason == "base:continue"


def test_multi_word_backchannel_still_continues():
    p = BargeInPolicy()
    action, _ = p.evaluate("yeah yeah", duration_ms=800)
    assert action is BargeAction.CONTINUE


# ── policy composability ──────────────────────────────────────


def test_trust_explicit_cues_can_be_disabled():
    """A/B config path — if the deployer wants NO cue bypass
    (rare), the min floors apply universally."""
    p = BargeInPolicy(
        min_interruption_words=3, trust_explicit_cues=False,
    )
    action, reason = p.evaluate("stop", duration_ms=100)
    # 'stop' is 1 word — below the 3-word floor — and cue bypass off.
    assert action is BargeAction.CONTINUE
    assert reason.startswith("min_words_not_met")


def test_default_policy_matches_livekit_defaults():
    """LiveKit ships min_words=2 + min_duration=500ms — starting
    point for a phone receptionist that hears 'wait' but not a cough."""
    assert DEFAULT_BARGE_POLICY.min_interruption_words == 2
    assert DEFAULT_BARGE_POLICY.min_interruption_duration_ms == 500
    assert DEFAULT_BARGE_POLICY.trust_explicit_cues is True


def test_policy_is_frozen():
    """No accidental mutation after construction."""
    p = BargeInPolicy()
    with pytest.raises(Exception):
        p.min_interruption_words = 999  # type: ignore[misc]


# ── real Roxana/Christiaan-style triggers ─────────────────────


@pytest.mark.parametrize("cue,expected", [
    ("stop", BargeAction.INTERRUPT),
    ("wait", BargeAction.INTERRUPT),
    ("hold on", BargeAction.INTERRUPT),
    ("actually never mind", BargeAction.INTERRUPT),
    ("cancel that", BargeAction.INTERRUPT),
])
def test_real_interrupt_cues_all_pass_defaults(cue, expected):
    action, _ = DEFAULT_BARGE_POLICY.evaluate(cue, duration_ms=400)
    assert action is expected


@pytest.mark.parametrize("noise", [
    "uh", "um", "eh", "ah",
])
def test_stumble_noises_dont_interrupt_by_default(noise):
    """Single-word non-cue stumbles must NOT interrupt under default
    policy.  This is the whole point of the min-words knob."""
    action, _ = DEFAULT_BARGE_POLICY.evaluate(noise, duration_ms=250)
    assert action is BargeAction.CONTINUE
