# Voice Receptionist — Sub‑1.5s R&D + Action Roadmap
## Codebase-specific audit and research map — 2026-08-23

**Scope:** current `receptionist-codebase-2026-08-23_1136-speed-audit-2026-08-23` snapshot.

**Current pipeline:** Twilio Media Streams → Deepgram Flux → dialogue/LLM runtime → ElevenLabs Flash v2.5 → Twilio.

**Primary target:** make normal turns feel ≤1.5s when possible, keep p95 sane, preserve booking/tool correctness and humanness, and make the same architecture deployable for multiple concurrent client calls.

---

# 0. Executive conclusion

The current stack is no longer primarily missing a faster model. The next large gains come from **eliminating entire sequential stages, starting safe work speculatively, and making timing/context state explicit**.

The highest-leverage architecture is:

```text
caller audio
    ↓
Flux streaming + Eager EOT
    ↓
ConversationState / NextActionPolicy
    ├─ deterministic recognition/action → tool/template → TTS
    ├─ tool action → deterministic/provenance validation → tool → renderer
    └─ real free-form language need → LLM → semantic chunk → TTS
                         ↓
               persistent TTS session
                         ↓
               single outbound playout queue
                         ↓
                       Twilio
```

The highest-value changes are:

1. Wire `NextActionPolicy` into the real runtime and make it the authority for the *next operation*, not a prompt hint.
2. Remove second/third LLM round trips from normal tool turns.
3. Replace LLM write validation with deterministic slot provenance for normal booking commits.
4. Split **ACTION** model calls from **SPEECH** model calls; never let the same request freely choose between internal JSON and spoken prose.
5. Add **speculative pre-synthesis**: Eager EOT can start LLM *and TTS*, but hold audio locally until final EOT authority.
6. Drive Flux EOT/eager/timeout values from expected input type using Deepgram's mid-stream `Configure` support.
7. Remove fixed 2-second structured-input and incomplete-word waits where state-aware logic can decide earlier.
8. Keep one ElevenLabs multi-context WebSocket for the entire phone call.
9. Stop polling SmartTurn continuously; use it when VAD/Flux says the turn boundary is ambiguous.
10. A/B a regional media path such as LiveKit India/Mumbai while preserving your existing brain.
11. Build a local-LLM voice lane benchmark because local inference can remove the 300–400ms public-Internet model RTT entirely.
12. Make all concurrency work measurable: real N=1/5/10/20 spoken-call load tests, not silent WebSocket probes.

---

# 1. What is already DONE / should not be re-investigated blindly

These items were either fixed in the current snapshot or already benchmarked and came back neutral. Do not let another coding session rediscover them as new work.

## DONE-1 — SmartTurn μ-law decoding bug

Current code correctly converts Twilio μ-law to linear PCM before 8→16k resampling:

- `packages/runtime/streaming_stt_bridge.py:275-289`

Do not reopen the old malformed-audio finding unless a regression test fails.

## DONE-2 — Flux duplicate EndOfTurn injection

Current Flux provider now emits a native `end_of_turn` instead of feeding the same final through both the normal-final and native-EOT paths:

- `apps/api/app/providers/stt/deepgram_flux_stt.py:246-264`

## DONE-3 — ElevenLabs HTTP chunk-size experiment

`aiter_bytes` chunk-size reduction was benchmarked and was neutral. Do not spend another cycle changing 1600→160/320/640 unless the transport implementation itself changes.

## DONE-4 — Flux μ-law8 vs linear16@48k benchmark

The encoding A/B was neutral for single-call latency. Keep raw μ-law as a **bandwidth/concurrency** research item only; do not sell it as a guaranteed p50 latency win.

## DONE-5 — prompt compaction first round

Large prompt reduction already saved roughly 100–200ms. More blind deletion is now lower ROI than active-state prompt slicing.

## DONE-6 — SentenceBuffer first-chunk threshold

`min_first_chars` has already been brought down aggressively. Do not simply send arbitrary token fragments to TTS.

## DONE-7 — response-cache/fastpath distinction is understood

Raw-LLM benchmarking can intentionally disable fastpaths, but production metrics must be reported separately.

---

# 2. P0 ACTIONABLES — eliminate whole LLM/network stages

These are the most important code changes because they save **hundreds of milliseconds to multiple seconds**, not 20–40ms micro-optimizations.

---

## A1 — Make `NextActionPolicy` the runtime controller

### Current code

`packages/dialogue/next_action_policy.py:1-22` explicitly says the policy is scaffolded and **NOT WIRED TO RUNTIME** in this snapshot.

It already models:

- phase;
- affect/style;
- known fields;
- missing fields;
- pending tools;
- required confirmation;
- next action;
- speech-act token budget;
- must-include facts.

### Change

At each authoritative user turn:

```text
DialogueState / booking state / transcript events
            ↓
ConversationDecisionState
            ↓
NextActionPolicy.decide()
            ↓
ExecutionPlan
```

Do **not** merely paste the selected action into the 19k-character prompt and still allow the LLM to choose a totally different operation.

Create an execution enum such as:

```text
DETERMINISTIC_SPEECH
LLM_SPEECH
CALL_TOOL
ASK_SLOT
PROPOSE_SLOT
CONFIRM_ACTION
ESCALATE
END_CALL
```

### Expected impact

- Simple stateful turns: **remove 1 LLM request (~0.6–1.2s)**.
- Tool turns: enables removal of additional model rounds below.
- Humanness: fewer repeated questions and fewer inappropriate generic acknowledgements.

### Files

- `packages/dialogue/next_action_policy.py`
- `packages/dialogue/reducer.py`
- `packages/core_agent/brain.py`
- `apps/api/app/routes/twilio_actor.py`

### Tests

Build node/action regression fixtures for:

- yes/no slot acceptance;
- time correction;
- missing phone;
- missing name;
- availability request;
- caller asks unrelated FAQ mid-booking;
- caller returns after silence;
- interruption;
- booking confirmation;
- goodbye.

---

## A2 — Remove the post-tool LLM round for deterministic tool results

### Current code

`packages/core_agent/brain.py:393+` loops over LLM/tool iterations.

`brain.py:710-766` explicitly allows the metadata `emit_semantic_plan` tool to fall through into another LLM iteration to get natural wording.

