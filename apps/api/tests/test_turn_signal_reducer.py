"""Tests for TurnSignalReducer.

2026-08-27 (task #138): the reducer that populates
ConversationDecisionState's boolean signals (caller_shared_hardship /
caller_corrected_us / caller_is_dictating / caller_asked_to_wait) from
real transcript.  Without it, ACK selector always sees empty state
and falls back to canonical acks.

Test surface:
- hardship detection: keywords + phrases
- correction detection: 'no I said X', 'actually', explicit rejections
- wait detection: 'hold on', 'give me a sec', 'let me check'
- dictation detection: digit-run heuristic + agent-asked-for-slot signal
- slot_capture_active override wins over text heuristic
- never raises on garbage
- doesn't produce false positives on normal booking chatter
"""
from __future__ import annotations

import pytest

from packages.dialogue.turn_signal_reducer import (
    ReducedSignals,
    TurnSignalReducer,
    reduce_turn_signals,
)


@pytest.fixture
def reducer():
    return TurnSignalReducer()


# ── hardship ─────────────────────────────────────────────────────


def test_hardship_keyword_pain(reducer):
    r = reducer.reduce("my tooth's been killing me since Monday")
    assert r.caller_shared_hardship is True
    # Reason surfaced.
    assert any("hardship" in x for x in r.reasons)


def test_hardship_keyword_swollen(reducer):
    r = reducer.reduce("my gum is swollen and bleeding")
    assert r.caller_shared_hardship is True


def test_hardship_emotional_context(reducer):
    r = reducer.reduce("I've been dealing with this for weeks")
    assert r.caller_shared_hardship is True


def test_hardship_life_event(reducer):
    r = reducer.reduce("my husband passed away last month")
    assert r.caller_shared_hardship is True


def test_hardship_phrase_regex(reducer):
    r = reducer.reduce("I'm in a lot of pain and can't sleep")
    assert r.caller_shared_hardship is True


def test_hardship_not_triggered_on_normal_speech(reducer):
    """Normal booking chatter should NOT fire hardship."""
    r = reducer.reduce("I'd like to book a cleaning for Tuesday")
    assert r.caller_shared_hardship is False


def test_hardship_not_triggered_on_price_question(reducer):
    r = reducer.reduce("how much is a cleaning")
    assert r.caller_shared_hardship is False


# ── correction ───────────────────────────────────────────────────


def test_correction_no_at_start(reducer):
    r = reducer.reduce("No, I said Thursday not Tuesday")
    assert r.caller_corrected_us is True


def test_correction_actually(reducer):
    r = reducer.reduce("Actually, three thirty works better")
    assert r.caller_corrected_us is True


def test_correction_i_didnt_say(reducer):
    r = reducer.reduce("I didn't say Tuesday, I said Thursday")
    assert r.caller_corrected_us is True


def test_correction_thats_wrong(reducer):
    r = reducer.reduce("Wait, that's wrong")
    assert r.caller_corrected_us is True


def test_correction_not_triggered_on_normal_no(reducer):
    """Caller answering a yes-no question with 'no' shouldn't fire
    correction — that's a normal answer."""
    r = reducer.reduce("no")
    # Bare 'no' doesn't match the anchored patterns.
    assert r.caller_corrected_us is False


def test_correction_not_triggered_on_no_pets(reducer):
    r = reducer.reduce("no pets")
    assert r.caller_corrected_us is False


# ── wait ─────────────────────────────────────────────────────────


def test_wait_hold_on(reducer):
    r = reducer.reduce("Hold on a sec, let me grab my calendar")
    assert r.caller_asked_to_wait is True


def test_wait_give_me_a_moment(reducer):
    r = reducer.reduce("Give me a moment")
    assert r.caller_asked_to_wait is True


def test_wait_let_me_check(reducer):
    r = reducer.reduce("Let me check my schedule")
    assert r.caller_asked_to_wait is True


def test_wait_one_second(reducer):
    r = reducer.reduce("One second please")
    assert r.caller_asked_to_wait is True


def test_wait_not_triggered_on_normal_speech(reducer):
    r = reducer.reduce("I'd like Tuesday afternoon")
    assert r.caller_asked_to_wait is False


# ── dictation ───────────────────────────────────────────────────


def test_dictation_slot_capture_wins(reducer):
    """When the actor tells us slot capture is active, dictating is True
    regardless of what the text says."""
    r = reducer.reduce("yes", slot_capture_active=True)
    assert r.caller_is_dictating is True


def test_dictation_digit_run(reducer):
    """Long run of digits + digit-words → dictation."""
    r = reducer.reduce("five five five one two three four")
    assert r.caller_is_dictating is True


def test_dictation_isolated_phone_number(reducer):
    r = reducer.reduce("0333 5244772")
    assert r.caller_is_dictating is True


def test_dictation_after_agent_asked_phone(reducer):
    """Even a short digit reply after agent asked for phone → dictation."""
    r = reducer.reduce(
        "555",
        last_agent_text="What's your phone number?",
    )
    assert r.caller_is_dictating is True


def test_dictation_after_agent_asked_email(reducer):
    r = reducer.reduce(
        "j-o-h-n at gmail",
        last_agent_text="Can you spell your email for me?",
    )
    # 'j-o-h-n at gmail' contains no digits at all; only fires when
    # agent asked structured AND there are digits.  Should NOT trigger
    # here — but it's a benign miss (agent will get 'gotcha' where a
    # slot-capture-aware system wouldn't).
    # Real fix: this specific class needs actor.slot_capture_active.
    # Test documents the limitation.
    assert r.caller_is_dictating is False


