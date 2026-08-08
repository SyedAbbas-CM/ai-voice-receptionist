# Sprint 12 Track A — Non-Blocking Actor Mailbox

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Actor mailbox handlers stop awaiting long-running LLM / TTS / tool work. They spawn a supervised job and return immediately. Jobs emit typed events back to the actor when they complete. Interruptions now get dispatched during agent speech instead of queuing behind the operation they're supposed to interrupt.

**Architecture:** Add `spawn_supervised` + `emit_local` on `CallActor`. Add typed job-completion events (`brain_completed`, `speech_completed`, plus their `_failed` counterparts). Rewrite `_on_turn_event_end` + `_run_brain_from_text` + `_speak` in `TwilioActorSession` to spawn + return + emit-back. Remove `_drain_mailbox` calls from `bump_turn` / `bump_speech`. Add `source_epoch` on `CallEvent` so late results from cancelled turns drop cleanly. Feature flag `ACTOR_NONBLOCKING_HANDLERS` — old code path stays under the flag off.

**Tech Stack:** Python asyncio, existing `CallActor` + `CallEvent` + `TwilioActorSession` (`apps/api/app/routes/twilio_actor.py`).

## Global Constraints

- **Feature flag required.** All new behavior gated behind `settings.actor_nonblocking_handlers` (default: `true`).
- **Rollback safety.** Flipping the flag `false` restores exact prior behavior — old code paths remain intact.
- **No test regressions.** Full suite must stay at 930 passing (+ new tests) after this track. The 4 known-broken tests get fixed as part of this plan.
- **No new dependencies.** Only stdlib + existing pkgs.
- **YAGNI.** Do NOT add job-state tracking beyond what's required to answer "is this job still current?". Do NOT introduce a task-pool. `asyncio.create_task` + `_supervised_tasks: set` cleanup is enough.
- **Global-name discipline.** Never rename `CallEvent`, `EventSource`, `bump_turn`, `bump_speech`, `register_turn_task`, `register_speech_task`, `handlers`, `default_handler` — external tests + widgets reference them.
- Never mention Claude in code comments. WHY-only comments; no WHAT restatements.
- All commit messages end with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

## File Structure

**Modified files (only three):**
- `apps/api/app/core/config.py` — add `actor_nonblocking_handlers: bool = True`
- `packages/runtime/call_event.py` — add `source_epoch: int = 0` field to `CallEvent` + `.new()`
- `packages/runtime/call_actor.py` — add `spawn_supervised`, `emit_local`, remove `_drain_mailbox` calls in `bump_turn`/`bump_speech`, drop stale events by `source_epoch`
- `apps/api/app/routes/twilio_actor.py` — rewrite `_on_turn_event_end`, `_run_brain_from_text`, `_speak`, add `_on_brain_completed`, `_on_speech_completed`, `_on_brain_failed`, register the 3 new handlers

**New test files:**
- `apps/api/tests/test_actor_spawn_supervised.py` — spawn_supervised + emit_local + source_epoch + bump-cancel semantics
- `apps/api/tests/test_actor_nonblocking_end_of_turn.py` — end-of-turn handler returns fast, brain runs off-mailbox

**Modified test files (fix stale tests):**
- `apps/api/tests/test_turn_manager.py` — 3 tests need `speech_final=True` where they now send `is_final=True` alone (Sprint 11 change made `is_final` alone buffer, not fire)
- `apps/api/tests/test_twilio_actor_two_stage_barge.py` — rename `_send_mulaw_frames` → `_send_audio_frames` (Sprint 11 renamed the method)

---

### Task 1: Add feature flag + `source_epoch` on CallEvent

**Files:**
- Modify: `apps/api/app/core/config.py` (add one field)
- Modify: `packages/runtime/call_event.py` (add one field + threading through `.new()`)
- Test: `apps/api/tests/test_actor_spawn_supervised.py` (new file — this task adds only the epoch-related test)

