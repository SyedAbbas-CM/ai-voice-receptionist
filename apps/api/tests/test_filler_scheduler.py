"""FillerScheduler tests (task #152, LK port T5).

The scheduler must:
  * Fire filler exactly when delay elapses
  * Stay silent when disarm() fires before delay
  * Fire multiple times up to max_steps at interval spacing
  * Cancel in-flight speaker on disarm mid-speech
  * Never raise out of arm/disarm public surface
  * Handle picker/speaker exceptions gracefully
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from packages.voice.filler_scheduler import FillerScheduler


# ── construction ─────────────────────────────────────


def test_construct_rejects_negative_delay():
    with pytest.raises(ValueError):
        FillerScheduler(
            picker=lambda step: "x",
            speaker=lambda text: asyncio.sleep(0),
            delay_ms=-1,
        )


def test_construct_rejects_zero_max_steps():
    with pytest.raises(ValueError):
        FillerScheduler(
            picker=lambda step: "x",
            speaker=lambda text: asyncio.sleep(0),
            max_steps=0,
        )


def test_construct_rejects_negative_interval():
    with pytest.raises(ValueError):
        FillerScheduler(
            picker=lambda step: "x",
            speaker=lambda text: asyncio.sleep(0),
            interval_ms=-5,
        )


def test_is_armed_false_before_arm():
    s = FillerScheduler(
        picker=lambda step: "x",
        speaker=lambda text: asyncio.sleep(0),
    )
    assert s.is_armed is False


# ── happy path: fires once after delay ─────────────


@pytest.mark.asyncio
async def test_fires_filler_after_delay():
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: f"filler-{step}",
        speaker=speak,
        delay_ms=20,
        interval_ms=None,
        max_steps=1,
    )
    s.arm()
    # Wait longer than delay so filler fires.
    await asyncio.sleep(0.08)
    assert spoken == ["filler-0"]
    assert s.speaks_fired == 1


@pytest.mark.asyncio
async def test_disarm_before_delay_prevents_fire():
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: "should-not-fire",
        speaker=speak,
        delay_ms=100,
        max_steps=1,
    )
    s.arm()
    await asyncio.sleep(0.02)   # well before delay
    s.disarm()
    # Even if we wait longer, nothing should fire.
    await asyncio.sleep(0.15)
    assert spoken == []
    assert s.speaks_fired == 0


@pytest.mark.asyncio
async def test_disarm_is_idempotent():
    s = FillerScheduler(
        picker=lambda step: "x",
        speaker=lambda text: asyncio.sleep(0),
    )
    # Disarm without arm → no-op.
    s.disarm()
    s.disarm()
    assert s.is_armed is False


# ── multi-step firing ──────────────────────────────


@pytest.mark.asyncio
async def test_fires_multiple_steps_at_interval():
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: f"step-{step}",
        speaker=speak,
        delay_ms=10,
        interval_ms=30,
        max_steps=3,
    )
    s.arm()
    # 10ms first + 30ms + 30ms = 70ms. Wait 200ms to be safe.
    await asyncio.sleep(0.2)
    assert spoken == ["step-0", "step-1", "step-2"]
    assert s.speaks_fired == 3


@pytest.mark.asyncio
async def test_stops_at_max_steps():
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: f"n{step}",
        speaker=speak,
        delay_ms=10,
        interval_ms=10,
        max_steps=2,
    )
    s.arm()
    await asyncio.sleep(0.15)
    assert len(spoken) == 2
    # Waiting more shouldn't add fires.
    await asyncio.sleep(0.1)
    assert len(spoken) == 2


# ── picker returning None ──────────────────────────


@pytest.mark.asyncio
async def test_picker_none_skips_step_but_continues_when_interval_set():
    """picker(step) returning None means 'skip this fire' — with
    interval configured, next step still tries."""
    spoken = []

    async def speak(text):
        spoken.append(text)

    def picker(step):
        return None if step == 0 else f"got-{step}"

    s = FillerScheduler(
        picker=picker,
        speaker=speak,
        delay_ms=10,
        interval_ms=20,
        max_steps=3,
    )
    s.arm()
    await asyncio.sleep(0.15)
    # First skipped, second + third fired.
    assert spoken == ["got-1", "got-2"]


@pytest.mark.asyncio
async def test_picker_none_stops_when_interval_none():
    """picker(step)=None + interval None → stops after first attempt."""
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: None,
        speaker=speak,
        delay_ms=10,
        interval_ms=None,
        max_steps=3,
    )
    s.arm()
    await asyncio.sleep(0.15)
    assert spoken == []


# ── exception handling ───────────────────────────


@pytest.mark.asyncio
async def test_picker_exception_never_propagates():
    def bad_picker(step):
        raise RuntimeError("picker broken")

    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=bad_picker,
        speaker=speak,
        delay_ms=10,
        interval_ms=None,
        max_steps=1,
    )
    s.arm()
    # Should NOT raise from the task.  We give it a beat.
    await asyncio.sleep(0.1)
    # No fires — picker returned None (via exception path).
    assert spoken == []
    # Scheduler is no longer armed (task completed).
    assert s.is_armed is False


@pytest.mark.asyncio
async def test_speaker_exception_stops_but_never_propagates():
    async def bad_speaker(text):
        raise RuntimeError("TTS broken")

    s = FillerScheduler(
        picker=lambda step: "x",
        speaker=bad_speaker,
        delay_ms=10,
        interval_ms=20,
        max_steps=3,
    )
    s.arm()
    await asyncio.sleep(0.15)
    # No crash + speaks_fired 0 because each attempt raised.
    assert s.speaks_fired == 0


# ── cancellation of in-flight speaker ───────────


@pytest.mark.asyncio
async def test_disarm_cancels_in_flight_speaker():
    """If disarm() fires while speaker is mid-await, the speaker
    coro receives CancelledError.  Prevents filler stepping on the
    real reply."""
    speak_started = asyncio.Event()

    async def slow_speaker(text):
        speak_started.set()
        await asyncio.sleep(1.0)   # simulate long TTS

    s = FillerScheduler(
        picker=lambda step: "long",
        speaker=slow_speaker,
        delay_ms=10,
        interval_ms=None,
        max_steps=1,
    )
    s.arm()
    # Wait until speaker starts.
    await asyncio.wait_for(speak_started.wait(), timeout=0.5)
    # Now disarm mid-speech.
    s.disarm()
    # Task should complete (via cancel) quickly.
    await asyncio.sleep(0.05)
    assert s.is_armed is False


# ── re-arm cycle ──────────────────────────────


@pytest.mark.asyncio
async def test_arm_after_disarm_can_fire_again():
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: "again",
        speaker=speak,
        delay_ms=10,
        interval_ms=None,
        max_steps=1,
    )
    # First cycle.
    s.arm()
    await asyncio.sleep(0.05)
    s.disarm()
    assert spoken == ["again"]
    # Second cycle.
    s.arm()
    await asyncio.sleep(0.05)
    assert spoken == ["again", "again"]


@pytest.mark.asyncio
async def test_arm_while_armed_is_noop():
    """Second consecutive arm() shouldn't start a second task."""
    spoken = []

    async def speak(text):
        spoken.append(text)

    s = FillerScheduler(
        picker=lambda step: "x",
        speaker=speak,
        delay_ms=30,
        max_steps=1,
    )
    s.arm()
    original_task = s._task
    s.arm()   # no-op
    assert s._task is original_task
    await asyncio.sleep(0.08)
    assert len(spoken) == 1
