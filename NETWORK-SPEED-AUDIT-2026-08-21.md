# Receptionist Agent — Local Networking & Speed Audit
## 2026-08-21 — Karachi / Cloudflare / no new server

**Scope:** Current `receptionist-agent-audit-lean-2026-08-21_0153` codebase and latest bundled call logs.  
**Hard constraint:** Keep deployment on the current Karachi machine behind Cloudflare for now. No Azure/VPS/US-East migration is required by this plan.  
**Primary goals:** Reduce real turn latency, improve audio continuity/humanness, remove network/runtime correctness bugs, and make local multi-call testing trustworthy.

---

# Executive verdict

The network stack does **not** need a rewrite.

The core architecture is directionally good:

```text
Twilio persistent WSS
→ per-call actor/mailbox
→ persistent Deepgram WSS
→ OpenAI shared HTTP/2 client
→ ElevenLabs streaming
→ Twilio WSS
```

The remaining problems are mostly **execution-path bugs and avoidable local overhead**, not a requirement for another server.

The highest-value findings are:

1. **Flux EndOfTurn currently enters the turn machine twice.**
2. **SmartTurn is classifying malformed audio.**
3. **Filler audio can overlap the real answer.**
4. **Flux is unnecessarily upsampled from μ-law 8 kHz to linear16 48 kHz even though current Flux supports raw μ-law 8 kHz.**
5. **The STT backpressure queue can hold ~16 seconds of stale phone audio.**
6. **FIRST40/caller-latency telemetry is semantically wrong and currently produces impossible negative durations.**
7. **ElevenLabs still creates one synthesis request per sentence; the right next transport architecture is one multi-context TTS WebSocket per call.**
8. **Synchronous DB work still runs inside async call paths and can stall every call on the single Uvicorn event loop.**
9. **SmartTurn + event-loop watchdog design scales CPU work linearly per call even when a caller is idle.**
10. **The Twilio Media Stream WebSocket is accepted without validating Twilio's signature.**
11. **Current OpenAI-only constraints do not prevent fast-lane routing; use deterministic/cache/no-tools/full-tools lanes with the same OpenAI account.**
12. **The existing `n=10` multi-call probe is explicitly not a real voice load test.**

---

# P0 — fix before trusting the next latency/concurrency benchmark

## NET-01 — Flux `EndOfTurn` is double-injected into TurnManager

### Evidence

`apps/api/app/providers/stt/deepgram_flux_stt.py:241-258`

On one Flux `EndOfTurn` message, provider code emits:

```python
STTEvent(kind="final", ..., speech_final=True)
STTEvent(kind="end_of_turn", ..., speech_final=True)
```

Then:

`apps/api/app/routes/twilio_actor.py:884-900`

routes `final` to `_on_stt_final` and `end_of_turn` to `_on_stt_native_turn`.

The normal final path enters:

`packages/runtime/turn_manager.py:409-516`

and emits:

```text
EAGER_END_OF_TURN
→ speculative brain
→ 400 ms confirmation task
```

Then milliseconds later the native `end_of_turn` path enters:

`packages/runtime/turn_manager.py:306-318`

and emits:

```text
END_OF_TURN
```

So a single **already-final Flux EndOfTurn** can trigger:

```text
Flux EndOfTurn
├─ synthetic "final"
│  └─ TurnManager EAGER
│     └─ speculative brain / commit-lock
└─ native end_of_turn
   └─ END_OF_TURN
```

Latest real logs visibly show the same `STT_FINAL` twice for one phrase.

### Why it matters

This adds:

- unnecessary speculative OpenAI calls;
- unnecessary commit-lock claims/releases;
- more cancellation races;
- duplicate telemetry;
- harder reasoning about turn ownership;
- more paid OpenAI work;
- more opportunities for double-response regressions.

### Fix

For Flux, treat its native state machine as authoritative.

Preferred:

```text
Update          → partial
StartOfTurn     → speech_start
EagerEndOfTurn  → eager_end_of_turn
TurnResumed     → turn_resumed
EndOfTurn       → end_of_turn (with final transcript)
```

Do **not** also emit a normal `final` for Flux `EndOfTurn`.

If some downstream logic requires storing the final transcript, make the native `end_of_turn` handler update the same transcript/timestamp state directly instead of routing it through Nova's heuristic-final path.

### Tests

Add one deterministic test:

```text
one provider TurnInfo(event=EndOfTurn)
→ exactly one CONTROL END_OF_TURN
→ zero synthetic EAGER events
→ one brain dispatch
```

