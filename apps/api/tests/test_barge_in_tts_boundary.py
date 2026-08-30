"""T9 tests (task #153): BargeInPolicy.evaluate_with_tts_boundary.

Leading grace: backchannel arriving in first N ms of TTS → CONTINUE.
Trailing grace: backchannel arriving in last N ms of TTS → CONTINUE.
Explicit interrupt cues ('stop', 'wait') always bypass both windows.
Non-backchannel utterances fall through to normal evaluate() semantics.

Existing evaluate() tests in test_barge_in_policy.py cover the timing-
free path — this file only adds TTS-boundary coverage.
"""
from __future__ import annotations

import pytest

from packages.voice.barge_in import BargeAction, BargeInPolicy


# ── leading grace ─────────────────────────────────────


def test_backchannel_in_leading_grace_continues():
    """Caller says 'mhm' 200ms into agent TTS start → keep talking."""
    p = BargeInPolicy(leading_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_started_ms_ago=200,
    )
    assert action is BargeAction.CONTINUE
    assert "leading_backchannel_grace" in reason


def test_backchannel_at_edge_of_leading_grace_continues():
    """Just under the threshold — still CONTINUE."""
    p = BargeInPolicy(leading_backchannel_grace_ms=1000)
    action, _ = p.evaluate_with_tts_boundary(
        "yeah",
        agent_tts_started_ms_ago=999,
    )
    assert action is BargeAction.CONTINUE


def test_backchannel_beyond_leading_grace_falls_through():
    """1500ms into TTS — outside the leading grace, so backchannel
    handling reverts to base classify_barge (which returns CONTINUE
    for 'mhm' anyway)."""
    p = BargeInPolicy(leading_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_started_ms_ago=1500,
    )
    # Base already returns CONTINUE for a raw backchannel token.
    assert action is BargeAction.CONTINUE
    # But the reason should NOT mention the leading grace.
    assert "leading_backchannel_grace" not in reason


# ── trailing grace ────────────────────────────────────


def test_backchannel_in_trailing_grace_continues():
    """Caller says 'yeah' when 500ms remains in agent TTS → keep talking
    (agent's last word not cut)."""
    p = BargeInPolicy(trailing_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "yeah",
        agent_tts_ends_in_ms=500,
    )
    assert action is BargeAction.CONTINUE
    assert "trailing_backchannel_grace" in reason


def test_backchannel_at_edge_of_trailing_grace_continues():
    p = BargeInPolicy(trailing_backchannel_grace_ms=1000)
    action, _ = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_ends_in_ms=999,
    )
    assert action is BargeAction.CONTINUE


def test_backchannel_beyond_trailing_grace_falls_through():
    """5s of TTS left — trailing grace doesn't apply."""
    p = BargeInPolicy(trailing_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_ends_in_ms=5000,
    )
    assert action is BargeAction.CONTINUE  # base is CONTINUE
    assert "trailing_backchannel_grace" not in reason


# ── explicit cues bypass grace windows ─────────────────


def test_explicit_stop_bypasses_leading_grace():
    """'stop' 100ms into TTS — the caller means it. Interrupt now."""
    p = BargeInPolicy(leading_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "stop",
        agent_tts_started_ms_ago=100,
    )
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"


def test_explicit_wait_bypasses_trailing_grace():
    p = BargeInPolicy(trailing_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "wait",
        agent_tts_ends_in_ms=500,
    )
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"


def test_solo_no_bypasses_grace():
    """Solo 'no' — a real correction, agent must stop."""
    p = BargeInPolicy(
        leading_backchannel_grace_ms=1000,
        trailing_backchannel_grace_ms=1000,
    )
    action, reason = p.evaluate_with_tts_boundary(
        "no",
        agent_tts_started_ms_ago=200,
    )
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"


# ── multi-word non-backchannel utterances ───────────


def test_multi_word_utterance_in_leading_grace_still_interrupts():
    """'what time is that' during first 500ms — not a backchannel,
    grace doesn't apply, normal min-word/duration gates run."""
    p = BargeInPolicy(
        min_interruption_words=2,
        leading_backchannel_grace_ms=1000,
    )
    action, _ = p.evaluate_with_tts_boundary(
        "what time is that",
        duration_ms=800,
        agent_tts_started_ms_ago=500,
    )
    assert action is BargeAction.INTERRUPT


def test_single_word_non_backchannel_still_falls_to_min_gate():
    """'umm' during leading grace — base is INTERRUPT (short 1-4
    word), not CONTINUE, so grace doesn't apply. Then min-words
    gate applies."""
    p = BargeInPolicy(
        min_interruption_words=2,
        leading_backchannel_grace_ms=1000,
    )
    action, reason = p.evaluate_with_tts_boundary(
        "umm",
        agent_tts_started_ms_ago=200,
    )
    assert action is BargeAction.CONTINUE
    # Reason indicates min-words gate fired, not leading grace.
    assert "min_words_not_met" in reason


# ── boundary conditions ─────────────────────────────


def test_no_tts_timing_supplied_falls_through_to_evaluate():
    """Missing agent_tts_started_ms_ago + agent_tts_ends_in_ms →
    behaves identically to bare evaluate()."""
    p = BargeInPolicy()
    action1, reason1 = p.evaluate_with_tts_boundary(
        "different time",
        duration_ms=800,
    )
    action2, reason2 = p.evaluate("different time", duration_ms=800)
    assert action1 == action2
    assert reason1 == reason2


def test_zero_ms_ago_still_within_grace():
    """agent_tts_started_ms_ago=0 (frame arrived exactly at TTS start)
    → within grace."""
    p = BargeInPolicy(leading_backchannel_grace_ms=500)
    action, _ = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_started_ms_ago=0,
    )
    assert action is BargeAction.CONTINUE


def test_negative_tts_timing_ignored():
    """Negative values (clock skew) → ignored, falls through."""
    p = BargeInPolicy(leading_backchannel_grace_ms=1000)
    action, reason = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_started_ms_ago=-100,
    )
    # Base classify_barge returns CONTINUE for 'mhm' anyway.
    assert action is BargeAction.CONTINUE
    # But grace shouldn't have fired.
    assert "leading_backchannel_grace" not in reason


# ── config knobs work ────────────────────────────


def test_zero_grace_disables_leading_gate():
    """leading_backchannel_grace_ms=0 → grace never fires."""
    p = BargeInPolicy(leading_backchannel_grace_ms=0)
    action, reason = p.evaluate_with_tts_boundary(
        "mhm",
        agent_tts_started_ms_ago=1,
    )
    # Grace didn't apply — but base still says CONTINUE for 'mhm'.
    assert action is BargeAction.CONTINUE
    assert "leading_backchannel_grace" not in reason


def test_default_grace_1000_matches_lk():
    p = BargeInPolicy()
    assert p.leading_backchannel_grace_ms == 1000
    assert p.trailing_backchannel_grace_ms == 1000


# ── existing evaluate() still works (backward-compat) ─────


def test_existing_evaluate_still_works_unchanged():
    """The pre-T9 evaluate() signature MUST NOT have changed.
    Regression guard."""
    p = BargeInPolicy()
    action, reason = p.evaluate("mhm")
    assert action is BargeAction.CONTINUE
    assert reason.startswith("base:")

    action, reason = p.evaluate("stop", duration_ms=200)
    assert action is BargeAction.INTERRUPT
    assert reason == "explicit_cue"
