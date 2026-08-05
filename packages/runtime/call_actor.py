"""Per-call actor — the temporal correctness kernel.

Sprint 7/8b: one asyncio.Queue mailbox per active call, one coroutine
consuming it, one owner of mutable state.  Everything that used to be
spawned as `asyncio.create_task(...)` per utterance in twilio.py now
emits a CallEvent to this actor and the actor decides whether it's
still relevant.

State machine:

    CONNECTING → GREETING → LISTENING ⇄ PREPARING → THINKING
                                                       ↓
                                                   TOOL_WAIT → SPEAKING → YIELDING
                                                                            ↓
                                                                       LISTENING
    LISTENING / SPEAKING → TRANSFERRING → ENDED
    * → ENDED (hang-up)

Cancellation hierarchy:

    call lifetime
    └── turn_generation
        ├── STT hypothesis generation
        ├── LLM generation
        ├── tool proposal
        └── speech_generation
            ├── provider synthesis context
            ├── transcoding
            └── outbound playback

A newer turn_generation invalidates every event stamped with an older
one.  Late results from cancelled turns are dropped, not applied.

This module is transport-agnostic.  The Twilio route wires it up; a
future SIP/LiveKit gateway would use the same interface.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from .call_event import CallEvent, EventSource
from .playback_ledger import PlaybackLedger

log = logging.getLogger(__name__)


class CallState(str, Enum):
    CONNECTING = "connecting"
    GREETING = "greeting"
    LISTENING = "listening"
    PREPARING = "preparing"     # eager end-of-turn detected; speculative work allowed
    THINKING = "thinking"       # final endpoint; running LLM
    TOOL_WAIT = "tool_wait"
    SPEAKING = "speaking"
    YIELDING = "yielding"       # detected target caller speech mid-response
    TRANSFERRING = "transferring"
    ENDED = "ended"


# Event handler signature — actor invokes these based on event.source + kind.
# Returning False from a handler means "reject / drop this event" (e.g.
# because a newer generation invalidated it).
EventHandler = Callable[["CallActor", CallEvent], Awaitable[bool]]


@dataclass
class CallActor:
    """One actor per active call.

    Usage:

        actor = CallActor(call_id="CA...", tenant_id="acme-dental")
        await actor.start()

        # from any coroutine — media parser, STT client, TTS callback:
        await actor.emit(CallEvent.new(...))

        # actor consumes events serially and updates state.  Only the
        # actor's task mutates .state / .turn_generation / etc.

        # on call end:
        await actor.stop()
    """
    call_id: str
    tenant_id: str
    mailbox_size: int = 256

    # State (only the actor task should mutate these)
    state: CallState = CallState.CONNECTING
    turn_generation: int = 0
    speech_generation: int = 0
    _sequence: int = 0
    ledger: PlaybackLedger = field(default_factory=PlaybackLedger)

    # Handler registry — populated by wire_handlers() at construction
    # by the transport layer (see apps/api/app/routes/twilio.py).
    handlers: dict[tuple[EventSource, str], EventHandler] = field(default_factory=dict)

    # Fallback handler when no (source, kind) match — logs by default.
    default_handler: Optional[EventHandler] = None

    # Internal
    _mailbox: asyncio.Queue = field(init=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False)
    _stopping: asyncio.Event = field(init=False)
    _current_turn_task: Optional[asyncio.Task] = field(default=None, init=False)
    _current_speech_task: Optional[asyncio.Task] = field(default=None, init=False)
    # Sprint 12 Track A: generation-scoped supervised tasks.
    # Maps generation → set of tasks spawned during that generation.
    # bump_turn cancels every entry with generation < new generation.
    _supervised_by_gen: dict[int, set[asyncio.Task]] = field(
        default_factory=dict, init=False,
    )

    def __post_init__(self) -> None:
        self._mailbox = asyncio.Queue(maxsize=self.mailbox_size)
        self._stopping = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spin up the actor coroutine.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name=f"call-actor-{self.call_id}",
        )
        log.info("CallActor started call_id=%s tenant=%s", self.call_id, self.tenant_id)

    async def stop(self, reason: str = "hangup") -> None:
        """Signal the actor to drain + shut down.  Cancels any in-flight
        turn / speech tasks + any remaining supervised jobs."""
        self._stopping.set()
        await self._cancel_current_turn(reason=f"actor-stop:{reason}")
        # Sprint 12 Track A: cancel every remaining supervised task
        for bucket in list(self._supervised_by_gen.values()):
            for task in list(bucket):
                if not task.done():
                    task.cancel()
        self._supervised_by_gen.clear()
        # Send a poison event so the mailbox loop exits promptly
        try:
            self._mailbox.put_nowait(_POISON_EVENT)
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self.state = CallState.ENDED
        log.info("CallActor stopped call_id=%s reason=%s", self.call_id, reason)

    # ── event ingest ─────────────────────────────────────────────────

    async def emit(self, event: CallEvent) -> None:
        """Enqueue an event from any task.  Non-blocking except when the
        mailbox is full (which is intentional backpressure)."""
        if self._stopping.is_set():
            log.debug("dropping event: actor stopping call_id=%s kind=%s",
                      self.call_id, event.kind)
            return
        # Stamp sequence — actor is authority on ordering
        self._sequence += 1
        stamped = CallEvent(
            call_id=event.call_id or self.call_id,
            tenant_id=event.tenant_id or self.tenant_id,
            sequence=self._sequence,
            monotonic_ns=event.monotonic_ns,
            source=event.source,
            turn_generation=event.turn_generation,
            speech_generation=event.speech_generation,
            kind=event.kind,
            payload=event.payload,
        )
        await self._mailbox.put(stamped)

    def emit_nowait(self, event: CallEvent) -> None:
        """Non-async version for use inside synchronous callbacks
        (Twilio protocol parsers, provider SDK callbacks)."""
        if self._stopping.is_set():
            return
        self._sequence += 1
        stamped = CallEvent(
            call_id=event.call_id or self.call_id,
            tenant_id=event.tenant_id or self.tenant_id,
            sequence=self._sequence,
            monotonic_ns=event.monotonic_ns,
            source=event.source,
            turn_generation=event.turn_generation,
            speech_generation=event.speech_generation,
            kind=event.kind,
            payload=event.payload,
        )
        try:
            self._mailbox.put_nowait(stamped)
        except asyncio.QueueFull:
            log.error("CallActor mailbox full call_id=%s — dropping %s",
                      self.call_id, event.kind)

    # ── generation control (turn / speech) ───────────────────────────

    async def bump_turn(self, reason: str = "new-utterance") -> int:
        """Start a new caller turn.  Cancels the current turn task, all
        supervised tasks from prior generations, and the current speech.
        Advances turn_generation + speech_generation.

        Sprint 12 Track A: no longer drains the mailbox.  Late events
        get dropped via source_epoch check in _run — event whose
        capture-time epoch < current generation is stale by definition."""
        await self._cancel_current_turn(reason=f"bump-turn:{reason}")
        self.turn_generation += 1
        self.speech_generation += 1  # any queued speech is invalidated too
        self._cancel_supervised_below(self.turn_generation)
        log.debug("call_id=%s turn_generation=%d speech_generation=%d",
                  self.call_id, self.turn_generation, self.speech_generation)
        return self.turn_generation

    async def bump_speech(self, reason: str = "new-response") -> int:
        """Start a new agent speech generation.  Cancels current speech
        task.  Sprint 12 Track A: no mailbox drain."""
        await self._cancel_current_speech(reason=f"bump-speech:{reason}")
        self.speech_generation += 1
        return self.speech_generation

    def spawn_supervised(
        self,
        coro,
        *,
        generation: int,
        name: str,
    ) -> asyncio.Task:
        """Run `coro` off the mailbox loop.  Task tagged with `generation`;
        bump_turn cancels every task tagged with an older generation.

        Use this instead of asyncio.create_task from inside mailbox
        handlers.  Handlers must NEVER await these tasks — they should
        return True immediately after spawning."""
        task = asyncio.create_task(coro, name=name)
        self._supervised_by_gen.setdefault(generation, set()).add(task)
        # Self-cleanup so the bucket doesn't grow across a long call.
        def _on_done(t: asyncio.Task) -> None:
            bucket = self._supervised_by_gen.get(generation)
            if bucket is not None:
                bucket.discard(t)
                if not bucket:
                    self._supervised_by_gen.pop(generation, None)
        task.add_done_callback(_on_done)
        return task

    def emit_local(self, event: CallEvent) -> None:
        """Synchronous emit for use from inside a spawned supervised job.
        Delegates to emit_nowait — same semantics, clearer intent at
        the call site."""
        self.emit_nowait(event)

    def _cancel_supervised_below(self, current_generation: int) -> None:
        """Cancel every supervised task tagged with a generation <
        current_generation.  Tasks self-clean via add_done_callback."""
        stale_gens = [g for g in self._supervised_by_gen
                      if g < current_generation]
        for g in stale_gens:
            for task in list(self._supervised_by_gen.get(g, ())):
                if not task.done():
                    task.cancel()

    def register_turn_task(self, task: asyncio.Task) -> None:
        """Track the current turn's LLM/tool coroutine so bump_turn can
        cancel it cleanly."""
        self._current_turn_task = task

    def register_speech_task(self, task: asyncio.Task) -> None:
        self._current_speech_task = task

    async def _cancel_current_turn(self, reason: str) -> None:
        if self._current_turn_task and not self._current_turn_task.done():
            log.debug("cancelling current turn task: %s", reason)
            self._current_turn_task.cancel()
            try:
                await self._current_turn_task
            except (asyncio.CancelledError, Exception):
                pass
        self._current_turn_task = None
        await self._cancel_current_speech(reason=reason)

    async def _cancel_current_speech(self, reason: str) -> None:
        if self._current_speech_task and not self._current_speech_task.done():
            log.debug("cancelling current speech task: %s", reason)
            self._current_speech_task.cancel()
            try:
                await self._current_speech_task
            except (asyncio.CancelledError, Exception):
                pass
        self._current_speech_task = None
        # Mark current speech generation cleared in the ledger so any
        # late mark-acks are discarded rather than committed to history.
        self.ledger.clear_current_generation(self.speech_generation)

    # ── state transition guard ──────────────────────────────────────

    def transition(self, new: CallState) -> None:
        """Advance the state machine.  Not strictly gated — the actor
        is responsible for calling this from valid states — but we log
        the transition for observability."""
        if new == self.state:
            return
        log.info("call_id=%s state %s -> %s", self.call_id, self.state.value, new.value)
        self.state = new

    # ── run loop ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        """The one coroutine that consumes the mailbox.  Everything
        else in the call feeds events into it via emit()."""
        try:
            while not self._stopping.is_set():
                event = await self._mailbox.get()
                if event is _POISON_EVENT:
                    return

                # Generation guard: drop events from cancelled turns / speeches.
                # This is where cancellation "sticks" — a provider that can't
                # actually stop mid-stream still sees its output discarded here.
                #
                # Sprint 12 Track A: source_epoch > 0 means the caller stamped
                # the capture-time generation; if that's older than current,
                # drop regardless of turn_generation.  source_epoch = 0 means
                # untracked, skip the epoch check (all existing callers).
                if event.turn_generation < self.turn_generation or (
                    event.source_epoch > 0
                    and event.source_epoch < self.turn_generation
                ):
                    log.debug(
                        "dropping stale event: source=%s kind=%s turn=%d epoch=%d (current=%d)",
                        event.source.value, event.kind,
                        event.turn_generation, event.source_epoch,
                        self.turn_generation,
                    )
                    continue
                # For TTS/PLAYBACK events, also gate by speech_generation
                if event.source in (EventSource.TTS, EventSource.PLAYBACK):
                    if event.speech_generation < self.speech_generation:
                        log.debug(
                            "dropping stale speech event: kind=%s gen=%d (current=%d)",
                            event.kind, event.speech_generation, self.speech_generation,
                        )
                        continue

                # Dispatch to handler
                key = (event.source, event.kind)
                handler = self.handlers.get(key) or self.default_handler
                if handler is None:
                    log.debug("no handler for %s.%s", event.source.value, event.kind)
                    continue
                try:
                    await handler(self, event)
                except Exception as e:
                    log.exception("handler failed for %s.%s: %s",
                                  event.source.value, event.kind, e)
        finally:
            self._stopping.set()


# Sentinel used to unblock the mailbox in stop().  Type is CallEvent
# subclass so type-checkers don't complain, but the actor short-circuits
# on identity check before any field access.
_POISON_EVENT = CallEvent(
    call_id="", tenant_id="", sequence=-1, monotonic_ns=0,
    source=EventSource.CONTROL, turn_generation=0, speech_generation=0,
    kind="__poison__", payload=None,
)


# ── registry ────────────────────────────────────────────────────────

class CallActorRegistry:
    """(call_id, tenant_id) → CallActor lookup.

    Process-local for now.  Sprint 8+ replaces with Redis-backed
    lease/heartbeat semantics so multi-worker deploys can hand off calls.
    """

    def __init__(self) -> None:
        self._actors: dict[tuple[str, str], CallActor] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        call_id: str,
        tenant_id: str,
        setup: Optional[Callable[[CallActor], None]] = None,
    ) -> CallActor:
        """Return the actor for this call, creating + starting one if
        needed.  Optional `setup` callback is invoked ONCE at creation
        to register handlers before the run loop starts consuming."""
        key = (call_id, tenant_id)
        async with self._lock:
            existing = self._actors.get(key)
            if existing is not None:
                return existing
            actor = CallActor(call_id=call_id, tenant_id=tenant_id)
            if setup is not None:
                setup(actor)
            await actor.start()
            self._actors[key] = actor
            return actor

    def get(self, call_id: str, tenant_id: str) -> Optional[CallActor]:
        return self._actors.get((call_id, tenant_id))

    async def stop(self, call_id: str, tenant_id: str, reason: str = "hangup") -> None:
        async with self._lock:
            actor = self._actors.pop((call_id, tenant_id), None)
        if actor is not None:
            await actor.stop(reason=reason)

    async def stop_all(self) -> None:
        async with self._lock:
            actors = list(self._actors.values())
            self._actors.clear()
        for a in actors:
            try:
                await a.stop(reason="registry-shutdown")
            except Exception:
                pass


# Module-level registry singleton — the Twilio route / test harness
# both use the same one so lookups work across module boundaries.
_registry_singleton: Optional[CallActorRegistry] = None


def get_registry() -> CallActorRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = CallActorRegistry()
    return _registry_singleton
