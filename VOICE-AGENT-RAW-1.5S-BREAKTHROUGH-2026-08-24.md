# RAW VOICE PATH: 3s → 1.5–2.0s BREAKTHROUGH AUDIT
## Exact codebase: `receptionist-codebase-2026-08-24_0254-3s-floor-audit-2026-08-24.zip`
## Goal: improve TRUE uncached/general-model latency, not hide it with response cache

---

# Executive conclusion

Keep `RESPONSE_CACHE_BYPASS=true` for the raw-path benchmark.

The latest codebase still has a much more fundamental problem than cache, Flux threshold tuning, or TTS chunk size:

> The confirmed nonblocking turn path and the speculative/legacy turn path use different LLM execution semantics.

With `ACTOR_NONBLOCKING_HANDLERS=true`, a normal confirmed `END_OF_TURN` spawns `_brain_job()`. `_brain_job()` calls `session_manager.run_user_turn()` without `on_delta`. In `ReceptionistBrain.handle_user_turn()`, streaming is enabled only when `on_delta is not None`, therefore the normal confirmed path uses the batch `complete()` path and does not begin TTS until the whole LLM response is returned.

The repo's own `WORKING-NOTES.md` already identified this on 2026-08-18:

> `STREAMING_LLM_TO_TTS=true` confirmed at runtime, but the live call had ZERO `TTS_SENTENCE_QUEUED`; `_brain_job` batch path used by nonblocking handlers never checks `_streaming_llm_eligible`.

The current zip still contains this architectural split.

This is the first P0 to fix.

---

# P0-1 — UNIFY THE NORMAL FINAL PATH WITH STREAMING LLM → TTS

## Current confirmed Final path

`apps/api/app/core/config.py`

```python
actor_nonblocking_handlers: bool = True
```

`apps/api/app/routes/twilio_actor.py::_on_turn_event_end`

```python
if settings.actor_nonblocking_handlers:
    actor.spawn_supervised(
        self._brain_job(text, turn_gen),
        generation=turn_gen,
        ...
    )
    return
```

`_brain_job()`:

```python
payload = await session_manager.run_user_turn(
    state,
    brain,
    transcript,
)
```

No `on_delta`.

`packages/core_agent/brain.py`:

```python
_stream_ok = (
    on_delta is not None
    and hasattr(self.llm, "stream_complete")
)
```

So the normal path is:

```text
Flux Final
→ full OpenAI request
→ wait for COMPLETE model answer
→ persistence
→ brain_completed
→ speech_job
→ ElevenLabs
```

rather than:

```text
Flux Final
→ OpenAI SSE
→ first safe sentence
→ ElevenLabs begins
while OpenAI continues generating
```

## Why this can cost hundreds of milliseconds

The relevant voice metric is not only TTFT.

Example:

```text
OpenAI first useful text      700ms
first safe sentence           800ms
full response completion     1050ms
```

With streaming:

```text
TTS can start ~800ms
```

With the current batch normal path:

```text
TTS cannot start until ~1050ms+
```

Then persistence can add more time before `brain_completed`.

Expected raw benefit on short replies:

```text
~150–400ms common
```

Potentially larger on longer replies.

More importantly, it removes a major p95 bifurcation:

```text
Eager fired → streaming path → fast
Eager did not fire / was suppressed → batch path → slow
```

This can make the same user-visible test fluctuate wildly despite identical providers.

## Correct implementation

Do NOT disable the nonblocking actor architecture.

"Nonblocking" should mean:

```text
spawn the brain task
```

not:

```text
switch to batch LLM behavior
```

### Preferred change

Create ONE canonical text-turn executor:

```python
async def _execute_text_turn(
    transcript,
    turn_gen,
    owns_lock=False,
):
    ...
```

It contains:

1. slot capture
2. fastpath/cache if enabled
3. streaming LLM→SentenceBuffer→TTS
4. batch fallback only if streaming unsupported
5. tool callbacks
6. generation/cancellation rules

Then:

```python
actor.spawn_supervised(
    self._execute_text_turn(text, turn_gen),
    generation=turn_gen,
)
```

for confirmed Final.

Eager calls the same executor with:

```python
owns_lock=True
```

This deletes the duplicated `_brain_job` vs `_run_brain_from_text` behavior.

### Minimal proof patch

Before a larger refactor, temporarily change confirmed Final nonblocking dispatch from:

```python
self._brain_job(...)
```

to supervised:

```python
self._run_brain_from_text(...)
```