And:

```text
EagerEndOfTurn
→ one speculative dispatch
TurnResumed
→ cancel
EndOfTurn
→ one committed dispatch
```

---

## NET-02 — SmartTurn receives malformed audio

### Evidence

`packages/runtime/streaming_stt_bridge.py:123-168`

The bridge correctly keeps incoming Twilio audio as raw μ-law for Deepgram:

```python
payload = frame
```

But SmartTurn then does:

```python
pcm16k = audioop.ratecv(payload, 2, 1, 8000, 16000, None)[0]
```

`payload` is still μ-law bytes.

`ratecv(... width=2 ...)` expects linear 16-bit PCM.

The required μ-law decode is missing.

The comment at lines ~159-163 says input is already linear after conversion, but the conversion was deliberately removed from the Deepgram hot path earlier.

### Correct conversion

```python
lin8k = audioop.ulaw2lin(frame, 2)
pcm16k, self._smartturn_rate_state = audioop.ratecv(
    lin8k,
    2,
    1,
    8000,
    16000,
    self._smartturn_rate_state,
)
```

Keep `rate_state` persistent across frames.

### Why this is load-bearing

SmartTurn affects whether the user:

- gets cut off;
- waits during pauses;
- gets premature speculation;
- gets a delayed final turn.

Malformed audio therefore damages both **speed** and **humanness**.

It also invalidates attempts to tune SmartTurn thresholds based on current results.

### Tests

Feed a known μ-law fixture and compare:

- decoded PCM length;
- nonzero signal;
- expected waveform/RMS;
- deterministic SmartTurn score range.

---

## NET-03 — filler and real answer can overlap on the Twilio wire

### Evidence

Filler:

`apps/api/app/routes/twilio_actor.py:4197-4242`

Real answer filler task is spawned:

`apps/api/app/routes/twilio_actor.py:4313-4322`

But it is only cancelled in `_run_brain_from_text`'s final cleanup:

`apps/api/app/routes/twilio_actor.py:4451-4458`

The filler itself sends directly through:

```python
await self._send_audio_frames(audio, mime)
```

There is no single shared outbound speech/audio arbiter.

The real answer concurrently reaches `_stream_tts_incremental()` and also calls `_send_audio_frames()`.

Newest call `CA83f5...`:

```text
00:53:48.508 filler starts: "Yep, on it."
00:53:48.687 real answer TTS_STREAM_START
```

Only ~179 ms separates them.

The cached filler clip is hundreds of milliseconds long.

### Consequence

Frames from:

```text
filler producer
+
real answer producer
```

can compete/interleave on the same Twilio WebSocket.

This is a direct digital explanation for:

> "voice is breaky"

and is more actionable than blaming mobile echo first.

### Fix

Create one per-call `OutboundAudioArbiter` / `SpeechOutputQueue`.

Only it may send Twilio `media`.

Producers submit:

```text
generation
kind = filler | answer | greeting | backchannel
priority
audio stream/chunks
cancel token
```

Rules:

- answer for current generation supersedes pending filler;
- filler may never run concurrently with answer;
- barge-in cancels current source and sends `clear`;
- stale generations are rejected;
- marks are generated centrally.

### Immediate minimal patch

Before full arbiter:

- cancel filler as soon as the first safe real answer sentence is ready, not after the whole brain call;
- if filler has begun, wait for it to finish or explicitly clear/cancel it before sending answer;
- never let `_play_cached_backchannel()` and `_stream_tts_incremental()` call `_send_audio_frames()` concurrently.

### Filler timing

700 ms is too aggressive for current raw-LLM calls if the actual answer often becomes speakable only ~100–300 ms later.

A better temporary policy:

```text
routine conversational turn → no filler
tool/wait operation → context-specific bridge
slow-brain fallback → ~1000–1200 ms
```

Do not "always acknowledge immediately." That adds another conversational move and can make the agent more robotic.

---

## NET-04 — STT audio backpressure is 16 seconds, not 10 seconds

### Evidence

`packages/runtime/streaming_stt_bridge.py:77-80`

```python
asyncio.Queue(maxsize=800)
```

Twilio sends roughly one 20 ms frame per queue item.

Therefore:

```text
800 × 20 ms = 16,000 ms
```

not 10 seconds.

### Why it matters

A realtime voice system should never consider 16 seconds of queued caller audio acceptable.

