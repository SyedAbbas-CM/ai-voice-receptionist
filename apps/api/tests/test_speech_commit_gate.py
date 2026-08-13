"""SpeechCommitGate correctness tests.

Two things this must NOT do:
  1. Speak a WAIT_PROMISE ("one moment") when no tool call ever started.
  2. Speak an ACTION_CONFIRMATION ("you're booked") when no successful
     action-tool receipt ever landed.

Two things it MUST do:
  1. Never delay a SAFE sentence when nothing is held.
  2. Preserve original reply order — a SAFE sentence arriving AFTER a
     held sentence must NOT jump ahead in the TTS queue.

The Abdullah call regression at 2026-08-13 18:52:52 (5 utterances on
gen=12 including "Gotcha! / Let me confirm that for you. / One moment,
please." with NO tool call) is the direct motivating case for this
gate.  The `test_abdullah_scenario_no_tool_started_holds_all_waits`
test is that exact reply — if it ever passes without dropping the wait
promises, the guard is broken.
"""
from __future__ import annotations

from typing import List

import pytest

from packages.core_agent.speech_commit_gate import (
    SpeechClass,
    SpeechCommitGate,
    classify,
)


# ── classifier unit tests ────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Sure, I can help with that.", SpeechClass.SAFE),
    ("The next available time is 8:30 AM.", SpeechClass.SAFE),
    ("Hello!", SpeechClass.SAFE),

    ("One moment, please.", SpeechClass.WAIT_PROMISE),
    ("Let me check the calendar.", SpeechClass.WAIT_PROMISE),
    ("Let me confirm that for you.", SpeechClass.WAIT_PROMISE),
    ("I'll pull up availability.", SpeechClass.WAIT_PROMISE),
    ("Hold on a sec.", SpeechClass.WAIT_PROMISE),
    ("Just a moment.", SpeechClass.WAIT_PROMISE),
    ("Checking now.", SpeechClass.WAIT_PROMISE),
    ("Bear with me.", SpeechClass.WAIT_PROMISE),

    ("You're all set!", SpeechClass.ACTION_CONFIRMATION),
    ("You're booked for tomorrow.", SpeechClass.ACTION_CONFIRMATION),
    ("Your appointment is confirmed.", SpeechClass.ACTION_CONFIRMATION),
    ("I've booked you in.", SpeechClass.ACTION_CONFIRMATION),
    ("See you tomorrow!", SpeechClass.ACTION_CONFIRMATION),
    ("Locked in.", SpeechClass.ACTION_CONFIRMATION),

    ("", SpeechClass.SAFE),
    ("   ", SpeechClass.SAFE),
])
def test_classify(text: str, expected: SpeechClass):
    assert classify(text) == expected


# ── gate: safe sentences stream immediately ──────────────────────────

@pytest.mark.asyncio
async def test_safe_sentence_releases_immediately():
    released: List[str] = []

    async def release(s):
        released.append(s)

    g = SpeechCommitGate(release=release, call_id="T1", turn_gen=1)
    await g.on_sentence("Sure, I can help with that.")
    assert released == ["Sure, I can help with that."]
    assert g.stats.safe_released == 1


# ── gate: WAIT_PROMISE held without tool, released on tool start ────

@pytest.mark.asyncio
async def test_wait_promise_held_until_tool_starts():
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T2", turn_gen=1)
    await g.on_sentence("Sure!")
    await g.on_sentence("Let me check availability.")
    # Only the safe sentence has been released; the wait is held.
    assert released == ["Sure!"]
    assert g.stats.wait_held == 1

    # Tool call starts — the wait becomes honest.
    await g.on_tool_call_started("check_availability")
    assert released == ["Sure!", "Let me check availability."]
    assert g.stats.wait_released == 1


@pytest.mark.asyncio
async def test_wait_promise_released_immediately_if_tool_already_running():
    """A tool call started earlier in the turn → later wait phrase is
    honest.  This models the real streaming order where the LLM
    dispatches a tool round 1, then round 2 emits 'checking now'."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T3", turn_gen=1)
    await g.on_tool_call_started("check_availability")
    await g.on_sentence("Checking now for you.")
    assert released == ["Checking now for you."]
    assert g.stats.wait_released == 1
    assert g.stats.wait_held == 0


# ── gate: WAIT_PROMISE dropped at flush if no tool ever started ─────

@pytest.mark.asyncio
async def test_wait_promise_dropped_at_flush_when_no_tool():
    """The Hamzah / Abdullah fake-wait case.  Wait promise arrives, no
    tool call ever fires, stream ends — must be DROPPED, not spoken."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T4", turn_gen=1)
    await g.on_sentence("One moment, please.")
    assert released == []
    dropped = await g.flush()
    assert dropped == ["One moment, please."]
    assert released == []
    assert g.stats.wait_dropped == 1


# ── gate: ACTION_CONFIRMATION held until successful receipt ─────────

