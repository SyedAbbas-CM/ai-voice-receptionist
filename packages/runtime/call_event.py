"""Immutable event envelope for the per-call actor.

Sprint 8a: every media/STT/LLM/tool/TTS/playback signal is wrapped in
one of these before entering the actor's mailbox.  A newer
`turn_generation` invalidates events from older turns (the invariant
that makes cancellation actually work); a newer `speech_generation`
invalidates queued audio and alignment.

Frozen so nothing can mutate the envelope after emission — the actor
only decides whether to accept or reject.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventSource(str, Enum):
    MEDIA = "media"          # inbound audio frames from Twilio / WebRTC
    STT = "stt"              # partial + final speech hypotheses
    LLM = "llm"              # streaming text + tool-call deltas
    TOOL = "tool"            # tool proposal + result
    TTS = "tts"              # generated audio + alignment
    PLAYBACK = "playback"    # Twilio mark ack, clear ack, buffer levels
    TIMER = "timer"          # deadline / debounce / retry
    CONTROL = "control"      # hang-up, transfer, start-of-call, end-of-call


def _monotonic_ns() -> int:
    """Monotonic wallclock in nanoseconds — deterministic ordering
    across events emitted from different tasks."""
    return time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class CallEvent:
    """One event flowing through the call actor's mailbox.

    Fields:
      call_id / tenant_id — identity + isolation, forwarded from the
        authenticated media gateway.
      sequence — actor-scoped monotonic sequence.  Assigned by the
        actor on enqueue, NOT by the source, so the actor is the
        authority on ordering.
      monotonic_ns — event birth-time on this process.  Used for
        latency spans and deadline enforcement.
      source — which subsystem emitted this event.
      turn_generation — every new caller utterance increments this.
        Late events from an older turn are DROPPED by the actor.
      speech_generation — every new agent speech reply increments this.
        Late audio chunks from a cancelled response are DROPPED.
      kind — subsystem-specific event type ("partial_hypothesis",
        "final_hypothesis", "tool_result", "audio_chunk", "mark_ack",
        "deadline_reached", etc).
      payload — event-specific data.  Typed at the consumer boundary.
    """
    call_id: str
    tenant_id: str
    sequence: int
    monotonic_ns: int
    source: EventSource
    turn_generation: int
    speech_generation: int
    kind: str
    payload: Any = field(default=None)
    # Sprint 12 Track A: the actor turn_generation active at the moment
    # the underlying signal (audio frame, HTTP response chunk, etc.) was
    # captured.  If the signal was captured before a bump_turn and the
    # event only reaches the mailbox afterward, source_epoch stays with
    # the OLD generation and the actor drops it as stale.
    #
    # Distinct from turn_generation (stamped at emit time).  0 = untracked.
    source_epoch: int = field(default=0)

    @classmethod
    def new(
        cls,
        *,
        call_id: str,
        tenant_id: str,
        source: EventSource,
        turn_generation: int,
        speech_generation: int,
        kind: str,
        payload: Any = None,
        sequence: int = 0,
        source_epoch: int = 0,
    ) -> "CallEvent":
        """Build a CallEvent with monotonic_ns filled in.  `sequence`
        defaults to 0 — actual sequence gets stamped by the actor on
        enqueue; callers don't set it.

        `source_epoch` (Sprint 12 Track A): the actor generation active
        when the underlying signal was captured, if the caller wants
        stale-event dropping to be based on capture-time rather than
        emit-time.  Default 0 = untracked (existing callers unaffected)."""
        return cls(
            call_id=call_id,
            tenant_id=tenant_id,
            sequence=sequence,
            monotonic_ns=_monotonic_ns(),
            source=source,
            turn_generation=turn_generation,
            speech_generation=speech_generation,
            kind=kind,
            payload=payload,
            source_epoch=source_epoch,
        )