`brain.py:820` executes the real tool, and the loop can then ask the LLM again to verbalize the result.

### Replace with deterministic result renderers

Examples:

```text
check_availability → SlotProposalRenderer
book_appointment   → BookingConfirmationRenderer
cancel             → CancellationRenderer
lookup profile     → concise factual renderer
```

Example:

```python
slots = ["14:30", "16:00"]
→ "I've got two thirty or four. Which works better?"
```

No second LLM.

### Expected impact

**~0.6–1.5s saved per normal tool turn.**

### Files

- `packages/core_agent/brain.py:393-845`
- new `packages/dialogue/tool_result_renderer.py`
- `packages/dialogue/plan.py`

### Important existing evidence

`packages/dialogue/plan.py:163-167` already says `CONFIRM_ACTION` requires a deterministic template. The architecture is already pointing in this direction.

---

## A3 — Replace normal-case LLM `write_guard` with slot provenance

### Current code

`brain.py:791` invokes `validate_write()` before write tools.

`packages/core_agent/classifiers/write_guard.py` calls another LLM.

Therefore a booking can be:

```text
LLM #1 route
→ LLM #2 validate write
→ tool
→ LLM #3 wording
```

### Replace normal-case validation with evidence-bearing state

Each booking field should store:

```text
value
normalized_value
source_turn_id
source_text
confidence
confirmed_by_caller
last_corrected_at
```

Example:

```json
{
  "time": {
    "normalized": "14:30",
    "source_turn": 12,
    "source_text": "yeah, two thirty works",
    "confirmed": true
  }
}
```

A write is permitted only if deterministic predicates pass.

Use the LLM guard only as a fallback for ambiguous evidence, not as mandatory normal-case processing.

### Expected impact

**~0.6–1.2s saved on booking/write turns**, plus higher reliability.

### Files

- `packages/core_agent/classifiers/write_guard.py`
- `packages/dialogue/state.py` / reducer state
- booking tool adapters
- `packages/core_agent/brain.py`

---

## A4 — Split ACTION requests from SPEECH requests

### Current problem

The model is currently often given tools and permission to produce ordinary text in the same request. This is why a malformed internal control JSON can leak into caller-facing content and why `LEAKED_META` is necessary.

### New architecture

#### SPEECH lane

```text
tools = none
model may output only caller-facing language
```

#### ACTION lane

```text
tools = [only the relevant tool(s)]
tool_choice = required / named tool
caller-facing prose from this round is ignored
```

#### After tool

```text
deterministic renderer
OR
text-only LLM realization with tools disabled
```

### Benefits

- Makes raw tool JSON leakage structurally much harder.
- Reduces tool schema tokens.
- Reduces tool-choice ambiguity.
- Makes small/fast/local LLMs much more viable.
- Keeps `LEAKED_META` as the last airbag instead of the main safety mechanism.

### Expected direct latency impact

Likely **50–250ms on ordinary LLM calls**, but the real benefit is avoiding broken/retry turns that cost seconds.

### Files

- `packages/core_agent/brain.py:468-471`
- `apps/api/app/providers/llm/*`
- tool schema builder
- `twilio_actor.py` LEAKED_META guard stays.

---

## A5 — Remove `emit_semantic_plan` as a mandatory model-tool round where possible

### Current code

`brain.py:710-766` intercepts `emit_semantic_plan`, but if it is the only tool call the code intentionally loops back into the LLM for natural language.

### Research question

Once `NextActionPolicy` and reducer state are authoritative, determine whether `emit_semantic_plan` still needs to be an LLM-exposed tool at all.

Possible target:

```text
NextActionPolicy/semantic planner = application layer
LLM = realization layer
```

If the operation is deterministic, immediately render it.

### Expected impact

**~0.6–1.2s** on turns that currently emit only semantic metadata then require another model pass.

---

# 3. P0/P1 ACTIONABLE — speculative overlap rather than sequential waits

---

## A6 — Add speculative pre-synthesis with delayed release

This is one of the strongest unimplemented latency techniques.

### Current behavior

Flux Eager can start speculative LLM generation, but TTS generally starts once model text is accepted downstream.

### New pattern

```text
EagerEndOfTurn
    ↓
start speculative LLM
    ↓
first semantically safe text
    ↓
start ElevenLabs synthesis NOW
    ↓
BUFFER audio locally — DO NOT send to Twilio yet

if TurnResumed:
    cancel LLM + cancel TTS context + discard buffer

if final EndOfTurn:
    release already-generated audio immediately
```

### Why this is safer than speaking on Eager

You get the TTS overlap without risking the agent interrupting the caller based on a false EOT.

LiveKit exposes the same design tradeoff: preemptive LLM is enabled by default and optional preemptive TTS gives lower latency at the cost of wasted compute on cancellations.

### Expected impact

Potential **~150–400ms** depending on how much TTS can complete during the Eager→final interval.

### Files

- `apps/api/app/routes/twilio_actor.py`
- `packages/runtime/turn_manager.py`
- ElevenLabs session object
- generation cancellation/commit-lock subsystem

### Critical metrics

- Eager→Final delta
- speculative TTS first audio ready
- discarded speculative TTS rate
- TurnResumed rate
- useful audio release latency

---

## A7 — Dynamic Flux endpointing from expected input type

Deepgram now supports updating:

- `eot_threshold`
- `eager_eot_threshold`
- `eot_timeout_ms`
- keyterms

mid-stream via `Configure`, without reconnecting.

### Add to conversation state

```text
expected_input_type:
  YES_NO
  TIME
  DATE
  SLOT_SELECTION
  PHONE
  NAME
  EMAIL
  FREEFORM
  STORY
```

Then select profiles.

### Example starting profiles to A/B

**Yes/no / slot acceptance**

```text
eager 0.35–0.40
final 0.50–0.60
timeout 1000–2000ms
```

**Normal question**

```text
eager 0.40
final 0.70
timeout 3000–5000ms
```

**Name / phone / email / address dictation**

```text
eager 0.50–0.60
final 0.75–0.85
timeout 4000–6000ms
```

**Long free-form explanation**

```text
eager 0.55–0.65
final 0.75–0.85
longer timeout
```

These are experiment ranges, not production values.