If Deepgram or the network stalls, the app can continue processing extremely stale audio long after the human has moved on.

### Fix

Track backlog in time:

```python
stt_audio_backlog_ms = queue.qsize() * 20
```

Suggested first cap:

```text
hard queue: 50–100 frames = 1–2 sec
warning: >250–500 ms
```

When over the target backlog:

- discard oldest frames aggressively until back near target;
- record dropped milliseconds;
- if sustained, reconnect/fail over rather than "catching up" old speech.

Do not wait until 16 seconds before pressure becomes visible.

---

## NET-05 — Twilio WSS is accepted without signature validation

### Evidence

HTTP webhook validates Twilio:

`apps/api/app/routes/twilio.py:139-171`

But media WSS:

`apps/api/app/routes/twilio.py:562-576`

starts with:

```python
await ws.accept()
```

and no Twilio signature validation.

Twilio's current Media Streams documentation explicitly requires validating `X-Twilio-Signature` for the WebSocket endpoint.

### Why it matters

An unauthenticated public WSS can be used to imitate Twilio events and consume:

- Deepgram;
- OpenAI;
- ElevenLabs;
- CPU;
- memory;
- business tools reachable through the conversation.

### Fix

Validate the Twilio signature **before** `ws.accept()` using the exact public WSS request URL/headers according to Twilio's current security algorithm.

Optional next hardening:

```text
signed /twilio/voice webhook
→ reserve CallSid for short TTL
→ WSS start.CallSid must match
```

Do not overbuild admission control before basic WSS authentication lands.

---

# P1 — strong local speed/network wins

## NET-06 — remove unnecessary Flux 48 kHz transcode after A/B

### Current code

`packages/runtime/streaming_stt_bridge.py:181-223`

converts:

```text
Twilio μ-law 8k
→ linear16 8k
→ linear16 48k
→ 80 ms / 7680-byte chunks
```

This was added while debugging Flux 1005 disconnects.

But the later root causes found were:

- Nova-style KeepAlive sent to Flux;
- wrong Flux message parser.

Current Deepgram documentation supports raw Flux:

```text
encoding=mulaw
sample_rate=8000
```

and recommends ~80 ms chunks.

### Better local path to test

Aggregate four Twilio frames:

```text
4 × 160-byte μ-law frame
= 640 bytes
= 80 ms
```

then send to Flux as:

```text
encoding=mulaw
sample_rate=8000
```

No decode/resample.

### Capacity effect

Current 48k linear16 path is:

```text
48,000 samples/s × 2 bytes ≈ 768 kbps/call
```

Raw μ-law 8k is:

```text
8,000 bytes/s ≈ 64 kbps/call
```

So the current Flux STT uplink is roughly **12× larger** than necessary.

At multiple calls this becomes an important Karachi uplink/concurrency issue.

### Rollout

Feature flag:

```text
FLUX_AUDIO_MODE=linear48
FLUX_AUDIO_MODE=mulaw8
```

Run identical call fixtures.

Promote raw μ-law only if:

- connection remains stable;
- transcript accuracy matches;
- EOT latency matches/improves.

---

## NET-07 — fix Flux query parameter names

### Current code

`apps/api/app/providers/stt/deepgram_flux_stt.py:93-110`

uses:

```text
language_hints
keyterms
```

as URL query parameters.

Current Deepgram API uses:

```text
language_hint
keyterm
```

for connection query params.

Plural forms are used by Flux **Configure messages**.

Also, `language_hint` is only valid with `flux-general-multi`; it should not be sent to `flux-general-en`.

### Fix

For `flux-general-en`:

```text
model=flux-general-en
(no language_hint)
keyterm=tooth implant
keyterm=root canal
...
```

For multilingual:

```text
model=flux-general-multi
language_hint=en
...
```

Add a URL-building test against expected query parameter keys.

---

## NET-08 — keep Flux, but split "Flux EOT" from "Flux Eager"

Flux itself is no longer obviously broken after the parser/keepalive fixes.

But `EagerEndOfTurn` has a different risk profile from normal Flux EOT.

Current situation:

```text
Flux Eager
→ speculative paid OpenAI call
→ possible TurnResumed
→ cancel
→ commit-lock state
```

Given OpenAI is the only reliable paid LLM provider, unnecessary speculation costs both quota and complexity.

### Run three explicit modes

**A — Nova baseline**
```text
Nova endpointing=150
```

**B — Flux standard**
```text
Flux EndOfTurn only
eager disabled
```