@pytest.mark.asyncio
async def test_action_confirmation_held_until_successful_receipt():
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T5", turn_gen=1)
    await g.on_sentence("You're booked for tomorrow at eight thirty!")
    assert released == []
    assert g.stats.action_held == 1

    # Tool call started but NOT a successful receipt yet.
    await g.on_tool_call_started("book_appointment")
    assert released == []

    # Receipt lands, ok=True.
    await g.on_tool_receipt("book_appointment", ok=True)
    assert released == ["You're booked for tomorrow at eight thirty!"]
    assert g.stats.action_released == 1


@pytest.mark.asyncio
async def test_action_confirmation_dropped_on_failed_receipt():
    """Tool ran but failed — confirmation must stay held and be
    dropped at flush.  Downstream must NOT tell the caller they're
    booked when the booking failed."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T6", turn_gen=1)
    await g.on_sentence("You're all set!")
    await g.on_tool_call_started("book_appointment")
    await g.on_tool_receipt("book_appointment", ok=False)
    dropped = await g.flush()
    assert dropped == ["You're all set!"]
    assert g.stats.action_dropped == 1


# ── gate: order preservation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_safe_after_held_does_not_jump_the_queue():
    """This is the incoherence bug: reply order MUST be preserved even
    if a later SAFE sentence is trivially releasable while the head is
    held.  Otherwise:
       'Let me check availability.'  [held, WAIT_PROMISE]
       'The next slot is 8:30 AM.'   [SAFE — would jump ahead]
    speaks the slot BEFORE the wait phrase.  Wrong."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T7", turn_gen=1)
    await g.on_sentence("Let me check availability.")
    await g.on_sentence("The next slot is 8:30 AM.")
    # Neither released yet — the SAFE one is queued behind the held.
    assert released == []
    # Tool starts — both drain in original order.
    await g.on_tool_call_started("check_availability")
    assert released == [
        "Let me check availability.",
        "The next slot is 8:30 AM.",
    ]


@pytest.mark.asyncio
async def test_multiple_holds_release_in_order():
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T8", turn_gen=1)
    await g.on_sentence("Let me check.")               # WAIT
    await g.on_sentence("I'll pull up availability.")  # WAIT
    await g.on_sentence("Give me a second.")           # WAIT
    assert released == []
    await g.on_tool_call_started("check_availability")
    assert released == [
        "Let me check.",
        "I'll pull up availability.",
        "Give me a second.",
    ]


# ── gate: real Abdullah reply — the whole point of this module ──────

@pytest.mark.asyncio
async def test_abdullah_scenario_no_tool_started_holds_all_waits():
    """Real reply from Abdullah's call 2026-08-13 18:52:48-18:52:52
    gen=12.  Brain streamed:
        'Gotcha!'
        'Let me confirm that for you.'
        'One moment, please.'
    No tool call happened.  R2's guard then rewrote the reply, but by
    that point all three had already been spoken.

    Correct behavior: 'Gotcha!' streams immediately (SAFE), the two
    wait phrases are HELD, and because no tool call ever starts, they
    are DROPPED at flush.  Caller only hears 'Gotcha!' + whatever the
    rewrite decides to speak (that path is outside the gate)."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T_ABDULLAH", turn_gen=12)
    await g.on_sentence("Gotcha!")
    await g.on_sentence("Let me confirm that for you.")
    await g.on_sentence("One moment, please.")

    # Only "Gotcha!" is on the wire.  The two waits are held.
    assert released == ["Gotcha!"]

    # Brain finishes with no tool_call this round → flush.
    dropped = await g.flush()
    assert dropped == [
        "Let me confirm that for you.",
        "One moment, please.",
    ]
    assert released == ["Gotcha!"]  # unchanged
    assert g.stats.wait_dropped == 2
    assert g.stats.safe_released == 1


# ── gate: released_text (used by actor divergence check) ────────────

@pytest.mark.asyncio
async def test_released_text_reflects_only_what_crossed_the_gate():
    """The actor uses `gate.released_text` (not `buf.full_text`) for
    its STREAM_REPLY_REPLACED divergence check.  This must equal the
    concatenation of every SAFE + released-hold sentence and MUST NOT
    include any dropped hold."""
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T10", turn_gen=1)
    await g.on_sentence("Gotcha!")                       # SAFE
    await g.on_sentence("Let me confirm that for you.")  # WAIT, held
    await g.on_sentence("One moment, please.")           # WAIT, held
    assert released == ["Gotcha!"]
    assert g.released_text == "Gotcha!"
    # Flush drops the two waits; released_text stays unchanged.
    await g.flush()
    assert g.released_text == "Gotcha!"


@pytest.mark.asyncio
async def test_released_text_grows_when_hold_becomes_releasable():
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T11", turn_gen=1)
    await g.on_sentence("Sure!")                       # SAFE
    await g.on_sentence("Let me check availability.")  # WAIT, held
    assert g.released_text == "Sure!"
    await g.on_tool_call_started("check_availability")
    # Held sentence releases → grows released_text.
    assert g.released_text == "Sure! Let me check availability."


# ── gate: closed after flush ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_late_sentences_after_flush_are_dropped_with_warn():
    released: List[str] = []
    async def release(s): released.append(s)

    g = SpeechCommitGate(release=release, call_id="T9", turn_gen=1)
    await g.on_sentence("Hello!")
    await g.flush()
    await g.on_sentence("This is too late.")
    assert released == ["Hello!"]