### Expected impact

**50–300+ms depending on turn type**, with much better false-cutoff control than one global threshold.

### Files

- `apps/api/app/providers/stt/deepgram_flux_stt.py`
- `packages/dialogue/next_action_policy.py`
- reducer / expected-input state

---

## A8 — Lower Eager first, not final EOT globally

Current config is roughly:

```text
final = .7
eager = .5
```

Deepgram's own low-latency example uses:

```text
eager = .4
final = .7
```

Run a controlled A/B before touching final .7.

### Metrics

- Eager lead time over final
- TurnResumed %
- wasted LLM requests
- wrong speculative next actions
- p50/p95 EOT→useful speech

---

# 4. P1 — eliminate static artificial waiting rules

---

## A9 — Replace the fixed 2000ms structured-input merge window

### Current code

`apps/api/app/routes/twilio_actor.py:4063-4078`

Structured asks such as phone/name/address/email can widen the fragment merge window to **2000ms**.

That is intentionally safe, but it can dominate latency.

### Replace with slot-specific completion logic

**Phone:**
- digit/token parser;
- country/expected length;
- completion confidence;
- shorter silence once enough digits are present.

**Time:**
- parser recognizes `2:30`, `two thirty`, `half past two`;
- if canonical time is complete, no arbitrary 2s hold.

**Name:**
- use Flux final + semantic completion, but keep a longer fallback for halting callers.

**Email/address:**
- remain conservative.

### Expected impact

On relevant turns, **hundreds of milliseconds to >1 second**.

---

## A10 — Replace K1's long incomplete-word hold with contextual completion

### Current code

`twilio_actor.py:4113-4152` can hold incomplete-looking transcripts for up to roughly 2 seconds.

### Better logic

Use:

```text
expected_input_type
+ Flux EOT confidence
+ transcript punctuation
+ parser completeness
+ trailing token vocabulary
```

Examples:

- `"and"`, `"can you"` during free-form → hold.
- complete recognized phone/time/name → commit earlier.
- yes/no state + `"yeah"` → immediate.

### Expected impact

Large on false-held turns; neutral otherwise.

---

# 5. P1 — TTS architecture

---

## A11 — One ElevenLabs multi-context WebSocket per phone call

### Current code

- Shared HTTP client: good.
- `apps/api/app/providers/tts/elevenlabs_tts.py:133+` `ws_stream_synthesize()` still opens a WebSocket inside each synthesis call.

### Target

```text
call start → connect once
turn 1     → context A
turn 2     → context B
barge-in   → close context B
turn 3     → context C
call end   → close socket
```

ElevenLabs explicitly recommends one WebSocket per end-user session for this endpoint.

### Add instrumentation

- connection setup ms
- serving `x-region` for HTTP baseline
- context create→first audio
- turn 1 vs warm turns
- cancellation time

### Expected impact

Rough target to verify: **~100–200ms warm-turn improvement**, plus better interruption/prosody behavior.

---

## A12 — Log ElevenLabs serving region

ElevenLabs' global API now routes across US/Netherlands/Singapore and documents South Asia Flash+WS TTFB around 150–200ms.

Your measured ~300ms suggests there may still be application/network overhead.

For HTTP baseline, capture:

```text
x-region
request_start
headers_received
first audio byte
```

Research whether WebSocket handshake exposes equivalent routing information/logging.

### Decision

If Karachi is regularly hitting Singapore and still ~300ms, focus on local pipeline. If it is hitting a distant backend, provider support/routing may be worth pursuing.

---

## A13 — Decouple TTS receive from Twilio playback pacing

### Current risk

`_stream_tts_incremental()` consumes provider chunks and awaits outbound audio sends.

`_send_audio_frames_locked()` paces Twilio in realtime.

This means the TTS reader can stop reading provider data while the Twilio sender sleeps/paces.

### Target

```text
ElevenLabs reader task
    ↓ bounded AudioChunkQueue
Twilio playout task
    ↓ real-time pacing + mark ledger
```

Benefits:

- prevent TTS socket backpressure;
- smoother audio;
- cleaner cancellation;
- measurable queue depth;
- better multi-call behavior.

### Important

Do not simply blast audio into Twilio. Twilio buffers outbound media and warns that excessive buffering can overflow. Keep a bounded playout horizon and use `mark`/`clear`.

### Expected impact

Primarily p95/choppiness/concurrency; possible >50ms win in some streamed turns if current pacing blocks next provider chunk.

---

## A14 — Pre-render likely deterministic branch speech

For high-confidence states:

```text
Agent: "Does two thirty work?"
Possible next actions:
  ACCEPT
  REJECT
  CORRECT_TIME
```

Prewarm/cache likely phrases:

```text
"Great — I'll book that now."
"No problem. What time works better?"
```

Do not speak until branch authority is known.

This is a simple version of speculative TTS with almost zero model risk.

---

# 6. P1 — prompt/state architecture, not more prompt prose

---

## A15 — Active-node prompt slicing

Current rendered prompt is still about ~6k tokens / ~19k chars in recent tests.

Do not continue deleting hard rules blindly.

Instead split:

```text
GLOBAL CORE
- identity
- speech style
- compliance
- hallucination/tool truth
- booking truth invariants

ACTIVE NODE/TASK
- what is happening right now
- known/missing fields
- allowed actions
- expected input
- relevant examples

RELEVANT TOOLS ONLY
```

This mirrors current production platform design: Retell separates ordinary conversation nodes, tool subagents, deterministic function nodes and logic nodes; Bland uses global prompts plus node-local instructions.

### Experiment

Compare:

```text
full current prompt + all tools
vs
global core + current-state node + relevant tools
```

Measure:

- model TTFT
- tool accuracy
- repeated questions
- policy adherence
- input tokens

### Expected impact

Likely **100–400ms depending on provider/model/prompt cache**, plus better instruction following.

---

## A16 — Add `expected_input_type` and `response_mode` to NextAction state

Suggested additions:

```python
expected_input_type: YES_NO | PHONE | NAME | TIME | DATE | FREEFORM | ...
response_mode: DETERMINISTIC | LLM_TEXT | TOOL | RAG
allowed_tools: tuple[str, ...]
can_speculate: bool
can_pre_synthesize: bool
```