**C — Flux low-latency**
```text
Flux EagerEndOfTurn + TurnResumed
```

Compare:

- actual EOT detection;
- response dispatch;
- p50/p95 first answer audio;
- false-EOT/TurnResumed frequency;
- cancelled OpenAI calls;
- commit-lock incidents.

### Recommendation

Keep Flux available, but do not make Eager speculation inseparable from Flux.

The most robust local default may turn out to be **Flux standard EOT**.

---

## NET-09 — correct the Nova 1000 ms mental model

Current Nova config:

`apps/api/app/providers/stt/deepgram_stt.py:125-147`

```text
endpointing=150
utterance_end_ms=1000
```

Deepgram documents these as independent mechanisms.

`endpointing=150` can produce:

```text
speech_final=true
```

after the configured detected silence.

`utterance_end_ms=1000` is a separate transcript-gap fallback/event.

Therefore:

> Nova does not inherently add a mandatory 1000 ms delay to every turn.

Correct the comments and dashboards so future optimization isn't based on a nonexistent fixed cost.

---

## NET-10 — one ElevenLabs multi-context WSS per call

This remains one of the strongest local changes.

Official ElevenLabs guidance for multi-context TTS says:

- one WebSocket per end-user session;
- independent contexts;
- flush at complete sentences;
- close/cancel context on interruption.

### Current architecture

The LLM sentence pump:

`apps/api/app/routes/twilio_actor.py:1763-1831`

calls `_stream_tts_incremental()` once per sentence.

Current HTTP mode therefore makes one `/stream` request per sentence.

Existing WS method also opens a new socket per call to `ws_stream_synthesize()`.

### Target

```text
call opens
→ one ElevenLabs /multi-stream-input WSS

assistant turn
→ create context
→ send sentence 1 + flush
→ send sentence 2 + flush
→ close context

barge-in
→ close/cancel current context
→ Twilio clear

call ends
→ close socket
```

**Use one context per assistant turn**, not one context per sentence.

This should improve:

- first-byte behavior after call warmup;
- voice continuity/prosody;
- interruption control;
- network setup overhead;
- "breaky" speech caused by sentence boundaries.

---

## NET-11 — do not stream arbitrary LLM token fragments into TTS

`packages/core_agent/streaming.py` intentionally buffers to punctuation and merges a tiny first opener.

That is broadly correct for voice quality.

The earlier conclusion that `speculative HIT → speaking` represented a full second of "sentence-buffer waste" is not valid: `speculative HIT` means the speculative brain task still owns the response, not that the LLM has completed an answer.

### Instrument instead

Add:

```text
llm_request_start
llm_first_token
first_sentence_boundary
speech_gate_release
tts_text_sent
tts_first_audio
```

Only then tune `min_first_chars`.

Do not send half-words or arbitrary 3–5 token fragments to ElevenLabs just to win a stopwatch metric.

---

## NET-12 — OpenAI-only is not a blocker to fast lanes

You do not need Groq/Cerebras credits to implement a useful fast-brain architecture.

Use the same OpenAI account/model but vary **whether you call it and what tools/context you expose**.

### Lane A — deterministic, zero LLM
Examples:

- response cache;
- "can you hear me?";
- business hours/address if authoritative state already has exact answer;
- fixed brief wait bridge selected by policy.

### Lane B — OpenAI Fast, reduced tool surface
For ordinary conversational language realization:

```text
no tools
or
only one relevant read tool
```

### Lane C — OpenAI Fast, full tool set
For:

- booking;
- rescheduling;
- business actions;
- ambiguity requiring tools.

Current logs show normal calls often carry the full tool schema.

Reducing irrelevant tool descriptions is a better local A/B than swapping to an unreliable free provider.

### Predicted Outputs

`gpt-4o-mini` supports Predicted Outputs, but use only when a meaningful portion of output is actually known.

Do not predict generic "Sure!" openers.

It is experimental, below the network/runtime fixes above.

---

## NET-13 — capture actual OpenAI rate-limit/capacity headers

Current shared OpenAI client is already a good design:

`apps/api/app/providers/llm/openai_llm.py:80-97`

```text
persistent HTTP/2
max_connections=20
max_keepalive=10
```

Do not rewrite this now.

Instead log per request:

```text
x-request-id
x-ratelimit-limit-requests
x-ratelimit-remaining-requests
x-ratelimit-reset-requests
x-ratelimit-limit-tokens
x-ratelimit-remaining-tokens
x-ratelimit-reset-tokens
```

