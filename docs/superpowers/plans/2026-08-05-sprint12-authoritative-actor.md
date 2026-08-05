# Sprint 12 — Authoritative Non-Blocking Actor

**Origin:** ChatGPT external audit `docs/AUDIT_2026-08-05-runtime-failure-patterns.md`, verified in `docs/AUDIT_VERIFICATION_2026-08-05.md`.

**Goal:** Make the per-call actor the single, non-blocking, event-sourced control plane. Handlers spawn supervised jobs and return immediately. External work returns typed events. Legacy paths shut off. One authority for turns, evidence, actions, playback truth.

**Non-goal:** New features, new intelligence, new tuning. This sprint is purely architectural.

## The invariant

> Every meaningful state transition occurs inside one per-call actor.
> Actor handlers never await external work.
> External work can only return typed events to the actor.

## Tracks (in order — each track is independently shippable)

### Track A — Non-blocking actor mailbox (fixes P0-1, P0-2)

- Introduce `Actor.spawn_supervised(coro, generation)` → returns a task registered against a generation, cancelled on `bump_turn`.
- Rewrite every `async def _on_*` handler to spawn + return. No `await` on brain/TTS/tool work inside handlers.
- Rewrite `_run_brain_from_text` as a job that emits typed events: `LLMStarted`, `LLMToolProposed`, `LLMCompleted`, `LLMFailed`.
- `_speak` becomes a job that emits `TTSChunkQueued`, `TTSCompleted`, `TTSFailed`.
- `bump_turn` becomes a pure state transition — no `_drain_mailbox`. Events carry `source_epoch`; late events checked against current epoch and dropped if stale.

**Ship criteria:** an interruption emitted during agent speech gets dispatched within ≤50 ms (not blocked behind the speech job).

### Track B — Single turn authority (fixes P0-3)

- Add config flag `TURN_AUTHORITY=streaming|legacy` (default `streaming`).
- Under `streaming`: `_buffer_barge_frame` runs in shadow mode (logs decisions, does not mutate state).
- Under `legacy`: TurnManager runs in shadow mode.
- Add a shadow-diff log that reports when the two systems would have disagreed.
- Remove the losing system entirely after 1 week of clean shadow logs.

**Ship criteria:** grep confirms only ONE code path mutates `actor.turn_generation` per call.

### Track C — Utterance decision preservation (fixes P0-4)

- Introduce `UtteranceRecord` in TurnManager state: `utterance_id`, `current_classification`, `committed_classification`.
- On partial → classify (BACKCHANNEL / PAUSE / INTERRUPTION_CANDIDATE / SPEECH).
- On final → REFINE the existing classification, don't overwrite. `BACKCHANNEL` + final content matching → stays `BACKCHANNEL` (don't fire brain).
- New tests: partial(yeah) → final(yeah) stays BACKCHANNEL; partial(hold on) → final(hold on) stays PAUSE; partial(actually…) → final(actually make it Tuesday) becomes INTERRUPTION.

**Ship criteria:** "Yeah" mid-agent-speech never fires the brain.

### Track D — Evidence provenance (fixes P0-5)

- Add `EvidenceSource` enum in `packages/dialogue/schemas.py`: `CALLER_UTTERANCE`, `TOOL_RESULT`, `BUSINESS_PROFILE`, `MODEL_INFERENCE`, `MODEL_PROPOSAL`.
- `SlotEvidence.source_role` replaced by `source_kind: EvidenceSource`.
- `_record_slot_evidence` in `kernel_wiring.py` REQUIRES an explicit `source_kind` (no default).
- LLM tool-call handler paths pass `MODEL_PROPOSAL`.
- Slot-satisfaction check requires ≥1 evidence with `source_kind in (CALLER_UTTERANCE, TOOL_RESULT)`.
- Migration: rewrite existing sites that pass no `source_kind` to pass `MODEL_INFERENCE`; grep -R for `_record_slot_evidence` should return zero call sites lacking the parameter.

**Ship criteria:** grep confirms no LLM-tool-call path creates a slot satisfying evidence entry.

### Track E — Commit coordinator is the only write path (fixes P0-6)