This one state object then controls:

- Flux thresholds;
- fragment merge windows;
- deterministic recognizers;
- tool narrowing;
- model selection;
- speculative generation;
- token budgets;
- TTS prewarm.

This is the central unifying system I would build.

---

# 7. P1/P2 — deterministic recognizers worth adding

These are not attempts to replace natural language. They are high-confidence states where using an LLM is unnecessary and slower.

## A17 — yes/no + confirmation recognizer

Handles:

- yes / yeah / sure / that's fine / works for me;
- no / nope;
- correction embedded in response.

Only active when current state expects a binary decision.

## A18 — offered-slot selection recognizer

If the previous agent turn offered:

```text
2:30 or 4:00
```

then:

```text
"the first one"
"two thirty"
"four"
"later one"
```

can resolve deterministically against the offered list.

## A19 — phone-number collector

Use a dedicated incremental grammar/normalizer instead of general LLM interpretation.

## A20 — time/date parser

Use deterministic parser + current timezone/calendar state for ordinary expressions.

LLM fallback only for genuinely ambiguous natural-language dates.

## A21 — greeting/hearing/are-you-there

Already has conversation-control fastpaths. Keep raw mode for benchmarks, but production should use them.

## A22 — goodbye / end-call

Deterministic high-confidence termination phrases avoid another LLM turn.

## A23 — human-transfer / emergency trigger

Use deterministic priority detection for obvious triggers, then policy-controlled response/escalation.

---

# 8. P1 RESEARCHABLE — local LLM lane

This is now worth serious experimentation because the voice server itself runs in Karachi. A local model removes the ~300–400ms public API RTT before model compute even starts.

## R1 — Qwen3.5-9B local, thinking OFF

Official Qwen guidance supports disabling thinking and says Qwen3.5 is strong at tool calling.

Serve through vLLM/SGLang and benchmark the **exact production prompt + exact tools**.

Research configurations:

- BF16/FP8/int4 as appropriate;
- context length capped to what voice needs;
- prefix/KV reuse;
- `enable_thinking=false`;
- vLLM Qwen3.5 MTP speculative decoding;
- no-tools speech lane vs action lane.

## R2 — Ministral 3 3B and 8B local

Mistral explicitly positions these for edge/local deployment and supports function calling and structured outputs.

Very interesting after NextActionPolicy reduces the model's role to short language realization.

## R3 — Gemma 4 12B local

Google says Gemma 4 12B can run locally with around 16GB VRAM/unified memory and is designed for agentic workflows. vLLM has explicit Gemma 4 MTP support.

Research as:

- speech-realization model;
- stronger local fallback;
- potentially direct audio-understanding R&D later, not current production path.

## R4 — vLLM speculative decoding

vLLM's current guidance says EAGLE/MTP/draft-model techniques offer the strongest latency reduction in low-QPS latency-focused workloads.

Voice calls are exactly that kind of workload.

Test:

```text
base
MTP if model supports it
EAGLE/draft model if available
n-gram/suffix cheap baseline
```

Measure **TTFT and inter-token latency**, not only tokens/sec.

### Critical benchmark rule

A local model wins only if it passes:

- tool/action correctness;
- next-action accuracy;
- no control leakage;
- humanness regression;
- p95 TTFT.

Do not pick a local model merely because it prints 150 tok/s.

---

# 9. P1 RESEARCHABLE — regional transport without throwing away the brain

---

## R5 — LiveKit India/Mumbai A/B

This is much more interesting than the old generic "try LiveKit" idea.

LiveKit Cloud currently documents:

- India realtime region group: Mumbai + South India;
- Middle East region group: Saudi Arabia + UAE;
- India SIP endpoint;
- Saudi SIP endpoint;
- agent deployment region `ap-south` = Mumbai.

### Experiments

**R5A — LiveKit number directly**

Use your LiveKit scaffolding with the same brain/providers.

**R5B — Twilio number → SIP trunk → LiveKit India endpoint**

Keep the customer-facing number/provider while moving the media layer closer.

**R5C — LiveKit media India + brain still Karachi**

Measure media benefit without moving the brain.

**R5D — LiveKit media + agent worker Mumbai later**

Only after local code-path work is stable.

### Metrics

- caller speech→server audio arrival;
- server audio→playout;
- jitter;
- packet loss;
- call setup;
- p50/p95 first useful audio.

This is an **A/B research item**, not permission to rewrite the agent around LiveKit.

---

## R6 — Twilio ConversationRelay as a control experiment

Twilio currently advertises internal ConversationRelay latency around p50 491ms / p95 713ms across differing model configurations and integrates STT/TTS/media while exposing a WebSocket to your application.

Do not replace your brain blindly.

Use it as a **benchmark control**:

```text
same business logic / same LLM where possible
Media Streams custom stack
vs
ConversationRelay managed speech/media path
```

If it is dramatically faster, identify which part of your transport/orchestration is responsible.

---

## R7 — Cloudflare Tunnel route A/B

Current Tunnel is persistent and can use QUIC/HTTP2 to Cloudflare.

Research:

1. upgrade `cloudflared` to a current version;
2. run `cloudflared tunnel diag`;
3. verify UDP/QUIC path is actually healthy;
4. A/B QUIC vs HTTP/2 connector protocol if supported/configurable;
5. A/B Argo Smart Routing;
6. temporary controlled direct WSS endpoint to quantify tunnel overhead.

### Rule

If improvement is <50ms p50 and p95 is unchanged, stop working on it.

Cloudflare itself says Argo gains are most visible when clients are far from the origin, so it is worth measuring with Karachi origin—but no guaranteed saving should be assumed.

---

## R8 — alternative telephony/SIP POPs

Research providers with India/Middle East/Singapore media ingress, not just AI providers with fast model compute.

The metric is:

```text
Karachi handset → media ingress → your brain → media egress → handset
```

not provider marketing TTFT.

Candidate research:

- LiveKit SIP India/Saudi;
- Telnyx regional SIP/media footprint;
- existing Twilio number routed through SIP if commercially viable.

---

# 10. P2 — alternate TTS research

ElevenLabs Flash is currently good enough and should not distract from architecture. But these are valid R&D lanes after multi-context WS lands.