This turns provider capacity from guesswork into measured headroom.

Also instrument HTTP-pool waiting if possible before changing `max_connections=20`.

---

# P1 — audio and caller-perceived latency measurement

## NET-14 — FIRST40 is emitted once per provider chunk, not once per answer

`apps/api/app/routes/twilio_actor.py:4627-4662`

creates one FIRST40 mark per call to `_send_audio_frames()`.

But streaming TTS calls `_send_audio_frames()` on each ElevenLabs response chunk.

Newest logs show:

```text
FIRST40_2
FIRST40_3
...
FIRST40_17
```

for one answer.

That is not "first 40 ms of a reply."

### Fix

Move first-answer mark ownership above `_send_audio_frames()`.

Generate only once per:

```text
speech_generation / assistant reply
```

Call it something like:

```text
ANSWER_FIRST40_<speech_generation>
```

---

## NET-15 — Twilio mark is playout acknowledgment, not literal "caller ear"

Current comment says:

```text
true wire-to-ear
Twilio confirmed caller has heard it
```

Twilio documents a mark as notification that preceding media has completed playback on the call.

That is the best server-side playout proxy.

It is **not** an acoustic timestamp from the handset speaker.

### Correct metric names

```text
answer_first_media_wire_ms
twilio_first_audio_playout_ack_ms
server_observable_interactive_latency_ms
```

Do not add an arbitrary fixed `+200 ms` and call it caller-perceived.

For actual acoustic caller-ear latency, use a physical/second-device test harness.

---

## NET-16 — filler currently corrupts the latency spans

Newest call contains:

```text
wire_first_frame=-448ms
brain=-538ms
```

Negative latency means marks from different logical outputs have been mixed into one span.

Filler can hit the wire before the real answer's `tts_first_byte`.

### Split telemetry

Per turn:

```text
turn_authority_at
llm_request_at
llm_first_token_at
first_safe_sentence_at

filler_first_wire_at          # optional
filler_first_playout_ack_at   # optional

answer_tts_request_at
answer_tts_first_byte_at
answer_first_wire_at
answer_first_playout_ack_at
```

Never let filler write `answer_*` milestones.

If timestamp ordering is invalid, log `invalid_ordering=true` rather than subtracting and printing a negative latency.

---

# P1 — local concurrency / deployability

## NET-17 — current n=10 result does not prove 10 full AI calls

`scripts/multi_call_probe.py:1-21` explicitly states:

> "Not a load test."

It sends:

```text
connected
start
silence
stop
```

and verifies greeting/isolation.

That proves the actor/WebSocket design can host multiple call shells.

It does **not** simultaneously exercise:

```text
real speech
Deepgram EOT
SmartTurn
OpenAI
ElevenLabs
Twilio outbound audio
DB persistence
barge-in
```

### Build a real local load test

Use prerecorded μ-law caller fixtures over the production WebSocket protocol.

Test:

```text
N=1
N=5
N=10
N=20
```

for now.

Two shapes:

**normal staggered**
- callers speak at randomized offsets.

**EOT burst**
- all callers finish speaking within ~1 sec.

The EOT burst is crucial because it creates simultaneous:

```text
SmartTurn
OpenAI
ElevenLabs
DB
```

work.

### Measure

```text
active_calls
CPU
RSS
event_loop_lag
mailbox_depth
STT audio backlog_ms
SmartTurn inflight + p95
OpenAI pool wait
OpenAI rate-limit remaining
ElevenLabs generation starts
TTS queue depth
dropped audio ms
Twilio media/mark delay
p50/p95 turn latency
```

Do not publish a production concurrency number until this passes.

---

## NET-18 — SmartTurn wastes CPU continuously per call

`apps/api/app/routes/twilio_actor.py:601-623`

runs per call every 200 ms:

```text
5 predictions/sec/call
```

even when there may be no useful EOT decision to make.

At:

```text
10 calls → ~50 predictions/sec
20 calls → ~100/sec
```

before other CPU work.

### Fix

After fixing audio:

- run SmartTurn only when caller is actively speaking or in an EOT candidate window;
- do not poll idle/listening silence indefinitely;
- use a bounded shared inference semaphore/executor;
- benchmark ONNX Runtime with low thread counts, e.g. intra/inter-op 1;
- expose SmartTurn queue/inference duration.

---

## NET-19 — per-call event-loop watchdog multiplies timers