and run targeted regression tests.

If lifecycle differences make that unsafe, port `_run_brain_streaming` callbacks into `_brain_job`, but the required invariant is:

```text
STREAMING_LLM_TO_TTS=true
=> normal confirmed text turn must emit LLM_STREAM_START
=> first TTS sentence may start before LLM_STREAM_DONE
```

## Acceptance criterion

For every novel no-tool raw turn:

```text
LLM_STREAM_START = exactly 1
LLM_FIRST_TEXT = exactly 1
TTS first safe sentence starts BEFORE LLM_STREAM_DONE whenever response length permits
```

No `complete()` batch call should happen unless the provider streaming path fails.

---

# P0-2 — MOVE SESSION PERSISTENCE OFF FIRST-SPEECH CRITICAL PATH

Exact current code:

`apps/api/app/core/session_manager.py`

```python
async def run_user_turn(...):
    result = await brain.handle_user_turn(...)

    for tool_payload in result.tool_results:
        persist_booking_from_tool(state, tool_payload)

    _persist_session(state)

    return {...}
```

`_persist_session()` performs synchronous SQLAlchemy work:

- create DB session
- query SessionRow
- count TranscriptRow
- PII-redact transcript data
- insert records
- `db.commit()`

`persist_booking_from_tool()` is also synchronous.

No `asyncio.to_thread()` exists around these operations in the exact uploaded zip.

## Why this matters especially today

On a proper streaming path:

```text
first sentence can already be speaking
while run_user_turn later persists
```

So persistence is mostly off the first-audio path.

On `_brain_job` batch path:

```text
LLM completes
→ synchronous persistence
→ run_user_turn returns
→ brain_completed
→ TTS begins
```

Thus the persistence bug and the batch-path bug amplify each other.

## Correct split

### Critical business write

If calendar/CRM/tool result must be committed before claiming success:

```text
await required business transaction
```

### Noncritical conversation/session persistence

```text
snapshot state
→ bounded persistence worker
```

Do not await it before first speech.

Possible implementation:

```python
await persistence_queue.put(snapshot)
```

where a single bounded worker serializes SQLite writes.

Minimum improvement:

```python
await asyncio.to_thread(_persist_session, snapshot)
```

but even that still waits.

Better:

```python
asyncio.create_task(...)
```

with:
- bounded queue
- shutdown flush
- error telemetry
- no unbounded task creation.

## Telemetry

```text
PERSIST_QUEUE_WAIT_MS
PERSIST_DB_MS
PERSIST_REDACTION_MS
PERSIST_COMMIT_MS
```

---

# P0-3 — EAGER MUST HAVE A DURABLE SPECULATION RECORD

Deepgram's intended pattern:

```text
EagerEndOfTurn
→ start LLM

TurnResumed
→ cancel

EndOfTurn
→ use already-prepared response
```

Current Final HIT test:

```python
if spec_task is not None and not spec_task.done() and spec_text:
    ...
```

This means a speculative task that finishes before Final does not enter this HIT branch.

Even if existing commit-lock/dedupe prevents an audible duplicate in many cases, task completion is the wrong definition of speculation ownership.

## Replace with

```python
@dataclass
class SpeculativeTurn:
    provider_turn_index: int
    transcript: str
    generation: int
    task: Task
    status: RUNNING | COMPLETED | CANCELLED | COMMITTED
    request_at: float
    first_delta_at: float | None
    first_sentence_at: float | None
    first_tts_audio_at: float | None
```

Final matches:

```text
provider turn index
+
exact transcript
```

not:

```text
is task still running?
```

If status is COMPLETED, Final can still commit/release its prepared result.

---

# P0-4 — PREEMPTIVE TTS, HELD LOCALLY UNTIL FINAL

This is the next real overlap after P0-1.

Current optimized concept:

```text
Eager
→ LLM starts
```

More aggressive:

```text
Eager
→ LLM starts
→ first safe sentence
→ ElevenLabs starts
→ audio buffered locally
```

Do not send this speculative audio to Twilio yet.

On:

```text
TurnResumed
```

cancel ElevenLabs context + discard local buffer.

On:

```text
EndOfTurn
```

if transcript matches:

```text
release already-generated audio immediately
```

This is the same class of optimization now exposed by LiveKit as `preemptive_tts`.

Potential gain:

```text
~100–300ms
```

when Eager precedes Final enough to hide TTS TTFB.

Requires:
- durable speculation object
- one call-long ElevenLabs WebSocket
- cancellable TTS contexts.