## R9 — Rime Arcana v3 cloud

Rime claims about ~200ms cloud TTFB for Arcana v3 and 120ms model latency, with strong conversational quality.

Problem: its documented direct cloud endpoints are North America, so Karachi routing must be measured.

## R10 — Rime Mist v3 local/on-prem

Rime reports ~40ms p90 first byte on L40S/RTX 6000 for Mist v3.

This is potentially transformative if licensing/deployment economics make sense for a client-scale deployment.

Research only after current TTS session architecture is fixed.

## R11 — existing Cartesia adapter

Because the codebase already has alternative TTS support, run a modern equivalent-quality voice A/B only after ensuring:

- persistent streaming;
- same output codec;
- same sentence segmentation;
- same voice-quality scoring.

---

# 11. P1/P2 — SmartTurn and endpointing architecture

---

## A24 — Stop polling SmartTurn 5×/second per call

### Current code

`twilio_actor.py:741-785` runs a SmartTurn worker roughly every 200ms.

That scales approximately as:

```text
10 calls → 50 evaluations/s
20 calls → 100 evaluations/s
```

Pipecat's current Smart Turn architecture uses turn detection as an end-of-turn strategy combined with VAD/transcription, rather than treating it as a permanently-running poller.

### Target

Use SmartTurn when:

```text
VAD/Flux reports silence or boundary ambiguity
AND
semantic state does not already make completion obvious
```

### Benefit

Mostly CPU/p95/concurrency, plus fewer contradictory endpointing signals.

---

## A25 — Decide which layer owns final turn authority

The code currently combines:

- Flux native events;
- SmartTurn;
- K1 incomplete-word logic;
- fragment merge windows;
- structured-input capture;
- speculative commit locks.

Write one explicit authority table.

Example:

| State | Authority |
|---|---|
| Flux Eager | speculative only |
| Flux EndOfTurn | final unless structured parser says incomplete |
| expected yes/no + valid deterministic parse | immediate final |
| phone/email capture | structured parser + final EOT |
| non-Flux fallback | SmartTurn + STT final |

This reduces race conditions and latency from stacking multiple independent "are they done?" systems.

---

# 12. P1/P2 — async runtime and concurrency

---

## A26 — Move synchronous DB persistence off the media event loop

### Current code

`apps/api/app/core/session_manager.py:216`, `290`, `305`, etc. use synchronous SQLAlchemy/SQLite operations from async call workflows.

### Immediate local solution

Keep SQLite if desired, but use:

```text
bounded persistence queue
or
asyncio.to_thread()
```

for noncritical persistence.

Booking transaction writes must preserve ordering and failure semantics.

### Impact

Single-call p50 may barely move; multi-call p95 can improve dramatically if disk/DB stalls currently block the Uvicorn event loop.

---

## A27 — Bound/coalesce Deepgram event queue

### Current code

`apps/api/app/providers/stt/deepgram_flux_stt.py:125` uses an unbounded event queue.

### Policy

- `Update`/partial transcripts may be coalesced to latest;
- `StartOfTurn`, Eager, TurnResumed, EndOfTurn, errors must be preserved.

Add queue-depth metrics.

---

## A28 — Reduce the STT audio backlog ceiling

### Current code

`streaming_stt_bridge.py:91` has `Queue(maxsize=150)`.

At ~20ms Twilio frames this is around **3 seconds** of potential stale audio.

For realtime voice, 3 seconds is still too much.

Measure backlog in milliseconds and target a much smaller normal operating window.

Suggested research starting point:

```text
warning 250ms
severe 500ms
hard recovery ~1000ms
```

The exact drop/reconnect policy must be tested for transcript damage.

---

## A29 — One process-wide event-loop lag monitor

The actor currently creates a per-call loop-lag watchdog.

One asyncio process has one event loop, so a process-wide monitor is enough.

This is primarily scale cleanup.

---

## A30 — Outbound audio horizon / playout governor

Track:

```text
queued_audio_ms
sent_not_acknowledged_ms
last_mark_ack
```

Do not allow the agent to queue seconds of stale speech ahead of Twilio.

On barge-in:

```text
cancel source
clear Twilio
reset local queue
cancel/close TTS context
```

This should become a first-class playout controller rather than scattered media sends.

---

# 13. P1 — real concurrency capacity

---

## A31 — Build the real N=1/5/10/20 call load test

`scripts/multi_call_probe.py` is intentionally not a true AI load test.

Create a harness that streams prerecorded μ-law speech at real-time pace through the actual WebSocket interface.

### Test patterns

1. Normal staggered conversation.
2. Synchronized EOT burst.
3. 20 callers interrupting during agent speech.
4. Tool burst (availability requests simultaneously).
5. Booking write burst.

### Metrics

- active calls;
- CPU/RSS;
- event-loop lag;
- STT backlog ms;
- SmartTurn queue/inference;
- LLM inflight and provider limits;
- TTS inflight/context count;
- outbound audio queue ms;
- DB queue latency;
- p50/p95/p99 first useful audio;
- duplicate/stale response count;
- cross-call leakage.

Only after this test should the product claim a simultaneous-call capacity.

---

## A32 — Add a local `CapacityGovernor`

After measuring the safe ceiling:

```text
MAX_ACTIVE_CALLS
MAX_SIMULTANEOUS_LLM
MAX_SIMULTANEOUS_TTS
MAX_SMARTTURN_INFERENCE
```

Reject/overflow new calls gracefully before an overloaded process destroys latency for all existing calls.

---

# 14. P1 — provider-adapter modernization

Your alternative provider adapters are not currently equivalent to OpenAI/Groq for latency testing.

### Current examples

- `together_llm.py`: creates a fresh `AsyncClient` per request.
- `cerebras_llm.py`: fresh client.
- `fireworks_llm.py`: fresh client.
- `mistral_llm.py`: even its streaming path creates a fresh client.
- `gemini_llm.py`: fresh client and no equivalent native production streaming path in this adapter.
- OpenAI/Groq already have shared clients and real streaming.

## A33 — Create `SharedStreamingLLMTransport`

For OpenAI-compatible providers:

```text
one HTTP/2 AsyncClient/process
keepalive
native SSE stream parser
streamed tool-call reconstruction
request-id telemetry
rate-limit telemetry
TTFT instrumentation
cancellation
```

