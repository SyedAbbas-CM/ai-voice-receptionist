"""Sprint 10 C2: TurnManager tests.

Coverage of the 7-event taxonomy:
  * EAGER_END_OF_TURN on first final
  * END_OF_TURN after confirmation window with no resume
  * TURN_RESUMED when caller keeps talking
  * BACKCHANNEL for 'yeah'/'mm-hm' during agent speech
  * USER_REQUESTED_PAUSE for 'hold on'
  * INTERRUPTION on stable non-backchannel partial during speech
  * FALSE_INTERRUPTION when speech_start w/ no content within deadline

Plus classify_short_utterance unit tests.
"""
from __future__ import annotations

import asyncio

import pytest

from packages.runtime import (
    CallActor, CallState, TurnEventKind, TurnManager, TurnManagerConfig,
    classify_short_utterance,
)
from packages.runtime.call_event import EventSource


# ── classify_short_utterance ────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("yeah", TurnEventKind.BACKCHANNEL),
    ("mm-hm", TurnEventKind.BACKCHANNEL),
    ("mhm", TurnEventKind.BACKCHANNEL),
    ("okay", TurnEventKind.BACKCHANNEL),
    ("Yes.", TurnEventKind.BACKCHANNEL),
    ("Sounds good", TurnEventKind.BACKCHANNEL),
    ("hold on a sec", TurnEventKind.USER_REQUESTED_PAUSE),
    ("give me a second", TurnEventKind.USER_REQUESTED_PAUSE),
    ("wait", TurnEventKind.USER_REQUESTED_PAUSE),
    ("let me check my calendar", TurnEventKind.USER_REQUESTED_PAUSE),
    ("just a moment", TurnEventKind.USER_REQUESTED_PAUSE),
    ("book me a cleaning next week", None),
    ("actually, thursday works", None),
    ("", None),
])
def test_classify_short_utterance(text, expected):
    assert classify_short_utterance(text) == expected


# ── async fixtures ─────────────────────────────────────────────────

async def _capture_events(actor: CallActor, kinds: list[str]) -> list[dict]:
    """Wire an actor to capture CONTROL events by kind."""
    seen: list[dict] = []

    async def _h(a, ev):
        seen.append({"kind": ev.kind, "payload": ev.payload})
        return True

    for k in kinds:
        actor.handlers[(EventSource.CONTROL, k)] = _h
    return seen


async def _wait_for_event(seen: list[dict], kind: str, timeout_s: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if any(e["kind"] == kind for e in seen):
            return True
        await asyncio.sleep(0.02)
    return False


# ── LISTENING-state events (end-of-turn detection) ─────────────────

@pytest.mark.asyncio
async def test_first_final_fires_eager_end_of_turn():
    actor = CallActor(call_id="CA-t1", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.LISTENING)

    tm = TurnManager(actor)
    await tm.on_stt_event("final", "book me tomorrow", is_final=True, speech_final=True)

    assert await _wait_for_event(seen, "eager_end_of_turn")
    await actor.stop()


@pytest.mark.asyncio
async def test_final_then_no_resume_promotes_to_end_of_turn():
    """After eager end, if no speech_start arrives during window,
    END_OF_TURN fires."""
    actor = CallActor(call_id="CA-t2", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.LISTENING)

    tm = TurnManager(actor, config=TurnManagerConfig(eager_confirm_ms=50))
    await tm.on_stt_event("final", "hi there", is_final=True, speech_final=True)

    assert await _wait_for_event(seen, "end_of_turn", timeout_s=0.5)
    await actor.stop()


@pytest.mark.asyncio
async def test_speech_resume_after_final_fires_turn_resumed():
    """Caller keeps talking after first final → TURN_RESUMED, not
    END_OF_TURN."""
    actor = CallActor(call_id="CA-t3", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.LISTENING)

    tm = TurnManager(actor, config=TurnManagerConfig(eager_confirm_ms=100))
    await tm.on_stt_event("final", "book me", is_final=True, speech_final=True)
    # Before the confirm window elapses, caller resumes
    await asyncio.sleep(0.02)
    await tm.on_stt_event("speech_start")

    assert await _wait_for_event(seen, "turn_resumed", timeout_s=0.5)
    await actor.stop()


# ── SPEAKING-state events ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_backchannel_during_speech_emits_backchannel():
    actor = CallActor(call_id="CA-t4", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor)
    await tm.on_stt_event("partial", "yeah")

    assert await _wait_for_event(seen, "backchannel")
    # Must NOT emit interruption
    assert not any(e["kind"] == "interruption" for e in seen)
    await actor.stop()


@pytest.mark.asyncio
async def test_pause_request_during_speech_emits_pause():
    actor = CallActor(call_id="CA-t5", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor)
    await tm.on_stt_event("partial", "hold on a second")

    assert await _wait_for_event(seen, "user_requested_pause")
    await actor.stop()


@pytest.mark.asyncio
async def test_content_partial_during_speech_emits_interruption():
    """Real content (not backchannel/pause) during agent speech →
    INTERRUPTION after the confirm count."""
    actor = CallActor(call_id="CA-t6", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor, config=TurnManagerConfig(
        interruption_confirm_partials=2,
    ))
    await tm.on_stt_event("partial", "actually make that Thursday")
    await tm.on_stt_event("partial", "actually make that Thursday afternoon")

    assert await _wait_for_event(seen, "interruption")
    await actor.stop()


@pytest.mark.asyncio
async def test_speech_start_no_content_emits_false_interruption():
    actor = CallActor(call_id="CA-t7", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor, config=TurnManagerConfig(
        false_interruption_deadline_ms=80,
    ))
    await tm.on_stt_event("speech_start")

    assert await _wait_for_event(seen, "false_interruption", timeout_s=0.5)
    await actor.stop()


@pytest.mark.asyncio
async def test_backchannel_cancels_false_interruption_deadline():
    """Backchannel arriving before the false-interruption deadline
    should cancel it (no false_interruption emit)."""
    actor = CallActor(call_id="CA-t8", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor, config=TurnManagerConfig(
        false_interruption_deadline_ms=200,
    ))
    await tm.on_stt_event("speech_start")
    await asyncio.sleep(0.02)
    await tm.on_stt_event("partial", "yeah")
    # Wait past deadline
    await asyncio.sleep(0.3)
    assert any(e["kind"] == "backchannel" for e in seen)
    assert not any(e["kind"] == "false_interruption" for e in seen)
    await actor.stop()


@pytest.mark.asyncio
async def test_short_partial_below_min_chars_not_interruption():
    """Single-char partials shouldn't promote to interruption (VAD noise)."""
    actor = CallActor(call_id="CA-t9", tenant_id="acme")
    seen = await _capture_events(actor, [k.value for k in TurnEventKind])
    await actor.start()
    actor.transition(CallState.SPEAKING)

    tm = TurnManager(actor, config=TurnManagerConfig(
        interruption_min_chars=5, interruption_confirm_partials=1,
    ))
    await tm.on_stt_event("partial", "uh")
    await asyncio.sleep(0.05)
    assert not any(e["kind"] == "interruption" for e in seen)
    await actor.stop()
