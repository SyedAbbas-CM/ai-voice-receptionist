"""T1 LK-steal acceptance — OutputReadyGate (task #102).

Tests the primitive that closes the "greeting-clipped" bug. Gate holds
first TTS chunk until (a) inbound Twilio media frame proves the RTP
path is warm OR (b) fallback timer elapses (protecting against silent
callers who never send audio right away).

Wire integration into twilio_actor.py is a separate step — this file
locks in the gate's contract so a future wire-up (or refactor) can't
regress the fast-start behavior.
"""
from __future__ import annotations

import asyncio

import pytest

from packages.voice.output_ready_gate import OutputReadyGate


# ─── 1. Contract check — public surface ─────────────────────────────────────


def test_gate_starts_not_ready():
    gate = OutputReadyGate()
    assert gate.is_ready is False
    assert gate.first_frame_played_at_ms is None
    assert gate.unblock_reason is None


# ─── 2. Inbound-media path — the happy path ─────────────────────────────────


@pytest.mark.asyncio
async def test_gate_opens_on_inbound_media():
    """First inbound Twilio media frame unblocks the gate immediately."""
    gate = OutputReadyGate(fallback_ms=1000)
    gate.mark_stream_started()

    # Simulate inbound frame after 10ms
    async def sender():
        await asyncio.sleep(0.01)
        gate.mark_inbound_media_received()

    asyncio.create_task(sender())
    reason = await asyncio.wait_for(gate.wait(), timeout=0.5)
    assert reason == "inbound_media"
    assert gate.is_ready is True
    assert gate.unblock_reason == "inbound_media"


@pytest.mark.asyncio
async def test_repeat_mark_inbound_is_idempotent():
    gate = OutputReadyGate()
    gate.mark_stream_started()
    gate.mark_inbound_media_received()
    gate.mark_inbound_media_received()  # should not throw or double-set
    assert gate.is_ready is True


# ─── 3. Fallback path — silent caller ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_falls_back_after_timeout():
    """If no inbound media arrives, the fallback timer opens the gate
    so the greeting isn't blocked forever."""
    gate = OutputReadyGate(fallback_ms=50)  # tight for test
    gate.mark_stream_started()

    reason = await asyncio.wait_for(gate.wait(), timeout=0.5)
    assert reason == "fallback"
    assert gate.is_ready is True


@pytest.mark.asyncio
async def test_inbound_media_wins_race_against_fallback():
    """When both fire, inbound_media wins — proves the fallback isn't
    just always racing to open first."""
    gate = OutputReadyGate(fallback_ms=200)
    gate.mark_stream_started()

    async def sender():
        await asyncio.sleep(0.01)  # much faster than 200ms fallback
        gate.mark_inbound_media_received()

    asyncio.create_task(sender())
    reason = await asyncio.wait_for(gate.wait(), timeout=1.0)
    assert reason == "inbound_media"


# ─── 4. First-frame timestamp (for T7 chain) ────────────────────────────────


def test_first_frame_played_populated_on_mark():
    gate = OutputReadyGate()
    assert gate.first_frame_played_at_ms is None
    gate.mark_first_frame_played(mark_ts_ms=12345.6)
    assert gate.first_frame_played_at_ms == 12345.6


def test_first_frame_played_defaults_to_now():
    import time
    gate = OutputReadyGate()
    before = time.monotonic() * 1000.0
    gate.mark_first_frame_played()  # no ts arg
    after = time.monotonic() * 1000.0
    assert gate.first_frame_played_at_ms is not None
    assert before - 1 <= gate.first_frame_played_at_ms <= after + 1


def test_first_frame_played_is_idempotent():
    gate = OutputReadyGate()
    gate.mark_first_frame_played(1000)
    gate.mark_first_frame_played(2000)  # SHOULD NOT overwrite
    assert gate.first_frame_played_at_ms == 1000


# ─── 5. Wait behavior ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_returns_already_ready_when_gate_open():
    gate = OutputReadyGate()
    gate.mark_stream_started()
    gate.mark_inbound_media_received()
    reason = await gate.wait()
    assert reason == "already_ready"


@pytest.mark.asyncio
async def test_wait_honors_timeout():
    """Defensive per-caller max wait. Independent of the fallback."""
    gate = OutputReadyGate(fallback_ms=10_000)  # 10s fallback — long
    gate.mark_stream_started()
    reason = await gate.wait(timeout_ms=50)  # give up after 50ms
    assert reason == "timeout"
    # Even after timeout, subsequent inbound-media calls are safe no-ops
    gate.mark_inbound_media_received()


# ─── 6. Cross-actor isolation — sanity ──────────────────────────────────────


@pytest.mark.asyncio
async def test_two_gates_are_independent():
    a = OutputReadyGate(fallback_ms=1000)
    b = OutputReadyGate(fallback_ms=1000)
    a.mark_stream_started()
    b.mark_stream_started()

    a.mark_inbound_media_received()
    assert a.is_ready is True
    assert b.is_ready is False


# ─── 7. No event loop — degraded but safe ───────────────────────────────────


def test_mark_stream_started_without_event_loop_does_not_crash():
    """When called from a sync context (test harness, etc.) with no
    running loop, the fallback scheduling silently no-ops. Gate still
    usable if manually driven."""
    gate = OutputReadyGate(fallback_ms=100)
    # This must not raise
    try:
        gate.mark_stream_started()
    except RuntimeError:
        pytest.fail("mark_stream_started raised without running loop")
    gate.mark_inbound_media_received()
    assert gate.is_ready is True


# ─── 8. Zero-fallback edge case ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_fallback_opens_immediately_on_stream_start():
    """fallback_ms=0 means the greeting fires immediately — used as a
    kill-switch to disable the gate entirely."""
    gate = OutputReadyGate(fallback_ms=0)
    gate.mark_stream_started()
    reason = await asyncio.wait_for(gate.wait(), timeout=0.5)
    assert reason in ("fallback", "already_ready")
    assert gate.is_ready