---

# P0-5 — SPEECH TURNS SHOULD NOT CARRY EVERY TOOL

Current clinic brain receives approximately:

```text
check_availability
book_appointment
lookup_faq
escalate_to_human
find_existing_appointment
cancel_appointment
reschedule_appointment
emit_semantic_plan
```

on general LLM turns.

OpenAI receives:

```python
tools=self.tools
tool_choice="auto"
```

even for ordinary conversation.

This is architecturally unnecessary.

## Raw speed benchmark should still use good routing

No cache does NOT mean:

```text
force every possible tool schema into every request
```

A fair raw benchmark can still use an optimized architecture.

Build lanes:

```text
SPEECH
tools = None

FAQ_WITH_RETRIEVAL
tools = [lookup_faq]

AVAILABILITY
tools = [check_availability]

BOOK
tools = [book_appointment]

CANCEL
tools = [find_existing_appointment, cancel_appointment]

RESCHEDULE
tools = [find_existing_appointment, reschedule_appointment]
```

The controller deciding the lane can be:
1. deterministic current-state policy when obvious;
2. cheap local intent classifier;
3. LLM fallback when ambiguous.

Benchmark exact prompt+history:

```text
all 8 tools
vs
no tools
vs
one relevant tool
```

Measure TTFT and tool accuracy.

Likely effect is smaller than P0-1 but can remove both latency and errors.

---

# P0-6 — TRUE LATENCY TELEMETRY

The user's finger-count benchmark is valuable, but the server still cannot precisely identify mouth-close.

Build a canonical per-turn trace.

## Stable identity

Use:

```text
provider_turn_index
```

from Flux.

Do NOT reset telemetry because actor generation increments.

## Input/audio clock

For every inbound Twilio 20ms frame, record:

```text
media.timestamp
receive_perf
RMS / VAD state
```

The repository already has:
- `RmsVAD`
- `SileroVAD`
- cheap µ-law RMS helpers.

Track:

```text
last_voiced_frame_perf
last_voiced_media_timestamp
```

with a short 40–80ms speech hangover to avoid cutting off final consonants.

## Flux

```text
FLUX_UPDATE_LAST
FLUX_EAGER
FLUX_FINAL
FLUX_CONFIDENCE
FLUX_PROVIDER_TURN_INDEX
```

Propagate confidence and turn index from `deepgram_flux_stt.py`; they are currently read and then discarded.

## Actor

```text
EVENT_CREATED
MAILBOX_HANDLER_START
COMMIT_LOCK_WAIT
PENDING_MERGE_WAIT
POLICY_START
POLICY_DONE
BRAIN_TASK_SPAWN
```

## LLM

Do not log one generic LLM time.

```text
LLM_REQUEST_BUILD_MS
LLM_HTTP_SEND_AT
LLM_HEADERS_AT
LLM_FIRST_SSE_EVENT
LLM_FIRST_TEXT_DELTA
LLM_FIRST_SAFE_SENTENCE
LLM_STREAM_DONE
```

Also:

```text
tool_schema_bytes
prompt_chars
prompt_tokens_est
message_count
history_chars
```

## Sentence/gate

```text
SENTENCE_BUFFER_FIRST_PUSH
SENTENCE_BUFFER_FIRST_RELEASE
SPEECH_GATE_ENTER
SPEECH_GATE_RELEASE
```

This catches:
- waiting for punctuation
- fake-wait gate holds.

## Persistence

```text
PERSIST_START
PERSIST_DONE
```

## TTS

```text
TTS_REQUEST
TTS_HEADERS
TTS_FIRST_BYTE
TTS_FIRST_AUDIO_READY
```

## Outbound

```text
AUDIO_LOCK_WAIT_START
AUDIO_LOCK_ACQUIRED
FIRST_MEDIA_SEND_START
FIRST_MEDIA_SEND_DONE
FIRST40_MARK_SENT
PLAYOUT_ACK_OBSERVED
```

## Derived latency

```text
mouth_proxy_to_eager
mouth_proxy_to_final

eager_to_llm_request
llm_ttft
first_delta_to_safe_sentence

safe_sentence_to_tts_request
tts_ttfb

audio_lock_wait

final_to_first_media
mouth_proxy_to_first_media

send_to_playout_ack_observed
```

## Automatic slow-turn diagnosis

If:

```text
mouth_proxy_to_first_media > 1500ms
```

emit:

```json
{
  "winner": "llm_ttft",
  "segments": {
    "endpoint": 180,
    "mailbox": 6,
    "llm_ttft": 790,
    "sentence_buffer": 105,
    "persistence": 0,
    "tts": 205,
    "audio_lock": 0,
    "send": 8
  }
}
```

No more human log archaeology.

---

# P0-7 — DUAL-CHANNEL CALL RECORDINGS FOR GROUND-TRUTH

For controlled test calls, enable Twilio dual-channel recording with silence not trimmed.

Twilio can store:
- inbound audio received by Twilio
- outbound audio generated by Twilio

on separate channels.

Run a small offline script:

1. detect last voiced sample in inbound channel;
2. detect first agent voiced sample in outbound channel;
3. calculate:
   `twilio_view_turn_gap_ms`.

This gives an independent media-plane ground truth, separate from application log clocks.

It does not measure the user's final cellular handset speaker delay, but it precisely tells us whether the missing ~500–1000ms is:
- before/inside Twilio;
- or in our application.

For one final acoustic test, put the phone on speaker and record both sides on a second device, then waveform-align caller last syllable → agent first sample.

---

# PERCEIVED LATENCY LANE — MICRO-ACK / LATENCY BRIDGE

This is a valid technique, but keep it separate from raw semantic latency.

The existing repo already has:
- prewarmed `FillerPool`
- cached backchannel playback
- `ReactiveBrain`
- allowed acknowledgements.

The previous implementation failed because it was TIMER DRIVEN:

```text
if LLM hasn't answered after 600ms:
    say generic filler
```

Karachi LLM turns naturally crossed the timer almost every time, producing:

```text
"Gotcha..."
"Okay..."
"One second..."
```

before virtually every answer.

That sounds robotic.

## New architecture: `LatencyBridgePolicy`

Not a filler timer.

Input:

```yaml
expected_action:
caller_act:
sentiment:
question_type:
tool_started:
real_audio_ready:
elapsed_since_eot:
recent_backchannels:
```

Output:

```yaml
eligible: true|false
backchannel_class:
deadline_ms:
max_duration_ms:
```

## Trigger

Schedule the bridge around:

```text
250–400ms after Eager/Final
```

ONLY when context says an acknowledgement is natural.

If real answer audio becomes ready first:

```text
cancel bridge
```

If not:

```text
play one ultra-short cached clip
```

## Classes

### LISTENING / ACKNOWLEDGE

For caller providing context:

```text
Mm-hm.
Right.
Got it.
```

### ACCEPTANCE

When acknowledgement is semantically safe:

```text
Okay.
Alright.
```

### CORRECTION

```text
Right.
Got it.
```

### TOOL

Only once the real tool has actually started:

```text
Let me check.
One sec.
```

Never promise waiting before a tool exists.

### SENSITIVE

Avoid:
- Perfect.
- Awesome.
- Great.

Use:
- Mm-hm.
- I see. (if semantically safe)

## Suppress bridge entirely for

- direct yes/no factual question
- emergency
- caller complaint where a casual ack is inappropriate
- price question
- correction requiring immediate content
- caller interrupting previous answer
- real response expected within the next ~200ms.

---

# PROSODY VARIANTS: YES, DO THIS OFFLINE

The user proposed:

```text
"oook"
"aalright"
```

with different pitch/tone/intonation.

The underlying idea is good; do NOT dynamically ask ElevenLabs to generate these while the caller waits.

Pre-generate and manually curate variants.

Example library:

```text
ack_mhm_neutral_01
ack_mhm_warm_01
ack_mhm_rising_01

ack_right_quick_01
ack_right_soft_01
ack_right_warm_01

ack_okay_neutral_01
ack_okay_long_01
ack_okay_rising_01

ack_alright_quick_01
ack_alright_warm_01
ack_alright_thoughtful_01
```

Duration target:

```text
150–350ms
```

Avoid 500–700ms filler clips for latency bridging.

## Generate variation offline with ElevenLabs

For Flash v2.5:
- vary stability;
- vary speed;
- generate multiple seeds;
- vary punctuation/text spellings carefully;
- manually listen and retain only natural versions.

Lower stability broadens emotional/prosodic variation.
Speed can vary roughly 0.7–1.2.
Style exaggeration can increase latency, but offline generation means runtime latency does not matter; still prefer curated natural outputs.

Do not rely on Eleven v3 audio tags for Flash v2.5.

## Runtime selection

Weighted recency-aware pick:

```python
variant = bridge_pool.pick(
    semantic_class="acknowledge",
    caller_affect="neutral",
    caller_rate="fast",
    exclude_recent=5,
)
```

Never repeat the same phrase/prosody twice in a short window.

---

# IMPORTANT: MICRO-ACK AUDIO MUST NOT BLOCK THE REAL ANSWER

Current `_outbound_audio_lock` is held for the ENTIRE audio buffer.

So:

```text
microack starts
→ real answer becomes ready
→ answer waits for full microack
```

This can increase semantic latency.

For a 250ms clip the penalty is bounded and often acceptable.

Better output arbiter:

```text
SpeechOutputController
  priority:
    REAL_ANSWER = 100
    TOOL_ACK = 60
    LATENCY_BRIDGE = 20
```

Microack is:

```text
preemptible=true
max_duration <= 300ms
```

When real answer is ready:
- if ack has <80ms remaining: finish;
- otherwise `clear` queued Twilio audio at a safe boundary and start answer;
- never interleave media frames.

Track both:

```text
FIRST_ACOUSTIC_RESPONSE_MS
SEMANTIC_ANSWER_START_MS
```

The bridge is not allowed to "improve" the semantic latency metric.

---

# HOW FAST COMMERCIAL STACKS GET THERE

They combine several mechanisms rather than finding one magic provider.

## 1. Preemptive generation

Start LLM before final turn commitment.

## 2. Sometimes preemptive TTS

Generate audio before final commitment and discard it if caller resumes.

## 3. Stream model text immediately

Do not wait for the whole LLM response before starting TTS.

## 4. Context-sensitive backchannels

Backchannels occur when semantically appropriate, with configurable word sets/frequency, rather than a blind delay timer.

## 5. Dynamic responsiveness

Turn-taking aggression changes based on caller pace/context.

## 6. Smaller active context

Only active node/state/tools are in the model request.

## 7. Tool workflows avoid repeated general LLM rounds

Deterministic state/tool routing wherever the next action is already known.

## 8. Speech/media plane is heavily instrumented

They distinguish:
- STT
- network
- application
- TTS
- actual time-to-first-audio.

---

# ADD A RAW SPEED SLO

Maintain TWO independent SLOs even with cache disabled.

## Semantic raw SLO

For novel no-cache/no-fastpath no-tool turn:

```text
MOUTH_PROXY → SEMANTIC_ANSWER_FIRST_MEDIA

p50 <= 1.5s target
p95 <= 2.0s target
```

This is the hard engineering metric.

## Acoustic response SLO

If latency bridge is eligible:

```text
MOUTH_PROXY → FIRST_ACOUSTIC_RESPONSE

p50 <= 500–700ms
```

The user instantly feels that the agent is alive.

Then:

```text
FIRST_ACOUSTIC_RESPONSE → SEMANTIC_ANSWER
```

should remain short enough not to feel like stalling.

---

# EXPECTED RAW PATH AFTER P0 FIXES

Current simple raw trace is roughly:

```text
Flux Final
→ model / batch path        ~0.7–1.0s+
→ TTS/send                  ~0.28–0.4s
```

Current Final→first media:

```text
~1.2–1.4s
```

The user's actual mouth-to-ear experience includes:
- endpointing before Final
- last-mile/playout after first server media.

## After unified streaming Final path

Potential Final→first-media:

```text
~0.9–1.1s
```

depending on sentence boundary.

## Add useful Eager headstart

If Eager starts 150–300ms before Final:

```text
effective post-mouth semantic compute is reduced by that headstart
```

## Add persistent EL WebSocket

Potential another:

```text
~0.1–0.2s
```

## Add preemptive TTS

Can hide another:

```text
~0.1–0.25s
```

where Eager lead is available.

Therefore:

```text
1.5–2.0s typical TRUE raw novel turns from Karachi
```

is a reasonable engineering target.

A consistent sub-1.5 p95 may still require geographic/infrastructure changes.

---

# P1 — PERSISTENT ELEVENLABS MULTI-CONTEXT WS

Current direct gain is not enormous, but it is the correct primitive.

Use:
- one WebSocket per call;
- one context per answer/speculation;
- `flush` at complete sentence;
- close context on interruption.

This is also needed for clean preemptive TTS.

---

# P1 — OPENAI RESPONSES WEBSOCKET

Current code's custom persistent WS scaffold is incomplete.

A correct implementation should:
- keep one WS per call;
- use Responses API WebSocket mode;
- continue state with `previous_response_id`;
- preserve instructions correctly;
- continue tool outputs on the same response state;
- avoid rerunning the same tool turn over HTTP.