Then adapters supply only:

```text
base URL
headers
model quirks
reasoning/thinking flags
schema quirks
```

This makes future provider tournaments fair.

---

## R12 — Native Gemini streaming adapter

Direct Karachi tests already show Flash-Lite around the same ~600–650ms class as your fastest OpenAI result. Keep it as a real redundancy candidate, but benchmark with paid-production limits and a native streaming adapter.

Do not judge it through a buffered compatibility path.

---

## R13 — Provider routing/region telemetry

For every provider capture where possible:

```text
DNS/connect/TLS
request headers sent
headers returned
first model delta
serving region header
request ID
rate-limit remaining
```

This will tell you whether a 900ms turn is network, queueing, prefill, reasoning, or model compute.

---

# 15. P1/P2 — measuring actual human-perceived latency correctly

---

## A34 — Define canonical latency spans

Do not use a single ambiguous `STT_FINAL→ACK` metric.

Track:

```text
USER_AUDIO_LAST_FRAME       # closest server-side mouth-end proxy
FLUX_EAGER
FLUX_END_OF_TURN
POLICY_DECISION
LLM_REQUEST_START
LLM_FIRST_DELTA
FIRST_SAFE_TEXT
TOOL_START
TOOL_DONE
TTS_REQUEST_START
TTS_FIRST_AUDIO_READY
FIRST_MEDIA_SENT
TWILIO_FIRST40_ACK
```

Derived:

```text
endpointing_ms
policy_ms
llm_ttft_ms
semantic_text_delay_ms
tts_ttfb_ms
first_media_ms
Twilio_playout_ack_ms
```

Also separate:

```text
first_any_audio
first_useful_answer_audio
```

A filler is not an answer.

---

## A35 — Stamp every call with its exact runtime version

At call start log:

```text
git SHA
prompt SHA
prompt characters/tokens
model/provider
provider mode/reasoning
Flux thresholds
TTS model/voice/settings
cache mode
NextActionPolicy version
feature flags
```

Without this, old calls become impossible to compare reliably after rapid iteration.

---

## A36 — Acoustic mouth-to-ear benchmark

The server cannot know literal sound-at-handset time.

Create a repeatable physical test:

```text
speaker plays known waveform / phrase into caller phone
record caller-side output on second device/interface
cross-correlate waveforms
```

Use this for a small set of releases to calibrate server-side proxy metrics.

---

## A37 — Speech-act latency dashboards

Do not aggregate all turns into one p50.

Break out:

```text
greeting
FAQ
acknowledgement
yes/no
ask slot
availability tool
booking commit
correction
barge-in
free-form answer
```

The architecture should achieve sub-second on simple state transitions even if complex FAQ/tool turns remain slower.

---

# 16. HUMANNESS research that directly affects latency

---

## A38 — Adaptive interruption instead of raw VAD interruption

Industry systems increasingly distinguish:

```text
real interruption
vs
backchannel: "yeah", "uh-huh", "right"
vs
background voice/noise
```

Your state layer should classify interruption intent before killing long speech unnecessarily.

Research using:

- current STT transcript;
- speech duration;
- whether agent asked a question;
- expected input type;
- current speaking progress.

This reduces stop/restart churn, which is both a humanness and latency problem.

---

## A39 — False interruption recovery

If agent is stopped by noise but no meaningful transcript appears:

```text
resume prior speech
or
continue from a natural boundary
```

Do not regenerate the whole answer through the LLM.

---

## A40 — ASR semantic repair policy

If transcript is semantically weird for the business, do not repeat it confidently.

State should mark:

```text
uncertain_span
trusted_span
```

Then use a short targeted clarification.

This prevents repair loops that add entire extra turns.

---

## A41 — One conversational move per turn

Enforce at policy level, not only prompt level.

Examples:

```text
ASK_PHONE
```

not:

```text
acknowledge + explain + ask phone + ask preferred date
```

This reduces generation length and makes TTS start earlier.

---

# 17. R&D ideas that may become novel differentiators

These are not first-week tasks, but they are genuinely interesting systems once P0/P1 is stable.

---

## R14 — Turn-latency optimizer policy

Teach the policy to choose not only *what* to do next but the cheapest safe execution mode:

```text
DETERMINISTIC
CACHE
LOCAL_SMALL_LLM
REMOTE_FAST_LLM
REMOTE_STRONG_LLM
TOOL
RAG
```

Objective:

```text
minimize expected latency
subject to correctness/risk constraints
```

This can eventually be learned from your call/eval dataset.

---

## R15 — Expected-turn speculative branch engine

When state strongly constrains the next user response, precompute likely branches.

Example:

```text
waiting_for_slot_acceptance
branches:
  ACCEPT 0.65
  REJECT 0.20
  CORRECT 0.15
```

Pre-render/pre-synthesize the high-probability branches and discard unused ones.

Measure compute cost vs p50 reduction.

---

## R16 — Semantic prefix streaming

Instead of generic punctuation buffering, the policy/renderer can emit an immutable prefix:

```text
"I've got two options —"
```

while more complex wording is still generated.

Only use prefixes that cannot be invalidated by the pending model/tool result.

---

## R17 — local micro-model for classification, remote model for prose

Run a tiny local classifier for:

- expected intent;
- yes/no;
- correction;
- slot selection;
- whether tools are needed;
- caller affect/style.

This can make policy decisions in tens of milliseconds while the remote model is reserved for actual language generation.

Could be fine-tuned on your own call dataset later.

---

## R18 — state-dependent model routing

After evals exist:

```text
simple realization → local 3B/8B
complex conversational answer → remote fast model
high-risk ambiguous action → stronger model
```

Do not route merely on token count; route on action risk.

---

## R19 — direct audio-language local model research

Gemma 4 12B now supports audio input locally. Research whether an audio-language classifier could handle:

- caller affect;
- interruption classification;
- semantic EOT;
- yes/no/backchannel distinctions;

without replacing the production STT→LLM→TTS pipeline.

Use it as an auxiliary model first.

---

## R20 — per-client latency routing policy

Eventually store client deployment profiles:

```text
US caller base → US media/AI
Pakistan/India caller base → India/Middle East media/AI
EU → EU media/AI
```