def test_dictation_after_agent_asked_name(reducer):
    """Agent asked for name, caller said name → NOT dictation.
    Name isn't a digit stream; caller_is_dictating stays False."""
    r = reducer.reduce(
        "Sarah Chen",
        last_agent_text="Can I get your name please?",
    )
    assert r.caller_is_dictating is False


def test_dictation_normal_speech_not_flagged(reducer):
    """Regular booking speech shouldn't trigger dictation."""
    r = reducer.reduce(
        "I'd like to book for Tuesday at 2:30 pm please",
        last_agent_text="Sure, when would you like to come in?",
    )
    # Contains "2:30" digits but agent didn't ask structured, and the
    # tokens aren't mostly digits.
    assert r.caller_is_dictating is False


def test_dictation_partial_phone_after_ask(reducer):
    """Caller mid-utterance during phone dictation."""
    r = reducer.reduce(
        "uh five five five and I don't remember the rest",
        last_agent_text="What's your phone number?",
    )
    # Agent asked structured + has digits → dictation.
    assert r.caller_is_dictating is True


# ── multiple signals ─────────────────────────────────────────────


def test_multiple_signals_can_coexist(reducer):
    """Caller corrects us WHILE sharing hardship — both should fire.
    Downstream policy resolves precedence."""
    r = reducer.reduce("no, I said Tuesday — my tooth is killing me")
    assert r.caller_corrected_us is True
    assert r.caller_shared_hardship is True


def test_wait_and_hardship_can_coexist(reducer):
    r = reducer.reduce("hold on, this is really painful")
    assert r.caller_asked_to_wait is True
    assert r.caller_shared_hardship is True


# ── safety / never-raises ────────────────────────────────────────


def test_empty_text_returns_all_false(reducer):
    r = reducer.reduce("")
    assert r == ReducedSignals()


def test_whitespace_only(reducer):
    r = reducer.reduce("   \n\t  ")
    assert r == ReducedSignals()


def test_none_text_returns_all_false(reducer):
    r = reducer.reduce(None)  # type: ignore[arg-type]
    assert r == ReducedSignals()


def test_garbage_never_raises(reducer):
    # A pathological input shouldn't crash.
    r = reducer.reduce("\x00" * 100 + "!@#$%^&*()")
    assert isinstance(r, ReducedSignals)


def test_very_long_input_bounded(reducer):
    """10KB of text still returns cleanly in reasonable time."""
    long_text = "please book a cleaning " * 500
    r = reducer.reduce(long_text)
    # Result depends on content; assertion is that it returns.
    assert isinstance(r, ReducedSignals)


# ── to_state_kwargs ──────────────────────────────────────────────


def test_to_state_kwargs_shape(reducer):
    r = reducer.reduce("hold on, my tooth hurts")
    kw = r.to_state_kwargs()
    assert set(kw.keys()) == {
        "caller_shared_hardship",
        "caller_corrected_us",
        "caller_is_dictating",
        "caller_asked_to_wait",
    }
    assert kw["caller_shared_hardship"] is True
    assert kw["caller_asked_to_wait"] is True
    assert kw["caller_corrected_us"] is False
    assert kw["caller_is_dictating"] is False


def test_to_state_kwargs_excludes_reasons(reducer):
    """The `reasons` field is for logging, not state population."""
    r = reducer.reduce("hold on")
    assert "reasons" not in r.to_state_kwargs()


# ── convenience wrapper ──────────────────────────────────────────


def test_reduce_turn_signals_module_function():
    """The singleton-backed convenience wrapper matches instance."""
    r = reduce_turn_signals("my tooth is killing me")
    assert r.caller_shared_hardship is True


# ── integration with NextActionPolicy ────────────────────────────


def test_reducer_output_populates_decision_state():
    """Round-trip: reducer output feeds ConversationDecisionState,
    which feeds NextActionPolicy._select_ack."""
    from packages.dialogue.next_action_policy import (
        AcknowledgmentKind,
        ConversationDecisionState,
        NextActionPolicy,
    )
    r = reduce_turn_signals("my tooth's been killing me for days")
    state = ConversationDecisionState(**r.to_state_kwargs())
    policy = NextActionPolicy()
    decision = policy.decide(state)
    # Hardship → empathy ack, regardless of action type.
    assert decision.acknowledgment == AcknowledgmentKind.ACK_EMPATHY


def test_reducer_correction_yields_correction_ack():
    from packages.dialogue.next_action_policy import (
        AcknowledgmentKind,
        ConversationDecisionState,
        NextActionPolicy,
    )
    r = reduce_turn_signals("no I said Thursday")
    state = ConversationDecisionState(**r.to_state_kwargs())
    decision = NextActionPolicy().decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_CORRECTION


def test_reducer_wait_yields_wait_ack():
    from packages.dialogue.next_action_policy import (
        AcknowledgmentKind,
        ConversationDecisionState,
        NextActionPolicy,
    )
    r = reduce_turn_signals("hold on give me a sec")
    state = ConversationDecisionState(**r.to_state_kwargs())
    decision = NextActionPolicy().decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_WAIT


def test_reducer_dictation_yields_none_ack():
    from packages.dialogue.next_action_policy import (
        AcknowledgmentKind,
        ConversationDecisionState,
        NextActionPolicy,
    )
    r = reduce_turn_signals(
        "five five five one two three four",
        last_agent_text="What's your phone number?",
    )
    state = ConversationDecisionState(**r.to_state_kwargs())
    decision = NextActionPolicy().decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE
