"""CallActor + CallEvent + PlaybackLedger tests.

Sprint 7/8b — the temporal correctness kernel.

Test families (from deep-research doc §"required new tests"):
  * Actor determinism — same event log → same transitions
  * Actor concurrency — late events from superseded generations rejected
  * Cancellation — bump_turn cancels in-flight turn task
  * Playback history — heard-text reflects only ack'd marks
  * Registry — get_or_create returns same actor for (call_id, tenant_id)
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from packages.runtime import (
    AudioChunk,
    CallActor,
    CallActorRegistry,
    CallEvent,
    CallState,
    EventSource,
    PlaybackLedger,
)


# ─── PlaybackLedger ──────────────────────────────────────────────────

def test_playback_ledger_heard_text_advances_on_mark_ack():
    """The audit's core bug: after an interruption, the LLM must see
    only what the caller actually heard, not the full utterance."""
    led = PlaybackLedger()
    full = "Hi, I have openings at nine, ten thirty, and two fifteen."
    led.start_generation(speech_generation=1, full_text=full)

    # Three chunks: greeting + first slot + rest
    c1 = AudioChunk(generation_id="gen-1", sequence=0, audio_bytes=1000,
                    duration_ms=800, text="Hi, I have openings at nine, ",
                    text_start=0, text_end=28, mark_id="m1")
    c2 = AudioChunk(generation_id="gen-1", sequence=1, audio_bytes=800,
                    duration_ms=600, text="ten thirty, ",
                    text_start=28, text_end=41, mark_id="m2")
    c3 = AudioChunk(generation_id="gen-1", sequence=2, audio_bytes=1200,
                    duration_ms=900, text="and two fifteen.",
                    text_start=41, text_end=len(full), mark_id="m3",
                    is_final=True)

    for c in (c1, c2, c3):
        led.queue_chunk(1, c)

    # Twilio acks first two marks, then the caller interrupts
    led.mark_ack(1, "m1")
    led.mark_ack(1, "m2")
    led.clear_current_generation(1)

    # Even if a late mark3 ack arrives after clear, the heard boundary
    # must NOT advance — that audio was cleared, not played.
    led.mark_ack(1, "m3")

    heard = led.heard_text_for(1)
    assert heard == "Hi, I have openings at nine, ten thirty, "
    assert heard != full, "must NOT advance past the cleared boundary"


def test_playback_ledger_mark_ack_before_clear_committed():
    """The straight-through case: no interruption, all marks ack'd,
    heard_text == full_text."""
    led = PlaybackLedger()
    full = "How can I help?"
    led.start_generation(2, full)
    c = AudioChunk(generation_id="gen-2", sequence=0, audio_bytes=500,
                   duration_ms=400, text=full,
                   text_start=0, text_end=len(full), mark_id="mA",
                   is_final=True)
    led.queue_chunk(2, c)
    led.mark_ack(2, "mA")
    assert led.heard_text_for(2) == full


# ─── CallActor determinism ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_actor_starts_and_stops_cleanly():
    actor = CallActor(call_id="CA-t1", tenant_id="acme")
    await actor.start()
    assert actor.state == CallState.CONNECTING
    await actor.stop()
    assert actor.state == CallState.ENDED


@pytest.mark.asyncio
async def test_actor_dispatches_by_source_kind():
    """Handlers are keyed by (source, kind).  Actor invokes them
    serially in mailbox order."""
    seen: list[str] = []

    async def on_final(actor: CallActor, event: CallEvent) -> bool:
        seen.append(f"final:{event.payload}")
        return True

    async def on_partial(actor: CallActor, event: CallEvent) -> bool:
        seen.append(f"partial:{event.payload}")
        return True

    actor = CallActor(call_id="CA-t2", tenant_id="acme")
    actor.handlers[(EventSource.STT, "partial")] = on_partial
    actor.handlers[(EventSource.STT, "final")] = on_final
    await actor.start()

    await actor.emit(CallEvent.new(
        call_id="CA-t2", tenant_id="acme", source=EventSource.STT,
        turn_generation=0, speech_generation=0, kind="partial",
        payload="hi",
    ))
    await actor.emit(CallEvent.new(
        call_id="CA-t2", tenant_id="acme", source=EventSource.STT,
        turn_generation=0, speech_generation=0, kind="final",
        payload="hi there",
    ))

    # Give the actor loop time to drain
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(seen) >= 2:
            break

    assert seen == ["partial:hi", "final:hi there"]
    await actor.stop()


# ─── CallActor concurrency / cancellation ──────────────────────────

@pytest.mark.asyncio
async def test_stale_events_from_old_turn_are_dropped():
    """The core CRITICAL-08 fix: a partial hypothesis from turn N
    arriving after bump_turn() advanced to N+1 must be dropped, not
    applied."""
    applied: list[int] = []

    async def on_partial(actor: CallActor, event: CallEvent) -> bool:
        applied.append(event.turn_generation)
        return True

    actor = CallActor(call_id="CA-t3", tenant_id="acme")
    actor.handlers[(EventSource.STT, "partial")] = on_partial
    await actor.start()

    # Turn 0
    await actor.emit(CallEvent.new(
        call_id="CA-t3", tenant_id="acme", source=EventSource.STT,
        turn_generation=0, speech_generation=0, kind="partial", payload="a"))

    # Sprint 12 Track A: bump_turn no longer awaits _drain_mailbox.
    # Let the mailbox dispatch the turn-0 partial before we bump —
    # else it gets correctly dropped as stale.  The test's point is
    # about POST-BUMP late arrivals, not pre-bump ones.
    await asyncio.sleep(0.05)

    # Caller starts a new utterance — bump to turn 1
    await actor.bump_turn()

    # A late STT partial from turn 0 arrives (provider hasn't cancelled yet)
    await actor.emit(CallEvent.new(
        call_id="CA-t3", tenant_id="acme", source=EventSource.STT,
        turn_generation=0, speech_generation=0, kind="partial", payload="b-late"))

    # A fresh partial for turn 1
    await actor.emit(CallEvent.new(
        call_id="CA-t3", tenant_id="acme", source=EventSource.STT,
        turn_generation=1, speech_generation=1, kind="partial", payload="c"))

    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(applied) >= 2 and actor._mailbox.empty():
            break

    # First partial from turn 0 applied.  The late one dropped.  Then turn 1.
    assert applied == [0, 1], f"expected [0, 1] but got {applied}"
    await actor.stop()


@pytest.mark.asyncio
async def test_stale_speech_events_dropped():
    """TTS chunk from an interrupted speech_generation must be dropped."""
    applied: list[int] = []

    async def on_audio(actor: CallActor, event: CallEvent) -> bool:
        applied.append(event.speech_generation)
        return True

    actor = CallActor(call_id="CA-t4", tenant_id="acme")
    actor.handlers[(EventSource.TTS, "audio_chunk")] = on_audio
    await actor.start()

    await actor.emit(CallEvent.new(
        call_id="CA-t4", tenant_id="acme", source=EventSource.TTS,
        turn_generation=0, speech_generation=0, kind="audio_chunk", payload=b"a"))

    # Sprint 12 Track A: bump_speech no longer drains the mailbox; give
    # the actor time to dispatch the pre-bump event before we advance.
    await asyncio.sleep(0.05)

    # Interrupt: bump speech generation
    await actor.bump_speech()

    # Late audio from generation 0
    await actor.emit(CallEvent.new(
        call_id="CA-t4", tenant_id="acme", source=EventSource.TTS,
        turn_generation=0, speech_generation=0, kind="audio_chunk", payload=b"late"))

    # New audio from generation 1
    await actor.emit(CallEvent.new(
        call_id="CA-t4", tenant_id="acme", source=EventSource.TTS,
        turn_generation=0, speech_generation=1, kind="audio_chunk", payload=b"new"))

    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(applied) >= 2 and actor._mailbox.empty():
            break

    assert applied == [0, 1], f"expected [0, 1] but got {applied}"
    await actor.stop()


@pytest.mark.asyncio
async def test_bump_turn_cancels_current_task():
    """A registered turn task gets cancelled when bump_turn fires."""
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def long_running_turn():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    actor = CallActor(call_id="CA-t5", tenant_id="acme")
    await actor.start()

    task = asyncio.create_task(long_running_turn())
    actor.register_turn_task(task)
    # Let the task actually start its sleep before we cancel it
    await started.wait()

    await actor.bump_turn()

    assert cancelled.is_set(), "turn task should have been cancelled"
    assert actor.turn_generation == 1
    await actor.stop()


# ─── Registry ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registry_get_or_create_returns_same_actor():
    reg = CallActorRegistry()
    call_id = f"CA-{uuid.uuid4().hex[:8]}"
    a = await reg.get_or_create(call_id, "acme")
    b = await reg.get_or_create(call_id, "acme")
    assert a is b, "same (call_id, tenant_id) must return same actor"
    await reg.stop_all()


@pytest.mark.asyncio
async def test_registry_isolates_by_tenant():
    """(call_id, tenant_A) and (call_id, tenant_B) get separate actors —
    even with the same call_id, cross-tenant isolation is enforced."""
    reg = CallActorRegistry()
    call_id = f"CA-{uuid.uuid4().hex[:8]}"
    a = await reg.get_or_create(call_id, "tenant-a")
    b = await reg.get_or_create(call_id, "tenant-b")
    assert a is not b, "same call_id under different tenants must be separate actors"
    await reg.stop_all()


@pytest.mark.asyncio
async def test_registry_setup_runs_before_actor_starts():
    """setup callback must register handlers before the run loop
    consumes any events."""
    def setup(actor: CallActor) -> None:
        async def h(a, e): return True
        actor.handlers[(EventSource.CONTROL, "start")] = h

    reg = CallActorRegistry()
    actor = await reg.get_or_create("CA-setup", "acme", setup=setup)
    assert (EventSource.CONTROL, "start") in actor.handlers
    await reg.stop_all()


# ─── Interruption end-to-end (the audit's scenario) ────────────────

@pytest.mark.asyncio
async def test_interruption_scenario_end_to_end():
    """Simulates: agent speaks 3-chunk reply, caller interrupts after
    chunk 2, ledger.heard_text_for gives us only 'Hi, I have openings
    at nine, ten thirty, '."""
    actor = CallActor(call_id="CA-int", tenant_id="acme")
    await actor.start()

    full = "Hi, I have openings at nine, ten thirty, and two fifteen."
    actor.ledger.start_generation(actor.speech_generation, full)

    # Queue 3 chunks
    actor.ledger.queue_chunk(actor.speech_generation, AudioChunk(
        generation_id=f"gen-{actor.speech_generation}", sequence=0,
        audio_bytes=1000, duration_ms=800,
        text="Hi, I have openings at nine, ",
        text_start=0, text_end=28, mark_id="m1"))
    actor.ledger.queue_chunk(actor.speech_generation, AudioChunk(
        generation_id=f"gen-{actor.speech_generation}", sequence=1,
        audio_bytes=800, duration_ms=600,
        text="ten thirty, ",
        text_start=28, text_end=41, mark_id="m2"))
    actor.ledger.queue_chunk(actor.speech_generation, AudioChunk(
        generation_id=f"gen-{actor.speech_generation}", sequence=2,
        audio_bytes=1200, duration_ms=900,
        text="and two fifteen.",
        text_start=41, text_end=len(full), mark_id="m3", is_final=True))

    # First two chunks land, caller barges in
    actor.ledger.mark_ack(actor.speech_generation, "m1")
    actor.ledger.mark_ack(actor.speech_generation, "m2")
    await actor.bump_turn(reason="barge-in")

    heard = actor.ledger.heard_text_for(0)  # generation 0 (before bump)
    assert heard == "Hi, I have openings at nine, ten thirty, "
    assert actor.turn_generation == 1

    await actor.stop()
