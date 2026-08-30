"""FillerScheduler — bridge dead air during tool-call gaps.

2026-08-30 (task #152, LK port T5): when brain fires a tool call
(check_availability, book_appointment, lookup_faq), there's a
300-800ms silent gap between the LLM emitting `tool_calls=[...]` and
the tool returning a receipt.  Real callers perceive >500ms silence
as broken.  Vapi + LiveKit + our audit all identified this as a
humanness win.

Adapted from LK's beta/voice/filler_scheduler.py.  Their design
depends on their AgentSession event bus + SpeechHandle machinery;
ours is smaller — an arm/disarm interface a tool-call site owns.

## How it works

```python
scheduler = FillerScheduler(
    picker=lambda step: fill_pool.pick_text(),  # returns str or None
    speaker=lambda text: actor._speak(text),    # coro
    delay_ms=350,
    interval_ms=1500,
    max_steps=2,
)
scheduler.arm()
try:
    receipt = await self.tool_handler(tc)
finally:
    scheduler.disarm()
```

Behavior:
  * After `arm()`, wait `delay_ms`.  If `disarm()` fired in that
    window → no filler spoken (tool was fast).
  * If timeout elapses without disarm → call `picker(step=0)` for
    text, hand to `speaker(text)` (awaits until it finishes or is
    interrupted).
  * If tool still hasn't returned + `interval_ms` set → wait
    `interval_ms`, fire again with `step=1`.  Stop at `max_steps`.
  * `disarm()` is idempotent + always safe to call.  Cancels any
    in-flight speaker.

## Interoperates with existing FillerPool

`packages/voice/filler.py` already ships a `FillerPool` that
pre-synthesizes 12 audio clips at startup.  The scheduler is
transport-agnostic: it doesn't know about audio pools; the `picker`
callback returns text, `speaker` handles TTS.  Simple wire:

```python
picker=lambda step: (fill_pool.pick_text() or "one moment")
speaker=lambda text: actor._speak(text)
```

## Not doing (deferred)

  * Interrupt handling — if caller barges in during filler, the
    `speaker` coro should raise CancelledError which we propagate
    to the arm() context.  Actor's existing barge-in machinery
    handles the TTS-side stop; scheduler just needs to not
    re-schedule after cancellation.
  * Cross-tool coordination — two concurrent tool_calls in one
    round could arm twice; scheduler is single-shot, second arm()
    is a no-op unless the first disarmed first.  Real callers
    rarely see this in production (tool calls serialize in the
    brain loop) but tests cover the guard.

Never raises out of the public arm/disarm surface.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional


log = logging.getLogger(__name__)


# Picker: returns the TEXT to speak for step N, or None to skip.
PickerFn = Callable[[int], Optional[str]]

# Speaker: coroutine that speaks the text.  Should honor
# asyncio.CancelledError so disarm during a fire can interrupt
# mid-speech cleanly.
SpeakerFn = Callable[[str], Awaitable[None]]


class FillerScheduler:
    """One-shot per-arm scheduler.  Owns a background task while armed."""

    def __init__(
        self,
        picker: PickerFn,
        speaker: SpeakerFn,
        *,
        delay_ms: int = 350,
        interval_ms: Optional[int] = 1500,
        max_steps: int = 2,
    ) -> None:
        if delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        if interval_ms is not None and interval_ms < 0:
            raise ValueError("interval_ms must be non-negative when set")
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self._picker = picker
        self._speaker = speaker
        self._delay_s = delay_ms / 1000.0
        self._interval_s = (
            interval_ms / 1000.0 if interval_ms is not None else None
        )
        self._max_steps = max_steps
        self._task: Optional[asyncio.Task] = None
        self._speaks_fired: int = 0

    @property
    def is_armed(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def speaks_fired(self) -> int:
        return self._speaks_fired

    def arm(self) -> None:
        """Start the delay timer.  Second consecutive arm() while
        already armed is a no-op — call disarm() first to reset."""
        if self.is_armed:
            return
        self._speaks_fired = 0
        try:
            self._task = asyncio.create_task(
                self._run(), name="filler-scheduler",
            )
        except RuntimeError:
            # No running event loop — scheduler is inert.  This
            # happens in some test paths that arm() outside asyncio.
            log.debug("filler_scheduler.arm: no running loop, inert")
            self._task = None

    def disarm(self) -> None:
        """Cancel pending work + any in-flight speaker.  Idempotent."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None

    async def _run(self) -> None:
        """Delay → speak → maybe repeat → done."""
        try:
            step = 0
            while step < self._max_steps:
                await asyncio.sleep(
                    self._delay_s if step == 0 else (
                        self._interval_s or 0
                    )
                )
                try:
                    text = self._picker(step)
                except Exception as e:
                    log.warning(
                        "filler_scheduler picker raised: %s", e,
                    )
                    text = None
                if not text:
                    # Picker declined this step; keep going if
                    # interval configured, else stop.
                    if self._interval_s is None:
                        break
                    step += 1
                    continue
                try:
                    await self._speaker(text)
                    self._speaks_fired += 1
                except asyncio.CancelledError:
                    # Speaker was cut — either disarm() fired or
                    # the caller barged in.  Either way we're done.
                    raise
                except Exception as e:
                    log.warning(
                        "filler_scheduler speaker raised: %s", e,
                    )
                # Stop after first fire if interval is None.
                if self._interval_s is None:
                    break
                step += 1
        except asyncio.CancelledError:
            # Clean cancellation — no re-raise beyond this task.
            pass


__all__ = [
    "FillerScheduler",
    "PickerFn",
    "SpeakerFn",
]