The deployable product can choose transport/provider regions without changing business logic.

---

# 18. Recommended experiment queue

## Wave 1 — remove seconds, not milliseconds

1. Finish `NextActionPolicy` runtime wiring.
2. Deterministic post-tool renderers.
3. Slot provenance validator replacing normal write-guard LLM.
4. ACTION vs SPEECH model-call split.
5. Remove redundant `emit_semantic_plan` second-pass cases.

### Exit criterion

Normal booking acceptance/commit no longer needs 2–3 LLM roundtrips.

---

## Wave 2 — overlap

6. Eager .4 / final .7 A/B.
7. Speculative LLM + pre-synthesized TTS held until final authority.
8. One ElevenLabs multi-context WS per call.
9. Dynamic Flux profiles from `expected_input_type`.

### Exit criterion

Routine LLM turn p50 approaches ~1–1.5s server-observable first-media territory without premature interruptions.

---

## Wave 3 — remove hidden waits

10. Replace 2s structured merge waits with slot-specific completion.
11. Replace K1 fixed hold with state-aware completion.
12. Active-node prompt/tool slicing.
13. Event-driven SmartTurn.

---

## Wave 4 — runtime/concurrency

14. DB persistence off event loop.
15. Bounded/coalesced STT event queues.
16. Smaller/observable STT audio backlog.
17. TTS reader → outbound playout queue separation.
18. Process-wide loop-lag monitor.
19. Real N=1/5/10/20 spoken-call load test.
20. CapacityGovernor.

---

## Wave 5 — geography/provider R&D

21. LiveKit India direct-number A/B.
22. Twilio → LiveKit India SIP A/B.
23. Cloudflare QUIC/Argo/direct-route A/B.
24. Native Gemini production adapter.
25. Local Qwen/Ministral/Gemma voice-lane tournament.
26. Rime TTS A/B if current EL TTS still materially contributes after persistent WS.
27. ConversationRelay benchmark control.

---

# 19. Benchmark matrix every candidate must pass

Every provider/model/architecture experiment should run the same cases.

## Conversation cases

1. "Can you hear me?"
2. "What services do you offer?"
3. "Do you do implants?"
4. "I'd like an appointment tomorrow."
5. "Two thirty works."
6. "Yeah, sure." in a known confirmation state.
7. "No, I said two thirty."
8. Caller gives phone digits with pauses.
9. Caller explains a problem in a long free-form utterance.
10. Caller interrupts the agent halfway through a sentence.
11. Availability tool call.
12. Booking commit.
13. Tool failure.
14. FAQ during booking then return to task.

## Metrics

```text
turn-detection p50/p95
first action decision
LLM first useful delta
valid tool/action %
raw JSON/meta leak %
first safe speech text
TTS first audio
first media sent
Twilio playout ACK
mouth-to-ear on acoustic sample tests
cancellation recovery
wrong interruption %
caller repetition/repair rate
```

## Quality gates

A change does not ship merely because it saves 150ms if it causes:

- more false cutoffs;
- tool errors;
- repeated questions;
- confirmation mistakes;
- more caller repairs;
- choppy TTS.

---

# 20. Priority scorecard

| ID | Change | Latency upside | Reliability/humanness | Effort | Priority |
|---|---|---:|---:|---:|---:|
| A1 | Runtime NextActionPolicy | 0.6–1.2s simple turns | Very high | M | **P0** |
| A2 | Deterministic post-tool renderer | 0.6–1.5s tool turns | High | M | **P0** |
| A3 | Provenance write validator | 0.6–1.2s booking | Very high | M | **P0** |
| A4 | ACTION/SPEECH split | 50–250ms + avoids multi-sec failures | Very high | M | **P0** |
| A5 | Remove semantic-plan redundant pass | 0.6–1.2s affected turns | High | S/M | **P0** |
| A6 | speculative TTS held until final EOT | 150–400ms | High if cancel-safe | M | **P0/P1** |
| A7 | dynamic Flux profiles | 50–300+ms | High | M | **P1** |
| A9 | structured-input wait removal | 0.3–1.5s affected turns | High | M | **P1** |
| A10 | K1 contextual completion | 0.2–2s pathological turns | High | M | **P1** |
| A11 | call-long EL WS | 100–200ms | Medium/high | M | **P1** |
| A15 | active-node prompt slicing | 100–400ms possible | Very high | M/L | **P1** |
| A24 | event-driven SmartTurn | p50 small, p95/scale high | High | M | P1/P2 |
| A26 | DB off event loop | p50 small, p95 high | High | S/M | P1/P2 |
| A31 | real load harness | measurement | Essential | M | **P1** |
| R1-R4 | local LLM lane | 0.3–0.6s network avoided | TBD | M | **Research high** |
| R5 | LiveKit India transport | potentially hundreds ms | High | M | **Research high** |
| R6 | ConversationRelay control | unknown | learning value high | M | Research |
| R9-R10 | Rime TTS | 100–250ms possible | voice-dependent | M | Research |

---

# 21. What to explicitly NOT do next

1. Do not chase another 20–40ms Python micro-optimization while booking still has multiple model roundtrips.
2. Do not lower Flux final EOT globally to .5 before testing Eager/dynamic profiles.
3. Do not stream arbitrary half-sentences into TTS.
4. Do not add more filler phrases to hide latency.
5. Do not blindly delete prompt rules; move state/task rules into active nodes instead.
6. Do not judge Together/Cerebras/Fireworks/Mistral using adapters that recreate HTTP clients or buffer output.
7. Do not pick models by tokens/sec. Voice cares about first useful action/text + correctness + p95.
8. Do not add Uvicorn workers while actor/session state remains process-local.
9. Do not treat the existing multi-call silence probe as a production capacity test.
10. Do not call Twilio mark ACK literal caller-ear time.
11. Do not replace the custom brain with Vapi/Retell/LiveKit just because their demo is faster; extract their architectural techniques.

---

# 22. External research findings that validate this roadmap

## Deepgram Flux

Current documentation confirms:

- final EOT threshold .5-.9, default .7;
- eager threshold .3-.9;
- timeout 500–60000ms;
- Eager can reduce E2E by hundreds of milliseconds;
- speculative Eager can increase LLM calls 50–70%;
- all thresholds can be changed mid-stream without reconnecting;
- their low-latency example uses eager .4 / final .7.