`apps/api/app/routes/twilio_actor.py:954-982`

Each call wakes every 20 ms.

That means:

```text
50 wakes/sec/call
20 calls → 1000 wakeups/sec
```

just for identical process-level event-loop lag detection.

Replace it with one process-level event-loop lag monitor.

Call-specific stalls should be derived from span/queue telemetry, not 20 copies of the same event-loop clock.

---

## NET-20 — synchronous DB work is still on async call paths

`apps/api/app/core/session_manager.py`

does synchronous:

```text
SessionLocal
queries
PII redaction
db.commit()
```

inside methods called from async voice paths.

Examples:

- `_persist_session()`
- `persist_booking_from_tool()`
- `run_greeting()`
- `run_user_turn()`
- `end_session_async()`

`apps/api/app/db/session.py:73-76` even records the old assumption that sync work was acceptable below 10 req/s.

### Why it matters now

A blocking SQLite/SQLAlchemy call on the single Uvicorn event-loop thread can pause **all active calls**, including Twilio receive/send.

You do not need a new server or Postgres today to fix the immediate problem.

### Local patch

Move noncritical persistence off the event loop:

```python
await asyncio.to_thread(_persist_session, state)
```

or use one bounded local persistence worker queue.

Do the same for synchronous booking DB writes while preserving transaction ordering.

SQLite can remain your local dev/demo DB for this phase.

Before serious client production/multi-instance work, revisit database architecture separately.

---

## NET-21 — Deepgram event queues are unbounded

Both providers create:

```python
asyncio.Queue()
```

for STT events.

Files:

- `apps/api/app/providers/stt/deepgram_stt.py`
- `apps/api/app/providers/stt/deepgram_flux_stt.py`

Under downstream backpressure, interim transcripts can accumulate indefinitely.

### Fix

Bound queue, e.g.:

```text
maxsize=128
```

Policy:

- interim `partial/Update`: coalesce/drop old partials if full;
- final/EOT/TurnResumed/control events: never silently drop.

This protects memory and tail latency under multi-call load.

---

## NET-22 — keep one Uvicorn worker for now

The current actor registry is process-local:

`packages/runtime/call_actor.py:380-440`

and the server script starts one Uvicorn process.

Under the **current no-server / local-machine phase**, this is correct.

Do **not** casually add:

```text
uvicorn --workers N
```

because per-process:

- actor registry;
- session memory;
- caches;
- provider clients

would be independent.

Scale within one process first and measure realistic N=5/10/20 calls.

Multi-process/distributed architecture is a later problem, not part of this local speed audit.

---

# P2 — correctness/deployability cleanup

## NET-23 — TwiML `{{From}}/{{To}}` custom parameters are unsafe assumptions

`apps/api/app/routes/twilio.py:120-136`

renders literal:

```xml
<Parameter name="from" value="{{From}}"/>
<Parameter name="to" value="{{To}}"/>
```

The code assumes Twilio expands these strings.

Safer:

- read actual `From`, `To`, `CallerName` from the already-signed webhook form;
- XML-escape and insert those actual values into generated TwiML.

This becomes important when DNIS/tenant routing lands.

---

## NET-24 — Flux 1005 at call shutdown may be false alarm/reconnect

Newest successful Flux calls still end with:

```text
deepgram-flux stream closed abnormally ... code=1005
reconnect 1/3
```

very near call shutdown.

The provider's consumer treats 1005 as abnormal and bridge begins reconnect logic.

### Fix

Track local shutdown state explicitly.

If:

```text
call stopping
or
audio source exhausted and CloseStream sent
```

then a socket close without a normal close frame should not trigger a reconnect.

Only reconnect a 1005 while the call is still active.

---

## NET-25 — archive/export process included `.env` and `.env.bak`

The audit zip contains:

```text
.env
.env.bak
.env.example
```

Do not expose values.

### Fix

Any future audit/client/export bundle should automatically exclude:

```text
.env
.env.*
```

except:

```text
.env.example
```

If this archive was distributed beyond trusted tooling, rotate credentials.

This is not a latency issue, but it is a deployable-product issue.

---

# OpenAI-specific local speed strategy

OpenAI-only does **not** mean there is nothing else to optimize.

Priority:

## 1. Keep current streaming + Fast tier
Already shipped.

## 2. Keep prompt cache
Already shipped.

## 3. Keep speech-act output budgets
Already shipped.

Output-token reduction is usually a stronger latency lever than endlessly trimming a moderate prompt.