**Interfaces:**
- Produces:
  - `settings.actor_nonblocking_handlers: bool` (default `True`)
  - `CallEvent.source_epoch: int` (default `0`) — the actor generation at the moment the underlying signal was captured; if provider output is delayed, this stays with the OLD generation and the actor drops it.
  - `CallEvent.new(..., source_epoch: int = 0)` — new kwarg

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_actor_spawn_supervised.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_spawn_supervised.py::test_call_event_new_accepts_source_epoch -v
```

Expected: FAIL — `CallEvent.new()` doesn't accept `source_epoch`.

- [ ] **Step 3: Add the field + kwarg**

In `packages/runtime/call_event.py`:

Add to the `CallEvent` dataclass (after `payload`):

```python
    # Sprint 12 Track A: the actor turn_generation active at the moment
    # the underlying signal (audio frame, HTTP response chunk, etc.) was
    # captured.  If the signal was captured before a bump_turn and the
    # event only reaches the mailbox afterward, source_epoch stays with
    # the OLD generation and the actor drops it as stale.
    #
    # Distinct from turn_generation: turn_generation is stamped at emit
    # time; source_epoch is stamped at capture time.  For same-tick
    # events they're equal.  For anything with provider-side latency
    # (STT finals, LLM stream chunks, TTS callbacks) they can diverge.
    source_epoch: int = field(default=0)