- Rename `book_appointment` → `propose_booking` in the tool schema exposed to the LLM. Tool returns `{"proposal_id": "..."}` — no side effect.
- `try_commit_booking` becomes the ONLY code path that calls the calendar adapter.
- Add an assertion in the calendar adapter: `if not caller_stack_contains("CommitCoordinator"): raise RuntimeError`.
- Assertion stays in code with an env-gated bypass for tests.

**Ship criteria:** deleting the calendar adapter's public methods from every non-coordinator call site.

### Track F — Deepgram error propagation (fixes P0-7)

- Replace `StreamEnded` handling. Provider adapter yields typed terminal outcomes:
  ```python
  class StreamEnded:
      reason: Literal["normal_close", "remote_close", "auth_failure", "protocol_error", "timeout"]
      retryable: bool
      original_exception: Optional[str]
  ```
- Producer/consumer exceptions no longer swallowed — they classify + raise via a common `_terminate(reason, retryable)` helper.
- Bridge distinguishes `NORMAL_CLOSE` (stop) vs everything else (reconnect ladder).
- Contract test: monkeypatch `ws.recv` to raise `websockets.ConnectionClosed(1011, ...)` → assert bridge sees `StreamEnded(remote_close, retryable=True)` → assert reconnect attempt fires.

**Ship criteria:** killing Deepgram mid-call → bridge reconnects within 2s.

### Track G — Event epochs (fixes P1-1, P1-2)

- Every `CallEvent` gets an immutable `source_epoch` field, set when the underlying signal was captured (not when the event is emitted).
- STT events: `source_epoch = actor.turn_generation AT AUDIO CAPTURE TIME` — captured in bridge feed loop, NOT the transcribe callback.
- Mark ack events: parse `generation` from `mark_id` (`m{generation}-{counter}`), reject if `< actor.speech_generation`.
- Actor drops any event whose `source_epoch < current_generation`.

**Ship criteria:** delayed Deepgram final for stale audio doesn't get processed.

### Track H — Playback truth (fixes P1-3)

- TTS producer emits clause-level chunks (split on `. ! ? ,` + max 300ms chunks).
- Each chunk gets its own `mark_id` and its own `text_start` / `text_end` range.
- Ledger tracks per-chunk ack. `heard_text_for(gen)` sums the ack'd chunks' text ranges.
- Interruption reconciler operates on the actual last-ack'd chunk, not the whole utterance.

**Ship criteria:** mid-response interruption produces heard-text matching what actually played (within ±1 sentence).

## Tracks NOT in Sprint 12 (backlog)

- Config unification (P2-1)
- Reducer batch atomicity (P2-3)
- Persistent idempotency (P2-4)
- Language state (multilingual work, parked separately)
- Better small-model kernel (Sprint 13)
- Voice cloning (small polish task)

## Implementation order + budget

| Track | Effort | Deps |
|-------|--------|------|
| A     | 1 day  | none |
| B     | 0.5 day | A (shadow-mode needs typed events) |
| C     | 0.5 day | A |
| D     | 1 day  | none (parallel-safe) |
| E     | 1 day  | D (needs evidence provenance) |
| F     | 0.5 day | none (parallel-safe) |
| G     | 0.5 day | A |
| H     | 1 day  | A + G |

Total: ~6 dev days if sequential, ~4 if I parallelize A+D+F.

## Testing strategy per track

Every track ships with:
1. A unit test targeting the specific fix (existing test suite gets the update)
2. A regression test showing the OLD behavior is now impossible (e.g. "assert `spawn_supervised` was called, `handler` returned within X ms")
3. Manual test line in the demo test script (`docs/BROWSER_TEST_SCRIPT.md`)

## Rollback plan

Each track lands behind its own feature flag:
- `ACTOR_NONBLOCKING_HANDLERS=true|false`
- `TURN_AUTHORITY=streaming|legacy|shadow`
- `EVIDENCE_STRICT=true|false`
- `COMMIT_COORDINATOR_ONLY=true|false`
- `STT_TYPED_TERMINATION=true|false`
- `EVENT_EPOCHS=true|false`
- `TTS_CLAUSE_CHUNKS=true|false`

Flip individual flags off if regression appears. Delete flags after 1 sprint of clean production.

## The one number that says we succeeded

**Auditor's next-5-bugs list.** After Sprint 12, none of them should be reproducible.