## 4. Dynamic tool narrowing
A/B:

```text
ordinary conversation:
  no tools / minimal tools

booking:
  availability + booking + semantic plan

FAQ:
  lookup_faq only if cache/RAG needed
```

Do not send every tool schema on every conversational turn.

## 5. Deterministic fast lane
Use cache/conversation-control for safe cases.

Current `RESPONSE_CACHE_BYPASS=true` intentionally forces worst-case LLM behavior for humanness testing. That is fine for diagnostics but should **not** be used when reporting production p50.

Maintain two benchmark modes:

```text
RAW_LLM
response_cache_bypass=true

PRODUCT
response_cache_bypass=false
```

## 6. Predicted Outputs
Only after higher-priority changes.

Useful only when much of output is genuinely known.

Do not predict generic acknowledgements.

## 7. Persistent OpenAI Responses WSS
Later experiment.

Do not activate until local bugs + TTS session + tool narrowing are measured.

---

# What is actually causing the 2–4 second perception?

Do not express it as one guessed additive equation.

Current server-observable chain is approximately:

```text
caller finishes actual speech
  ↓
turn authority / EOT arrives
  ↓
brain/model produces first safe sentence
  ↓
ElevenLabs first audio
  ↓
first media sent to Twilio
  ↓
Twilio plays it / mark ACK
```

Current real evidence shows:

- OpenAI raw LLM path is still often ~1.3–2.1 s to first content/decision.
- ElevenLabs HTTP first byte is often ~0.27–0.42 s.
- Twilio FIRST40 mark ACK frequently adds roughly ~0.28–0.6 s from mark send to playout completion.
- filler can start before the real answer and currently contaminates timing.
- Flux/native-turn duplicate events and SmartTurn corruption mean EOT timing itself is not yet trustworthy.

Therefore the correct first action is **fix semantics + instrumentation**, not add a hardcoded "Karachi adds 1500 ms" term.

---

# Correct latency model

Track four separate quantities.

## A. Turn-detection latency

```text
last recognized caller word/audio authority
→ Nova speech_final OR Flux EndOfTurn
```

## B. Generation latency

```text
turn authority
→ first safe speakable sentence
```

## C. TTS/network-to-Twilio latency

```text
first safe sentence
→ answer first media frame sent
```

## D. Twilio playout latency

```text
first answer frame sent
→ FIRST40 mark ACK
```

Server-observable interactive latency:

```text
A + B + C + D
```

Actual acoustic handset/ear latency is not directly measurable from FastAPI. Use a second-device/acoustic harness if that number matters.

Also maintain:

```text
first_any_audio
first_useful_answer_audio
```

because filler and answer are not the same thing.

---

# The 19 pre-existing test failures

The bundle records the count but does not preserve the actual failing test identities/output.

Therefore it is not defensible to label exact failures "safe" or "load-bearing" merely from the number 19.

For this networking/speed work, the tests that are load-bearing are behaviors, regardless of whether they are among the 19:

1. one Flux EndOfTurn → one committed turn;
2. Eager → TurnResumed cancels exactly once;
3. μ-law→SmartTurn conversion correctness;
4. filler cannot overlap answer audio;
5. outbound audio has one producer;
6. one FIRST40 metric per answer;
7. Twilio WSS invalid signature rejected;
8. STT backpressure drops stale audio;
9. Deepgram reconnect only while call active;
10. sync persistence cannot stall media event loop;
11. N simultaneous real speech calls keep isolation;
12. barge-in clears stale output;
13. no same-generation double TTS;
14. no cross-call state leakage.

Do not spend time blindly making "19 failed" become zero before extracting their names and classifying them.

---

# Local realistic concurrency plan

The architecture is multi-call capable at the actor level.

But current evidence only proves concurrent **call shells**, not full AI workload.

Before making a capacity claim:

## Step 1 — fix P0/P1 items
Especially:
- SmartTurn input;
- Flux double event;
- filler overlap;
- DB blocking;
- queues.

## Step 2 — local deterministic app ceiling
Mock external providers with realistic timing/audio streams.

Test:
```text
1 / 5 / 10 / 20 calls
```

This measures the Python/runtime ceiling independently of provider quotas/Internet.

## Step 3 — real-provider full call test
Use prerecorded real μ-law speech at realtime pace.

Test:
```text
1 / 5 / 10
```
then:
```text
20
```
if healthy.

## Step 4 — synchronized EOT burst
Make 10–20 callers finish at approximately the same instant.