Sources:
- https://developers.deepgram.com/docs/flux/quickstart
- https://developers.deepgram.com/docs/flux/configuration
- https://developers.deepgram.com/docs/flux/configure
- https://developers.deepgram.com/docs/flux/voice-agent-eager-eot

## LiveKit

Current docs confirm:

- preemptive LLM generation enabled by default;
- optional preemptive TTS;
- adaptive interruption and false-interruption recovery;
- India realtime region: Mumbai + South India;
- Middle East: Saudi Arabia + UAE;
- India/Saudi SIP regional endpoints;
- agent deployment in Mumbai (`ap-south`).

Sources:
- https://docs.livekit.io/agents/logic/turns/tuning/
- https://docs.livekit.io/reference/agents/turn-handling-options/
- https://docs.livekit.io/deploy/admin/regions/endpoints/
- https://docs.livekit.io/telephony/features/region-pinning/

## ElevenLabs

Current docs confirm:

- one multi-context WebSocket per end-user session is the recommended design;
- Flash is the latency model;
- South Asia Flash+WebSocket expected TTFB roughly 150–200ms;
- global routing currently uses USA, Netherlands and Singapore;
- `x-region` can identify the backend on HTTP requests.

Sources:
- https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/multi-context-web-socket
- https://elevenlabs.io/docs/developer-guides/reducing-latency
- https://elevenlabs.io/blog/text-to-speech-api-up-to-40-faster-globally

## Industry flow architecture

Retell currently separates:

- conversation nodes without tools;
- subagent nodes with tools;
- deterministic function nodes;
- logic nodes.

Bland Pathways similarly separates node-local dialogue, static speech, variables and webhooks and has node-level regression testing.

Vapi exposes smart endpointing, punctuation/number-specific timing and separate start/stop-speaking plans.

These designs independently validate moving your monolithic LLM turn into a state/policy/operation pipeline.

Sources:
- https://docs.retellai.com/build/conversation-flow/overview
- https://docs.retellai.com/build/conversation-flow/function-node
- https://docs.bland.ai/tutorials/pathways
- https://docs.bland.ai/tutorials/standards
- https://docs.vapi.ai/customization/voice-pipeline-configuration

## Local inference

Current vLLM docs emphasize MTP/EAGLE/draft-model speculative decoding as particularly useful for low-QPS latency-focused workloads. Qwen3.5 supports disabling thinking and tool calling; Mistral's Ministral 3 family is explicitly edge-oriented with function calling/structured outputs; Google says Gemma 4 12B can run locally on dedicated 16GB-class hardware and supports agentic local workflows.

Sources:
- https://docs.vllm.ai/en/latest/features/spec_decode/
- https://huggingface.co/Qwen/Qwen3.5-9B
- https://docs.mistral.ai/models/ministral-3-3b-25-12
- https://developers.googleblog.com/gemma-4-12b-the-developer-guide/

## TTS R&D

Rime reports Arcana v3 at roughly 200ms cloud TTFB and Mist v3 at roughly 40ms p90 on suitable on-prem GPUs. These are worth future A/Bs only after the current ElevenLabs connection/session architecture is fixed.

Sources:
- https://www.rime.ai/resources/arcana-v3
- https://www.rime.ai/resources/introducing-mist-v3-enterprise-tts

## Cloudflare

Cloudflare says Argo Smart Routing optimizes paths between edge and origin and benefits users far from the origin; Tunnel uses persistent connections and can use QUIC/HTTP2. This makes a controlled Argo/QUIC/direct-route A/B worthwhile, but it should be dropped if it does not save at least ~50ms or materially improve p95.

Sources:
- https://developers.cloudflare.com/argo-smart-routing/
- https://developers.cloudflare.com/tunnel/
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/

---

# 23. Recommended ownership between Claude Code chats

## Speed / humanness / brain chat

Own:

- A1 NextActionPolicy runtime
- A2 deterministic tool renderer
- A3 provenance write validation
- A4 ACTION/SPEECH split
- A5 semantic-plan redundant-pass removal
- A14 branch pre-rendering
- A15 active-node prompt slicing
- A16 expected-input / response-mode state
- A17-A23 deterministic recognizers
- A38-A41 humanness/state behaviors

## Networking / realtime chat

Own:

- A6 speculative TTS buffering/release plumbing
- A7/A8 dynamic Flux configuration
- A9/A10 timing waits in actor (coordinate carefully)
- A11-A13 TTS session/transport
- A24/A25 turn-authority architecture
- A26-A30 async queues/playout/runtime
- A31/A32 concurrency harness/governor
- A33 provider transport base
- A34-A37 telemetry
- R5-R8 transport/geography experiments

## Shared-file collision warning

Both tracks heavily touch:

- `apps/api/app/routes/twilio_actor.py`
- `packages/core_agent/brain.py`

Use small commits and rebase between batches rather than simultaneous broad rewrites.

---

# 24. The single architecture target I would use as the north star

Create a `RealtimeConversationKernel` around existing pieces rather than another monolith.

Conceptually:

```text
RealtimeConversationKernel
├── TurnAuthority
│   ├── Flux events
│   ├── structured parser
│   └── SmartTurn fallback
│
├── ConversationReducer
│   └── immutable state + provenance
│
├── NextActionPolicy
│   ├── expected input type
│   ├── response mode
│   ├── allowed tools
│   ├── speculation policy
│   └── delivery intent
│
├── ActionExecutor
│   ├── deterministic recognizers
│   ├── business tools
│   ├── RAG
│   └── LLM lane
│
├── ResponseRealizer
│   ├── deterministic templates
│   └── text-only LLM
│
├── SpeculativePipeline
│   ├── eager LLM
│   ├── eager TTS buffer
│   └── commit/cancel
│
└── SpeechOutputController
    ├── persistent TTS session
    ├── bounded audio queue
    ├── Twilio playout ledger
    └── barge-in clear/cancel
```

Most of these components already exist in partial form. The work is **making ownership explicit and removing duplicated decision-making**.

That is the route to a genuinely fast agent while keeping your custom brain rather than rebuilding on someone else's framework.