OpenAI reports large gains on multi-step agentic workloads; expect smaller but potentially useful savings for a simple one-turn receptionist.

This is more valuable on booking/tool chains than a single FAQ turn.

---

# P1 — ACTIVE PROMPT / TOOL SLICES

The prompt source remains large.

Instead of global compaction:

```text
GLOBAL CORE
+
CURRENT TASK
+
CURRENT STATE
+
ONLY RELEVANT TOOL DEFINITIONS
```

No-cache testing remains honest.

Caching and context minimization are different optimizations.

---

# P1 — CONVERSATIONRELAY AS AN A/B CONTROL, NOT A REWRITE

Keep:
- custom brain
- policy
- LLM
- tools.

Replace only:
- Media Streams STT/TTS orchestration

with ConversationRelay for one branch.

Feed the same LLM streamed tokens.

Twilio provides component-level:
- Network
- STT
- Application
- TTS
- Time to first audio.

If the same brain is dramatically faster:
- media/orchestration/geography is the remaining bottleneck.

If not:
- focus on brain/model path.

This experiment answers the question faster than more guesses.

---

# P2 — US-EAST A/B

Do not migrate blindly.

Run exact same commit/config and recorded user audio on:
- Karachi
- US-East.

No cache in either.

Compare:
- Eager
- Final
- LLM TTFT
- first sentence
- first media
- Twilio playout ACK
- Twilio dual-channel turn gap.

This gives the exact price of the current Twilio-US ↔ Karachi ↔ provider topology.

---

# EXACT CLAUDE QUEUE

## NETWORK / RUNTIME CLAUDE

### R0 — prove current execution lane

For every Final turn log:

```text
EXECUTION_LANE=batch_nonblocking|streaming_final|streaming_eager
STREAMING_LLM_FLAG=true|false
```

### R1 — fix confirmed Final streaming

Unify `_brain_job` and `_run_brain_from_text`.

No ordinary raw turn may silently use batch when streaming flag is on.

### R2 — canonical latency trace

Implement stable provider-turn trace + last-voiced frame.

### R3 — Eager ROI telemetry

Measure actual headstart.

### R4 — durable speculation record

Fix completed-before-Final state.

### R5 — persistence off critical path

Measure before/after.

### R6 — persistent EL WS

### R7 — speculative TTS local buffer

### R8 — latency bridge output arbiter

Priority/preemption.

---

## VOICE / BRAIN CLAUDE

### V1 — speech vs action lanes

Relevant tool subset.

### V2 — active prompt slice

### V3 — LatencyBridgePolicy

No blind timer.

### V4 — build offline microack/prosody library

150–350ms clips.

### V5 — tool-result deterministic rendering

### V6 — provenance validator

Remove repeated LLM rounds.

---

# FIRST TEST AFTER R1

Keep:

```text
RESPONSE_CACHE_BYPASS=true
```

Use a novel no-tool question.

Required logs:

```text
EXECUTION_LANE=streaming_final OR streaming_eager
LLM_STREAM_START
LLM_FIRST_TEXT
FIRST_SAFE_SENTENCE
TTS_STREAM_START
LLM_STREAM_DONE
TWILIO_FIRST_MEDIA_SENT
```

The key assertion:

```text
TTS_STREAM_START < LLM_STREAM_DONE
```

If not:

```text
streaming is still fake/unreachable.
```

Run at least 20 turns and report:
- p50
- p95
- Eager hit ratio
- batch fallback ratio.

---

# SECOND TEST — LATENCY BRIDGE

No cache.

Same raw LLM.

A/B:

A:
```text
bridge off
```

B:
```text
bridge on
```

Report separately:

```text
first_acoustic_response
semantic_answer_start
```

Do not claim the bridge reduced actual model latency.

Pass condition:
- first acoustic response improves massively;
- semantic answer penalty <=100ms median, <=250ms max;
- no repeated robotic phrase complaints.

---

# FINAL PRIORITY

1. Fix normal Final batch-path bug.
2. Fix telemetry and measure Eager.
3. Move persistence off first-speech path.
4. Speech-only/relevant-tool lanes.
5. Persistent EL WS.
6. Preemptive TTS.
7. Semantic microack / latency bridge.
8. Responses WS.
9. ConversationRelay media-plane A/B.
10. US-East A/B.

Do not change models or Flux thresholds again until steps 1–3 are measured.