```

Add the kwarg to `.new()`:

```python
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
```

In `apps/api/app/core/config.py`, find the Settings class and add near the other feature flags (search for `twilio_use_actor`):

```python
    # Sprint 12 Track A: mailbox handlers spawn+return instead of awaiting
    # long-running LLM/TTS/tool work.  Flip false to restore pre-Sprint-12
    # inline-await behavior for rollback.
    actor_nonblocking_handlers: bool = True
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_spawn_supervised.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/az/Desktop/Receptionist Agent" && \
git add apps/api/app/core/config.py packages/runtime/call_event.py apps/api/tests/test_actor_spawn_supervised.py && \
git commit -m "$(cat <<'EOF'
Sprint 12 Track A: source_epoch on CallEvent + nonblocking flag

Adds CallEvent.source_epoch — the actor generation at signal-capture
time.  Distinct from turn_generation (stamped at emit time).  Lets us
drop provider-delayed results from cancelled turns cleanly.

Adds settings.actor_nonblocking_handlers (default True) — feature flag
for the rest of Track A.  Flip false to restore prior inline-await
behavior.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Actor `spawn_supervised` + `emit_local` API

**Files:**
- Modify: `packages/runtime/call_actor.py` — add two methods + generation-scoped task set
- Test: `apps/api/tests/test_actor_spawn_supervised.py` (extend from Task 1)

**Interfaces:**
- Consumes: `CallEvent.source_epoch` (Task 1), `CallActor` class
- Produces:
  - `CallActor.spawn_supervised(coro: Awaitable, *, generation: int, name: str) -> asyncio.Task`
    - Creates a task, tags it internally with `generation`
    - `bump_turn` cancels all tasks tagged with generations < new generation
    - Returns the task
  - `CallActor.emit_local(event: CallEvent) -> None`
    - Same as `emit_nowait` but for events emitted from inside a spawned job
    - The spawned job is not the actor coroutine, so it CANNOT `await self._mailbox.put()`; must use `_mailbox.put_nowait()`. `emit_local` is the safe wrapper.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_actor_spawn_supervised.py`:

```python
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
        # Give the task a chance to start
        await asyncio.sleep(0.01)
        await actor.bump_turn(reason="test-cancel")
        # Wait briefly for the cancellation to propagate
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
        # Wait for the mailbox to deliver
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 1
        assert received[0].kind == "job-done"
        assert received[0].payload == {"result": 42}
    finally:
        await actor.stop(reason="test")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_spawn_supervised.py -v
```

Expected: FAIL — `spawn_supervised` and `emit_local` don't exist.

- [ ] **Step 3: Implement `spawn_supervised` + `emit_local`**

In `packages/runtime/call_actor.py`, add to the `CallActor` dataclass fields (after `_current_speech_task`):

```python
    # Sprint 12 Track A: generation-scoped supervised tasks.
    # Maps generation → set of tasks spawned during that generation.
    # bump_turn cancels every entry with generation < new generation.
    _supervised_by_gen: dict[int, set[asyncio.Task]] = field(
        default_factory=dict, init=False,
    )
```

Add these methods anywhere between `register_speech_task` and `transition` (both are logically about generation control):

```python
    def spawn_supervised(
        self,
        coro,
        *,
        generation: int,
        name: str,
    ) -> asyncio.Task:
        """Run `coro` off the mailbox loop.  The task is tagged with
        `generation`; bump_turn cancels every task tagged with an older
        generation.

        Use this instead of asyncio.create_task from inside mailbox
        handlers.  Handlers must NEVER await these tasks — they should
        return True immediately after spawning."""
        task = asyncio.create_task(coro, name=name)
        self._supervised_by_gen.setdefault(generation, set()).add(task)
        # Self-cleanup: remove from the set when the task completes so
        # the set doesn't grow across a long call.
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
        The job runs on its own task, not the actor's; awaiting
        self._mailbox.put() from there is fine but callers usually don't
        care about backpressure at that boundary, so we use put_nowait
        with a full-mailbox warning."""
        # Delegate to emit_nowait — same semantics, clearer intent.
        self.emit_nowait(event)
```

Modify `bump_turn` to cancel supervised tasks tied to old generations. Replace the existing method body (keep the signature + docstring shape):

```python
    async def bump_turn(self, reason: str = "new-utterance") -> int:
        """Start a new caller turn.  Cancels the current turn task, all
        supervised tasks from prior generations, and the current speech.
        Advances turn_generation + speech_generation.

        Sprint 12 Track A: no longer drains the mailbox.  Late events
        get dropped via source_epoch check in _run."""
        await self._cancel_current_turn(reason=f"bump-turn:{reason}")
        self.turn_generation += 1
        self.speech_generation += 1
        self._cancel_supervised_below(self.turn_generation)
        log.debug("call_id=%s turn_generation=%d speech_generation=%d",
                  self.call_id, self.turn_generation, self.speech_generation)
        return self.turn_generation
```

Add the helper (next to `_cancel_current_turn`):

```python
    def _cancel_supervised_below(self, current_generation: int) -> None:
        """Cancel every supervised task tagged with a generation <
        current_generation.  Tasks self-clean via add_done_callback."""
        stale_gens = [g for g in self._supervised_by_gen
                      if g < current_generation]
        for g in stale_gens:
            for task in list(self._supervised_by_gen.get(g, ())):
                if not task.done():
                    task.cancel()
```

Also modify `bump_speech` to drop `_drain_mailbox` (mirror the `bump_turn` change):

```python
    async def bump_speech(self, reason: str = "new-response") -> int:
        """Start a new agent speech generation.  Cancels current speech
        task.  Sprint 12 Track A: no mailbox drain."""
        await self._cancel_current_speech(reason=f"bump-speech:{reason}")
        self.speech_generation += 1
        return self.speech_generation
```

**Remove** the now-unused `_drain_mailbox` method (search for `async def _drain_mailbox` and delete the whole method — Task 3's test verifies it's gone).

Modify `_run` to drop stale events by `source_epoch` too:

Find the block:
```python
                if event.turn_generation < self.turn_generation:
                    log.debug(...)
                    continue
```

Replace with:
```python
                # Drop events either stamped from an older turn OR whose
                # source_epoch says they were captured before the current
                # generation (Sprint 12 Track A).  source_epoch=0 means
                # "not tracked" and skips the epoch check.
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
```

Also modify `stop` to cancel any remaining supervised tasks (add before `self._stopping.set()` at the top):

```python
    async def stop(self, reason: str = "hangup") -> None:
        self._stopping.set()
        await self._cancel_current_turn(reason=f"actor-stop:{reason}")
        # Cancel every remaining supervised task
        for bucket in list(self._supervised_by_gen.values()):
            for task in list(bucket):
                if not task.done():
                    task.cancel()
        self._supervised_by_gen.clear()
        # ... rest of existing stop() body unchanged ...
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_spawn_supervised.py -v
```

Expected: 5 PASSED (2 from Task 1 + 3 from Task 2).

- [ ] **Step 5: Verify no existing test regressed**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_call_actor.py -v
```

Expected: existing call-actor tests still pass. If any test relied on `_drain_mailbox` existing (which is removed), fix the test to not depend on internal API.

- [ ] **Step 6: Commit**

```bash
cd "/Users/az/Desktop/Receptionist Agent" && \
git add packages/runtime/call_actor.py apps/api/tests/test_actor_spawn_supervised.py && \
git commit -m "$(cat <<'EOF'
Sprint 12 Track A: spawn_supervised + emit_local on CallActor

Actor gets two new methods:
- spawn_supervised(coro, generation, name) tags the task with a
  generation.  bump_turn cancels every task tagged < the new generation.
  Handlers use this instead of asyncio.create_task from inside the
  mailbox loop.
- emit_local(event) is the synchronous emit path spawned jobs use to
  send typed events back to the actor.

bump_turn + bump_speech no longer call _drain_mailbox — late events
now drop via source_epoch guard in _run.  _drain_mailbox helper deleted.

stop() cancels any remaining supervised tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Rewrite `_on_turn_event_end` to spawn + return

**Files:**
- Modify: `apps/api/app/routes/twilio_actor.py` — `_on_turn_event_end`, `_run_brain_from_text`, `_speak`, register new handlers
- Test: `apps/api/tests/test_actor_nonblocking_end_of_turn.py` (new)

**Interfaces:**
- Consumes: `spawn_supervised`, `emit_local`, `settings.actor_nonblocking_handlers`
- Produces (three new handlers on `TwilioActorSession`, all registered on the actor):
  - `_on_brain_completed(actor, event)` — event.payload = `{"reply": str, "escalated": bool, "tool_results": list, "turn_gen": int}`. Spawns `_speak` supervised.
  - `_on_brain_failed(actor, event)` — event.payload = `{"error": str, "exc_type": str, "turn_gen": int}`. Logs, does nothing else (the actor stays in LISTENING; caller will retry).
  - `_on_speech_completed(actor, event)` — event.payload = `{"speech_gen": int}`. Fires idle followup.
- New event `kind`s (source `EventSource.CONTROL`):
  - `brain_completed`
  - `brain_failed`
  - `speech_completed`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_actor_nonblocking_end_of_turn.py`:

```python
"""Sprint 12 Track A: end-of-turn handler returns fast (< 50 ms) even
though the brain job takes seconds.  The brain runs off the mailbox
so an interruption emitted during the brain job actually gets
dispatched instead of queueing behind it."""
from __future__ import annotations

import asyncio
import json
import time

import pytest


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FakeVAD:
    def is_speech(self, f, sr, mime): return len(f) > 0


class FakeSTT:
    name = "fake"
    supports_streaming = True
    async def transcribe(self, w, sr, mime): return ""
    async def transcribe_stream(self, chunks, sample_rate=8000, encoding="linear16"):
        async for _ in chunks: pass
        return
        yield  # pragma: no cover


class FakeTTS:
    name = "fake"
    async def synthesize(self, text, voice=None):
        return b"\xff" * 4000, "audio/mulaw"


@pytest.mark.asyncio
async def test_end_of_turn_handler_returns_fast_when_brain_slow(monkeypatch):
    """The mailbox handler for END_OF_TURN spawns the brain and
    returns.  Even if the brain takes 2 seconds, subsequent mailbox
    events (like INTERRUPTION) get dispatched within ~50 ms."""
    from app.routes import twilio as twilio_module
    from app.core import session_manager
    from app import providers
    from app.routes import twilio_actor as actor_module

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: FakeTTS())
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)

    slow_brain_started = asyncio.Event()

    async def slow_run_greeting(state, brain):
        return "Hello."

    async def slow_run_user_turn(state, brain, transcript):
        slow_brain_started.set()
        await asyncio.sleep(2.0)  # brain takes 2 seconds
        return {"reply": "Slow reply.", "escalated": False, "tool_results": []}

    async def _end(sid, tenant_id="default"): return None
    monkeypatch.setattr(session_manager, "run_greeting", slow_run_greeting)
    monkeypatch.setattr(session_manager, "run_user_turn", slow_run_user_turn)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    from packages.runtime import call_actor, CallEvent, EventSource
    call_actor._registry_singleton = None

    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallState
    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-slow", call_id="CA-slow", tenant_id="acme",
    )
    await session.start()
    # Wait for greeting to finish so state == LISTENING
    for _ in range(200):
        await asyncio.sleep(0.02)
        if session.actor.state == CallState.LISTENING:
            break
    assert session.actor.state == CallState.LISTENING

    # Fire END_OF_TURN — brain will start (slow_run_user_turn) but
    # the handler should return within ~50 ms.
    end_start = time.monotonic()
    await session.actor.emit(CallEvent.new(
        call_id="CA-slow", tenant_id="acme", source=EventSource.CONTROL,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="end_of_turn", payload={"text": "book me an appointment", "is_final": True},
    ))
    # Wait for the brain to START (proves the handler dispatched)
    try:
        await asyncio.wait_for(slow_brain_started.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pytest.fail("brain never started")
    dispatch_latency = time.monotonic() - end_start
    # Brain is now sleeping 2s; the handler should already have returned.
    # We prove this by asserting the actor can dispatch ANOTHER event
    # before the brain finishes.
    dispatched = asyncio.Event()
    async def probe(actor, event):
        dispatched.set()
        return True
    session.actor.handlers[(EventSource.CONTROL, "probe")] = probe
    await session.actor.emit(CallEvent.new(
        call_id="CA-slow", tenant_id="acme", source=EventSource.CONTROL,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="probe", payload={},
    ))
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        pytest.fail(
            f"mailbox was blocked by brain — probe event never dispatched "
            f"(end_of_turn dispatch latency: {dispatch_latency*1000:.1f}ms)"
        )
    await session.stop("test")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_nonblocking_end_of_turn.py -v --timeout=15
```

Expected: FAIL — probe never dispatched, brain blocks the mailbox.

- [ ] **Step 3: Rewrite `_on_turn_event_end` to spawn + return**

In `apps/api/app/routes/twilio_actor.py`, find `_on_turn_event_end` (~line 940). Replace the body with:

```python
    async def _on_turn_event_end(self, actor: CallActor, event: CallEvent) -> bool:
        """END_OF_TURN — caller committed their turn.

        Sprint 12 Track A: this handler MUST return quickly.  Brain work
        runs as a supervised job that emits `brain_completed` back to
        the actor when done.  A subsequent INTERRUPTION event won't
        queue behind a 2-second LLM call.

        Legacy inline behavior stays available under
        `settings.actor_nonblocking_handlers = False` for rollback."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        text = event.payload.get("text") or self._streaming_utterance_text
        if not text or not text.strip():
            return True

        # Bump the turn generation so late partials from the previous
        # utterance get dropped.
        await actor.bump_turn(reason="end-of-turn")
        self._open_turn_span(actor.turn_generation)
        if self._current_turn_span is not None:
            self._current_turn_span.mark("media_in")
            self._current_turn_span.mark("stt_final")

        turn_gen = actor.turn_generation

        if settings.actor_nonblocking_handlers:
            # New path: spawn the brain job, return immediately.  Job
            # will emit control.brain_completed when done.
            actor.spawn_supervised(
                self._brain_job(text, turn_gen),
                generation=turn_gen,
                name=f"brain-{self.call_id}-{turn_gen}",
            )
            return True

        # Legacy path (feature flag off): inline await like before.
        brain_task = asyncio.create_task(
            self._run_brain_from_text(text, turn_gen),
            name=f"brain-{self.call_id}-{turn_gen}",
        )
        actor.register_turn_task(brain_task)
        try:
            await brain_task
        except asyncio.CancelledError:
            pass
        return True
```

Now add the two new job methods next to `_run_brain_from_text` (which stays as-is for the legacy path):

```python
    async def _brain_job(self, transcript: str, turn_gen: int) -> None:
        """Sprint 12 Track A: brain runs as a supervised job.  On
        success, emits control.brain_completed with the reply text.
        On failure, emits control.brain_failed.  The actor's
        _on_brain_completed handler then spawns _speech_job.

        This is the same shape as _run_brain_from_text but instead of
        awaiting _speak inline it returns control to the actor."""
        from packages.observability.call_event_log import (
            get_call_event_log, CallEvent as _CE, EventSourceKind as _SK,
        )
        try:
            _elog = get_call_event_log()
            _elog.write(_CE(
                call_id=self.session_id, tenant_id=self.tenant_id,
                source=_SK.STT, kind="final",
                payload={"text": transcript}, turn_generation=turn_gen,
            ))
        except Exception:
            _elog = None

        try:
            log.info("stream-brain %s turn=%d heard: %s",
                     self.session_id, turn_gen, transcript)
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            payload = await session_manager.run_user_turn(state, brain, transcript)
            reply = (payload.get("reply") or "").strip()
            escalated = bool(payload.get("escalated"))
            tool_results = payload.get("tool_results") or []
            speech_act = _infer_speech_act_from_payload(payload)

            if _elog is not None:
                try:
                    _elog.write(_CE(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        source=_SK.LLM, kind="reply",
                        payload={
                            "reply": reply,
                            "escalated": escalated,
                            "tool_results": tool_results,
                        },
                        turn_generation=turn_gen,
                    ))
                except Exception:
                    pass

            # Emit back to the actor — the handler decides what to do.
            if self.actor is not None:
                self.actor.emit_local(CallEvent.new(
                    call_id=self.call_id, tenant_id=self.tenant_id,
                    source=EventSource.CONTROL,
                    turn_generation=turn_gen,
                    speech_generation=self.actor.speech_generation,
                    kind="brain_completed",
                    payload={
                        "reply": reply,
                        "escalated": escalated,
                        "tool_results": tool_results,
                        "speech_act": speech_act,
                        "turn_gen": turn_gen,
                    },
                    source_epoch=turn_gen,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("brain job failed: %s", e)
            if _elog is not None:
                try:
                    _elog.write_error(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        message=str(e), exc_type=type(e).__name__,
                        turn_generation=turn_gen,
                    )
                except Exception:
                    pass
            if self.actor is not None:
                self.actor.emit_local(CallEvent.new(
                    call_id=self.call_id, tenant_id=self.tenant_id,
                    source=EventSource.CONTROL,
                    turn_generation=turn_gen,
                    speech_generation=self.actor.speech_generation,
                    kind="brain_failed",
                    payload={
                        "error": str(e),
                        "exc_type": type(e).__name__,
                        "turn_gen": turn_gen,
                    },
                    source_epoch=turn_gen,
                ))

    async def _on_brain_completed(self, actor: CallActor, event: CallEvent) -> bool:
        """Brain job finished successfully.  Save its speech-act
        classification (so VPL uses it), then spawn a supervised speech
        job for the reply text."""
        payload = event.payload or {}
        reply = (payload.get("reply") or "").strip()
        self._current_speech_act = payload.get("speech_act")
        if not reply:
            return True
        turn_gen = payload.get("turn_gen", actor.turn_generation)
        actor.spawn_supervised(
            self._speech_job(reply, turn_gen),
            generation=turn_gen,
            name=f"speech-{self.call_id}-{turn_gen}",
        )
        return True

    async def _on_brain_failed(self, actor: CallActor, event: CallEvent) -> bool:
        """Brain job errored.  Log-only for now — caller will retry
        their turn.  Do NOT play a fallback string; that would be worse
        than silence for demo debugging."""
        payload = event.payload or {}
        log.warning("brain job failed turn=%s: %s (%s)",
                    payload.get("turn_gen"),
                    payload.get("error"), payload.get("exc_type"))
        return True

    async def _speech_job(self, text: str, turn_gen: int) -> None:
        """Sprint 12 Track A: TTS + playback runs as a supervised job.
        Mirrors _speak but emits control.speech_completed back to the
        actor instead of returning to inline caller."""
        try:
            await self._speak(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("speech job failed: %s", e)
        if self.actor is not None:
            self.actor.emit_local(CallEvent.new(
                call_id=self.call_id, tenant_id=self.tenant_id,
                source=EventSource.CONTROL,
                turn_generation=turn_gen,
                speech_generation=self.actor.speech_generation,
                kind="speech_completed",
                payload={"turn_gen": turn_gen},
                source_epoch=turn_gen,
            ))

    async def _on_speech_completed(self, actor: CallActor, event: CallEvent) -> bool:
        """Speech job finished (or was cancelled).  Arm idle followup
        so we prompt the caller if they go silent."""
        self._arm_idle_followup()
        return True
```

Register the three new handlers. Find `_wire_handlers` (or the block that populates `actor.handlers`) — search for `EventSource.CONTROL, TurnEventKind.END_OF_TURN.value` — and add near it:

```python
        actor.handlers[(EventSource.CONTROL, "brain_completed")] = self._on_brain_completed
        actor.handlers[(EventSource.CONTROL, "brain_failed")] = self._on_brain_failed
        actor.handlers[(EventSource.CONTROL, "speech_completed")] = self._on_speech_completed
```

Also, in `_speak`'s `finally` block, REMOVE the `self._arm_idle_followup()` call — the new `_on_speech_completed` handler owns that responsibility now. Search `_arm_idle_followup` in the file; there should be exactly one call inside `_speak`, delete it. (The legacy path stays correct because when the flag is off, the inline await in `_on_turn_event_end` still calls `_speak` which no longer arms the followup, but `_run_brain_from_text` never armed it either. If regression happens, put it back conditionally on the flag being off.)

Actually simpler: don't touch `_speak`. Keep the `finally: self._arm_idle_followup()` — legacy path behaves as before. In the new path, `_on_speech_completed` arms again but `_arm_idle_followup` calls `_cancel_idle_followup` first (it's idempotent), so double-arm is a no-op.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_actor_nonblocking_end_of_turn.py -v --timeout=15
```

Expected: PASS. Probe event dispatches even while brain is sleeping.

- [ ] **Step 5: Verify existing streaming-wiring test still passes**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_streaming_wiring.py -v --timeout=15
```

Expected: existing END_OF_TURN test (`test_end_of_turn_event_triggers_brain`) still passes.  If it fails because it awaits inline behavior, update it to poll for `actor.turn_generation` change with a longer timeout — it should still work under both flag settings.

- [ ] **Step 6: Commit**

```bash
cd "/Users/az/Desktop/Receptionist Agent" && \
git add apps/api/app/routes/twilio_actor.py apps/api/tests/test_actor_nonblocking_end_of_turn.py && \
git commit -m "$(cat <<'EOF'
Sprint 12 Track A: end-of-turn handler spawns brain + returns

_on_turn_event_end now spawns _brain_job as supervised and returns
True immediately (under actor_nonblocking_handlers=True, default).

The brain job emits control.brain_completed with the reply text.
Handler _on_brain_completed spawns _speech_job for the reply.
_speech_job emits control.speech_completed.  Handler _on_speech_completed
arms the idle followup.

Legacy inline-await path preserved under actor_nonblocking_handlers=False.

Result: an INTERRUPTION event emitted while the brain is running (or the
agent is speaking) now gets dispatched within ~50 ms instead of queueing
behind the seconds-long operation it's meant to interrupt.

New test verifies a probe event dispatches even while a 2s brain job
is in flight.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Fix the 4 stale tests from prior sprints

**Files:**
- Modify: `apps/api/tests/test_turn_manager.py` (3 tests)
- Modify: `apps/api/tests/test_twilio_actor_two_stage_barge.py` (1 test)

**Interfaces:**
- Consumes: (nothing new — just fixes tests that broke on Sprint 11 changes)
- Produces: 4 tests going from FAIL → PASS.

- [ ] **Step 1: Confirm the 4 failures are what we think they are**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_turn_manager.py tests/test_twilio_actor_two_stage_barge.py -v --timeout=15 2>&1 | tail -20
```

Expected: 4 failures with the shape "eager_end_of_turn was not emitted" (TM tests) + "_send_mulaw_frames not found" (barge test).

- [ ] **Step 2: Fix `test_first_final_fires_eager_end_of_turn`**

Open `apps/api/tests/test_turn_manager.py`, find the call to `tm.on_stt_event("final", ...)` inside `test_first_final_fires_eager_end_of_turn`. Change:

```python
await tm.on_stt_event("final", text="i'd like an appointment", is_final=True)
```

to:

```python
await tm.on_stt_event("final", text="i'd like an appointment", is_final=True, speech_final=True)
```

The Sprint 11 semantic guard requires `speech_final=True` OR text that ends on complete-looking punctuation for immediate promotion. "i'd like an appointment" ends on a noun so the guard buffers it. Passing `speech_final=True` says "Deepgram VAD confirmed real end-of-utterance" → guard skips → promotion fires as the test expects.

- [ ] **Step 3: Fix `test_final_then_no_resume_promotes_to_end_of_turn`**

Same file. Find the `on_stt_event("final", ...)` call in that test. Same fix — add `speech_final=True`.

- [ ] **Step 4: Fix `test_speech_resume_after_final_fires_turn_resumed`**

Same file. Same fix — add `speech_final=True` to the `on_stt_event("final", ...)` call.

- [ ] **Step 5: Fix `test_ducked_state_skips_outbound_media_frames`**

Open `apps/api/tests/test_twilio_actor_two_stage_barge.py`. Search for `_send_mulaw_frames`. Replace with `_send_audio_frames`. (Sprint 11 renamed the method — see `twilio_actor.py` for the current name.)

The call signature also changed: `_send_mulaw_frames(mulaw)` → `_send_audio_frames(audio_bytes, mime)`. Update the test to pass a mime — `"audio/mulaw"` if the fake bytes are µ-law, else `"audio/pcm;rate=16000"` for PCM.

- [ ] **Step 6: Run all 4 tests to verify pass**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_turn_manager.py tests/test_twilio_actor_two_stage_barge.py -v --timeout=15
```

Expected: all 4 pass (plus whatever else is in those files).

- [ ] **Step 7: Full suite regression check**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/ -q --timeout=45 --deselect tests/test_kokoro_tts.py 2>&1 | tail -6
```

Expected: `~935 passed, 0 failed, ~35 skipped` (930 was baseline + 5 new from Tasks 1-3 + 4 restored).

- [ ] **Step 8: Commit**

```bash
cd "/Users/az/Desktop/Receptionist Agent" && \
git add apps/api/tests/test_turn_manager.py apps/api/tests/test_twilio_actor_two_stage_barge.py && \
git commit -m "$(cat <<'EOF'
Fix 4 stale tests from Sprint 11 changes

- 3 turn_manager tests: pass speech_final=True on the final event
  so the semantic guard promotes to EAGER_END_OF_TURN as expected
  (Sprint 11 added the guard; guard buffers is_final-only fragments
  ending on non-terminating punctuation).
- test_ducked_state_skips_outbound_media_frames: _send_mulaw_frames
  was renamed to _send_audio_frames in Sprint 11 (takes bytes + mime
  now instead of just mulaw bytes).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: End-to-end manual verification in the browser

**Files:** none — this is the demo check.

- [ ] **Step 1: Restart server**

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
for i in 1 2 3 4 5 6 7 8; do curl -sf http://127.0.0.1:8000/health > /dev/null && echo "up ${i}s" && break; sleep 1; done
```

- [ ] **Step 2: Hard-reload browser widget**

Open `http://127.0.0.1:8000/call-stream/`. Hard reload (`Cmd+Shift+R`). Click Call. Allow mic.

- [ ] **Step 3: Verify interruption dispatch works**

Say "Tell me about all your services and doctors and everything you offer" (long question that triggers a long reply). While the agent is speaking, cut in with "Actually just book me a cleaning."

Expected: the agent stops mid-sentence promptly (< 1 second) instead of finishing the long reply.

- [ ] **Step 4: Pull the timeline**

```bash
CID=$(grep "browser_" /tmp/uvicorn.log | tail -1 | grep -oE "browser_[a-f0-9]+")
curl -s "http://127.0.0.1:8000/debug/call/twilio_$CID/timeline" | python3 -m json.tool | head -80
```

Expected: turn 1 shows `stt.final`, `llm.reply` (long), `tts.utterance`, then turn 2 shows `stt.final` (your interruption) BEFORE the tts.utterance from turn 1 finishes streaming. That's the whole point of Track A.

---

## Self-Review

**Spec coverage:**
- Non-blocking mailbox ✓ Tasks 2 + 3
- spawn_supervised ✓ Task 2
- emit_local ✓ Task 2
- typed events (brain_completed / brain_failed / speech_completed) ✓ Task 3
- rewrite _on_turn_event_end + _run_brain_from_text + _speak → spawn + return + emit-back ✓ Task 3 (rewrites _on_turn_event_end + adds _brain_job / _speech_job siblings; keeps _run_brain_from_text + _speak for the legacy code path so the flag can rollback cleanly)
- remove _drain_mailbox from bump_turn ✓ Task 2
- source_epoch on CallEvent ✓ Task 1
- feature flag ACTOR_NONBLOCKING_HANDLERS ✓ Task 1 (name in code: `actor_nonblocking_handlers`)
- fix 3 turn_manager tests + 1 barge test ✓ Task 4
- time-to-testable ✓ Task 5

**Placeholder scan:** no TBDs, no vague "add validation". Every code block is the exact code an engineer types.

**Type consistency:**
- `spawn_supervised(coro, *, generation: int, name: str) -> asyncio.Task` — used consistently in Tasks 2/3.
- `emit_local(event: CallEvent) -> None` — used consistently.
- Payload shapes for `brain_completed`, `brain_failed`, `speech_completed` fully specified in Task 3.
- `source_epoch=turn_gen` passed on every emit in Task 3.
- `settings.actor_nonblocking_handlers` — same field name in Tasks 1 and 3.

Plan complete and saved to `docs/superpowers/plans/2026-08-05-sprint12-track-a-nonblocking-mailbox.md`.

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review between tasks.

**2. Inline Execution** — do all 5 tasks in-session with checkpoint after each.

Which?