This is the real stress case for:
```text
SmartTurn
OpenAI
ElevenLabs
DB
```

## Pass criteria
At a concurrency level, require:

```text
0 cross-call leakage
0 duplicate replies
0 mailbox overflow
0 sustained STT backlog
0 provider queue collapse
0 DB-induced event-loop stalls
0 stale audio after barge-in
p95 latency within agreed bound
```

Until that exists, do not claim a production simultaneous-call number from the old n=10 probe.

---

# Recommended task split between the two Claude Code chats

## SPEED / HUMANNESS CHAT

Own:

1. `NET-03` filler vs answer arbitration
2. `NET-10` ElevenLabs call-long multi-context WSS
3. `NET-11` first-safe-sentence telemetry / preserve sentence-quality gate
4. `NET-12` OpenAI-only fast lanes + dynamic tool narrowing
5. product-vs-raw-cache A/B
6. ConversationNextActionPolicy after network correctness

Do **not** edit:
- Flux parser/state machine;
- STT queues;
- SmartTurn audio conversion;
unless coordinated with networking chat.

## NETWORK / CAPACITY CHAT

Own:

1. `NET-01` Flux EndOfTurn double event
2. `NET-02` SmartTurn μ-law conversion
3. `NET-04` STT backpressure
4. `NET-05` Twilio WSS signature
5. `NET-06` Flux raw μ-law 80ms A/B
6. `NET-07` Flux query params
7. `NET-08` Flux standard vs Eager modes
8. `NET-09` Nova endpointing comments/telemetry
9. `NET-13` provider rate-limit headers/pool telemetry
10. `NET-14/15/16` latency metric semantics
11. `NET-17–22` real concurrency/runtime work
12. `NET-24` Flux shutdown/reconnect semantics

## Shared file ownership warning

Both chats touch:

```text
apps/api/app/routes/twilio_actor.py
```

Avoid simultaneous edits there.

Preferred coordination:

- networking chat first fixes STT/telemetry sections and commits;
- speed chat rebases/pulls;
- speed chat then owns TTS/filler sections.

---

# Exact next order

## First pass — correctness before another call benchmark
1. Fix Flux `EndOfTurn` double injection.
2. Fix SmartTurn μ-law decoding/resampler state.
3. Fix filler/answer overlap or temporarily raise/disable filler while testing.
4. Fix FIRST40 one-per-answer semantics.
5. Split filler vs useful-answer telemetry.
6. Fix Flux URL params.

## Second pass — local network reduction
7. A/B Flux raw μ-law 8k/80ms.
8. Add STT backlog telemetry and shrink queue.
9. Make 1005 reconnect shutdown-aware.
10. Capture OpenAI rate-limit/pool telemetry.

## Third pass — speed/humanness
11. ElevenLabs one multi-context WSS/call.
12. Dynamic OpenAI tool narrowing.
13. Re-enable response cache for **product-mode** benchmark.
14. Run Flux standard vs Flux Eager vs Nova.

## Fourth pass — concurrency
15. Remove sync DB work from event loop.
16. Gate/bound SmartTurn inference.
17. Replace per-call loop-lag watchdog with process-level monitor.
18. Bound/coalesce Deepgram event queues.
19. Build real μ-law N=1/5/10/20 load test.
20. Run synchronized EOT burst.

---

# Things NOT to do right now

- Do not move to Azure/VPS just to make progress.
- Do not send arbitrary token fragments to TTS.
- Do not turn every response into an immediate filler/ack.
- Do not turn off barge-in to hide echo.
- Do not switch LLM providers merely for benchmark TTFT.
- Do not keep trimming the prompt aggressively before fixing runtime semantics.
- Do not add Uvicorn workers yet.
- Do not claim n=10 full production calls from `multi_call_probe.py`.
- Do not treat `utterance_end_ms=1000` as an unavoidable 1-second Nova tax.
- Do not treat Twilio mark ACK as literal acoustic caller-ear measurement.
- Do not benchmark "product speed" with `RESPONSE_CACHE_BYPASS=true`.

---

# Bottom line

Keeping the app in Karachi is not preventing useful progress.

The biggest available wins are currently **inside the code**:

```text
Flux turn semantics
SmartTurn audio
audio-source arbitration
TTS connection lifetime
stale queue control
OpenAI request shape
event-loop blocking
measurement correctness
```

Fixing these first will make any later infrastructure move easier to evaluate, but none requires a new server today.
