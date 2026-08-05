# External Audit — Runtime Failure Patterns

**Source:** ChatGPT deep audit, provided by user on 2026-08-05
**Prompt:** `docs/CHATGPT_AUDIT_PROMPT.md`
**Bundle:** `audit-bundle.zip` (repo snapshot at audit time)
**Verdict:** Architecture is present but not authoritative — old paths still make the real decisions.

## Executive summary (in the auditor's words)

> The new repository is substantially better architecturally than the previous version. Several sophisticated systems are present, but the old execution paths still make the real decisions:
>
> - The dialogue kernel records state, but direct LLM tool calls still execute actions.
> - The commit coordinator exists, but production booking does not use it.
> - The actor has generation cancellation, but its mailbox blocks during the work that must be interrupted.
> - TurnManager classifies backchannels, but final transcripts override that classification.
> - The playback ledger supports detailed reconciliation, but the live path registers one whole response as one chunk.
> - Streaming STT has reconnect machinery, but the provider swallows the errors that should trigger it.
>
> This explains why it "works" while still feeling unintelligent and unpredictable. The intelligence components are not consistently in control.

## Test-suite state at audit time

- 915 passed
- 28 failed
- 37 skipped

Failures mixed environmental (Cartesia SDK, sqlite-vec, num2words) with real (TurnManager tests assume pre-fragment-buffer behavior; stale `_send_mulaw_frames` reference; reconnect test uses fake provider that raises while real one swallows).

## The 8 failure classes (auditor's taxonomy)

| Class | Definition |
|-------|-----------|
| Lifecycle ownership + scheduler starvation | Control events, long-running work, cancellation without one clear owner |
| Split-brain authority | Two subsystems independently deciding the same turn/state/action |
| Edge-vs-level state errors | Flag meant to detect transition treated as persistent state |
| Noisy-input overcommitment | Partial speech, echo, single keyword promoted to intent/task/action |
| Representation + layering leakage | Transport formats leaking into dialogue/timing/domain code |
| Optimistic vendor contracts | Assumes providers report failures cleanly and return valid data |
| Observability epistemic gaps | Metric measures a proxy — data lies while being technically valid |
| Configuration + environment parity drift | Runtime, tests, docs get config through different paths |

## P0 findings (verbatim, ordered by impact)

### P0-1 — Actor mailbox blocked by the work it must control
Handlers await LLM/tool/TTS/playback completion, so an interruption sits in the queue behind the operation it's meant to interrupt. Invalidates the actor's principal concurrency guarantee.

**Fix:** handlers `spawn_supervised(coroutine)` and return; jobs emit typed events (`LLM_COMPLETED`, `TTS_CHUNK`, etc.) back to the mailbox.

### P0-2 — `bump_turn()` drains its own mailbox
Called from inside a handler, awaits the handler's own queue to empty. Deadlocks until timeout. Not a permanent hang — an unexplained 500 ms latency.

**Fix:** generation advancement is an actor transition, not a helper that waits for future actor work. Events carry immutable source epochs; queue ordering determines precedes/follows.

### P0-3 — Two interruption systems process the same speech
`_buffer_barge_frame` (legacy VAD/batch) runs alongside StreamingSTTBridge + TurnManager. Same interruption can independently: clear playback, advance generations, fire brain, append transcript, initiate tool actions.

**Fix:** pick one turn-management authority per call. Shadow mode may observe/log; must not produce state changes.

### P0-4 — Backchannels and pauses become interruptions on final transcript
Partial correctly classifies "yeah" as BACKCHANNEL or "hold on" as USER_REQUESTED_PAUSE. On final, `_on_final` unconditionally emits INTERRUPTION if agent speaking.

**Fix:** `UtteranceDecision` with `current_classification` + `committed_classification`; finalization refines existing classification, doesn't disregard it.

### P0-5 — Model-proposed tool arguments stored as explicit caller evidence
Before write validation, kernel records LLM tool args as slot evidence with `source_role=caller, status=explicit, confidence=0.85`. If model hallucinates `{"caller_name": "Alex"}`, the evidence ledger now preserves it as an explicit caller fact.

**Fix:** separate evidence classes: `CALLER_UTTERANCE`, `TOOL_RESULT`, `BUSINESS_PROFILE`, `MODEL_INFERENCE`, `MODEL_PROPOSAL`. Model tool args = `MODEL_PROPOSAL` only. Cannot satisfy required slots without valid source evidence.

### P0-6 — Propose→confirm→commit isn't authoritative
`try_commit_booking()` exists but production brain path uses `await self.tool_handler(tc)` directly. Commit coordinator exists mostly as observability.

