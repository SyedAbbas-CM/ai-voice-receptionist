"""Sprint 12 Track A tests: spawn_supervised + emit_local + source_epoch."""
from __future__ import annotations

import asyncio
import pytest

from packages.runtime import CallActor, CallEvent, EventSource


def test_call_event_has_source_epoch_default_zero():
    ev = CallEvent.new(
        call_id="c1", tenant_id="t1", source=EventSource.STT,
        turn_generation=3, speech_generation=1, kind="partial",
    )
    assert ev.source_epoch == 0  # default


def test_call_event_new_accepts_source_epoch():
    ev = CallEvent.new(
        call_id="c1", tenant_id="t1", source=EventSource.STT,
        turn_generation=5, speech_generation=1, kind="partial",
        source_epoch=3,   # captured back when turn was 3
    )
    assert ev.source_epoch == 3


@pytest.mark.asyncio
async def test_spawn_supervised_returns_task_and_completes():
    """spawn_supervised runs a coroutine off the mailbox loop.  The
    task completes normally + we can await it directly."""
    actor = CallActor(call_id="c1", tenant_id="t1")
    await actor.start()
    try:
        results: list[str] = []
        async def job() -> None:
            await asyncio.sleep(0.02)
            results.append("done")
        task = actor.spawn_supervised(
            job(), generation=actor.turn_generation, name="test-job",
        )
        await task
        assert results == ["done"]
    finally:
        await actor.stop(reason="test")


@pytest.mark.asyncio
async def test_spawn_supervised_task_cancelled_on_bump_turn():
    """A supervised task for turn N gets cancelled when bump_turn
    advances past N."""
    actor = CallActor(call_id="c2", tenant_id="t2")
    await actor.start()
    try:
        cancelled = asyncio.Event()
        async def job() -> None:
            try:
                await asyncio.sleep(5)   # never finishes on its own
            except asyncio.CancelledError:
                cancelled.set()
                raise
        gen_before = actor.turn_generation
        task = actor.spawn_supervised(
            job(), generation=gen_before, name="doomed",
        )
        await asyncio.sleep(0.01)
        await actor.bump_turn(reason="test-cancel")
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("supervised task was not cancelled by bump_turn")
        assert task.done()
    finally:
        await actor.stop(reason="test")


@pytest.mark.asyncio
async def test_emit_local_from_spawned_job_reaches_mailbox():
    """A supervised job calls actor.emit_local(...) and the actor's
    run loop dispatches the event to a handler."""
    actor = CallActor(call_id="c3", tenant_id="t3")
    received: list[CallEvent] = []

    async def handler(actor_arg, event):
        received.append(event)
        return True

    actor.handlers[(EventSource.CONTROL, "job-done")] = handler
    await actor.start()
    try:
        async def job() -> None:
            actor.emit_local(CallEvent.new(
                call_id="c3", tenant_id="t3",
                source=EventSource.CONTROL,
                turn_generation=actor.turn_generation,
                speech_generation=actor.speech_generation,
                kind="job-done",
                payload={"result": 42},
            ))
        actor.spawn_supervised(
            job(), generation=actor.turn_generation, name="emitter",
        )
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 1
        assert received[0].kind == "job-done"
        assert received[0].payload == {"result": 42}
    finally:
        await actor.stop(reason="test")