**Fix:** booking tools shouldn't be exposed to main LLM as direct commit ops. Expose `propose_booking`; app policy decides if evidence sufficient; only commit coordinator invokes the actual adapter.

### P0-7 — Deepgram failures don't trigger reconnect
Producer/consumer catch exceptions, log warnings, terminate through normal queue sentinel. Bridge sees "stream completed cleanly" — no reconnect, no `stream_failed`, no distinction from intentional shutdown.

**Fix:** provider adapters produce typed terminal outcomes (`StreamEnded(reason=NORMAL_CLOSE|REMOTE_CLOSE|AUTH_FAILURE|PROTOCOL_ERROR, retryable=bool)`).

## P1 findings (summary)

1. Late STT results stamped with current generation instead of source epoch
2. Twilio mark ack ignores generation encoded in mark ID
3. Heard-text reconciliation is response-level, not clause-level (single AudioChunk per response)
4. Audio timing hardcoded to 16 kHz PCM math (`duration_ms = bytes / 32`)
5. `tts_first_byte` measured after complete synthesis
6. Greeting blocks inbound processing
7. Browser echo protection disables barge-in (my Sprint 11 quick fix)
8. Streaming queue drops incoming frame under overload (should drop oldest)
9. TurnManager delayed tasks not cancelled on session stop
10. Idle followup hardcoded to Smile Dental + armed in `finally` after cancellation
11. Deepgram hardcoded to `en-US`

## P2 findings (structural debt)

1. Config still dual-loaded via `os.environ` + Pydantic in multiple places
2. Intent discovery still overcommits on regex keyword matches
3. Reducer batches only "atomic-ish" — partial patches remain on failure
4. Commit idempotency is in-memory only (dies on restart, not per-tenant persistent)
5. State-transition errors swallowed rather than typed failure events

## The 5 predicted next bugs

1. Duplicate reply or booking after simultaneous barge paths
2. Ghost turn from delayed Deepgram final stamped with current generation
3. "Yeah" or "hold on" unexpectedly stops the agent
4. Heard-text reconciliation deletes or preserves too much
5. Hallucinated slot survives the safety architecture

Plus a 6th: silent STT death after provider ws close — real adapter converts failure to normal completion.

## The one architectural change

> Build one authoritative, non-blocking, event-sourced per-call runtime.
>
> Every meaningful state transition occurs inside one per-call actor. Actor handlers never await external work. External work can only return typed events to the actor.

### Mandatory implementation rules

1. Mailbox handlers cannot await provider work — spawn supervised, return
2. Jobs never mutate call state directly — emit typed events back to actor
3. Exactly one system owns each decision (one turn manager, one reducer, one commit coordinator, one ledger, one transcript, one language state, one config source)
4. Every event has immutable provenance (call_id, transport_session_id, provider_stream_id, audio_epoch, utterance_id, turn_generation, speech_generation, event_sequence, monotonic_timestamp)
5. Actions can only originate from evidence-backed state (model can propose, but proposal must reference `caller_turn_7, span 14–22`)
6. Observability derived from canonical events — timeline, latency, debug UI all consume the same actor event log

## Recommended implementation order

**First: stabilize temporal control**
1. Stop awaiting brain and speech tasks inside actor handlers
2. Remove mailbox draining from `bump_turn()`
3. Disable either legacy barge or TurnManager (one authority)
4. Preserve partial classifications when final STT arrives
5. Add provider-stream and utterance epochs to STT events
6. Propagate abnormal Deepgram termination to reconnect logic

**Second: make the intelligence kernel real**
7. Stop recording model tool args as caller evidence
8. Commit coordinator = only route to booking writes
9. Require evidence IDs for every material write argument
10. Atomic reducer batches
11. Persist idempotency records
12. Typed failure events on invalid state transitions

**Third: repair playback truth**
13. Stream TTS in clause-level chunks
14. Structured audio-format metadata
15. Per-chunk playback ack
16. Reconcile transcript to caller-heard text after interruption
17. Measure actual first token / first audio byte / first transmitted frame

**Fourth: enforce parity**
18. All config via Pydantic
19. Contract tests against real adapters (not fakes)
20. Partial→final TurnManager sequence tests
21. Duplicate-interruption tests with both barge systems
22. Browser + Twilio run same canonical scenarios

## My verification status

Pending. See `docs/AUDIT_VERIFICATION_2026-08-05.md` for line-by-line check of each P0 claim against actual code + real vs environmental test failures.
