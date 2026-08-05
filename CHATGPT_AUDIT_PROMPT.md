# Voice-Agent Bug Audit — Prompt for ChatGPT / Deep Analysis

## What this is

Attached: full source of a real-time voice-agent SaaS (FastAPI + Python + browser
widget). Below: 20+ real bugs found across a 12-hour debug session, each with
symptom / root cause / fix. Use this to build a bug-class taxonomy so you can
**pre-empt** these classes of failure when you audit new code in this domain.

## What I want from you

1. **Cluster the bugs** into ~5-8 recurring failure classes. Name each class.
   Give each a one-sentence definition and the signature (what to grep for /
   what to look at) that reliably surfaces it.

2. **For each class**, produce a checklist of preventive questions to ask
   during code review — the sort of thing a senior reviewer would automatically
   check but a junior wouldn't.

3. **Audit the attached codebase** with those classes in mind. Look for any
   instances of the same bug shapes that DIDN'T show up in this session's
   debugging (because we didn't trip them yet). Report them by file:line with
   severity.

4. **Predict the next 5 bugs** that are architecturally likely to appear
   given the shape of this codebase — bugs we haven't hit yet but will.

5. **One killer question**: if you had 30 minutes with the founder before they
   ship this to a paying customer, what's the ONE architectural change you'd
   push for that would eliminate a whole class of these bugs?

---

## The bugs (in the order they were found)

### 1. Silent WebSocket handshake race

**Symptom:** Browser client connected to `/twilio/stream`, server saw the WS
accept, but the actor never received `connected` / `start` frames. Zero events,
zero errors — just hung.

**Root cause:** JS code did `const ws = new WebSocket(url); await new Promise(r => ws.onopen = r)`.
On localhost with warm cache, `onopen` fires before the assignment lands, so
the promise never resolves.

**Fix:** Check `readyState` before subscribing; use `addEventListener` with
`{once: true}`.

**Class hint:** async initialization race between event source and handler
attachment.

---

### 2. Deepgram consumer async-iterator starvation

**Symptom:** Deepgram WS connected fine, producer sent audio (500+ chunks),
consumer coroutine spawned. Zero messages parsed. Deadlock — brain never fired.

**Root cause:** Consumer used `async for raw in ws` on the `websockets` v15
library. Under load with a busy producer holding the loop, the async iterator
starves; the internal reader task never gets scheduled.

**Fix:** Switch to explicit `while True: raw = await ws.recv()` — deterministically
yields on each read.

**Class hint:** async-for vs await-recv semantics differ under contention;
library version bumps silently change scheduling behavior.

---

### 3. TurnManager `saw_speech_start` never resets

**Symptom:** STT finals reached the actor. TurnManager fired
`EAGER_END_OF_TURN` correctly. But `_confirm_end_of_turn` always emitted
`TURN_RESUMED` instead of `END_OF_TURN` → brain never fired.

**Root cause:** `saw_speech_start = True` on Deepgram `SpeechStarted`, reset
only on `speech_end`. With continuous VAD (Deepgram Nova-3), the flag stayed
True forever, so the confirm window always saw "speech resumed."

**Fix:** Reset `saw_speech_start = False` when scheduling the confirm task, so
the window only observes NEW speech during the 400ms confirmation.

**Class hint:** state flags used as edge detectors that were never designed for
continuous input; assumption "signal will end cleanly" doesn't hold with
always-on VAD.

---

### 4. Cross-tenant leak guard blocks own writes

**Symptom:** WebSocket accepted then immediately closed. Server log:
`CrossTenantLeakError: query on 'sessions' has no tenant_id filter`. Even
though session_manager set `tenant_id` on the row.

**Root cause A:** `_persist_session` called `db.get(SessionRow, session_id)`
without the tenant contextvar being set on the Twilio async path. The
auto-filter listener needs `current_tenant` to inject WHERE.

**Root cause B:** The guard's SQL-inspection regex checked for `" where "`
(space-space) but SQLAlchemy compiles with `"\nwhere "` (newline). So even
a properly filtered query was rejected.

**Fix A:** Wrap DB scope in `set_current_tenant(state.tenant_id)`; add explicit
`.filter(tenant_id)`.
**Fix B:** Normalize whitespace with `.split()` before substring check.

**Class hint:** silent guard bugs that reject legitimate queries; SQL text
inspection that misses common formatting.

---

### 5. Twilio trial `<Connect><Stream>` silently drops

**Symptom:** Placed outbound call from Twilio → phone rang → picked up →
13 seconds of silence → hangup. No error in Twilio logs, no error in ours,
no WebSocket open on our side.

**Root cause:** Twilio Trial accounts silently drop `<Connect><Stream>` on
outbound calls (only on outbound). `<Say>` and `<Play>` work; Media Streams
don't. No error is reported anywhere.

**Fix:** Upgrade to a Full account. But then Full accounts get an automatic
Fraud-Ops hold on Voice for new upgrades — error 10005 blocks every outbound
call until compliance manually approves.

**Class hint:** vendor tiers with feature availability that isn't documented in
error responses; assume "no error" ≠ "worked".

---

### 6. Streaming path had zero durable event log writes

**Symptom:** `/debug/call/{id}/timeline` returned 0 events for real calls even
though brain replied. Post-mortem impossible.

**Root cause:** Only `kernel_wiring._log_event` wrote to the durable log, and
it only fired if the dialogue kernel had processed a turn. Streaming brain
path bypassed the kernel entirely for many replies, so nothing got persisted.

**Fix:** Instrument `_run_brain_from_text` and `_speak` to write STT / LLM /
TTS events directly to the log.

**Class hint:** observability gaps in orthogonal execution paths — Path A got
instrumented, Path B didn't; failures on Path B look identical to "no
activity."

---

### 7. Deepgram `endpointing` too aggressive

**Symptom:** Caller says "Am I calling Smile? Can you hear me?" → STT fires
`is_final=True` on the fragment "Am I calling Smile?" and the second half
becomes a new turn. Agent replies to fragments.

**Root cause:** `endpointing=300ms` (Deepgram default) fires final too fast on
natural mid-sentence pauses.

**Fix:** Bump to 800ms → 1200ms. Add semantic guard that buffers finals
ending on conjunctions/prepositions/articles until a "complete-looking"
final arrives.

**Class hint:** turn-boundary heuristics tuned for benchmark speech, wrong for
real users; layered heuristics work better than one aggressive threshold.

---

### 8. Audio downsample "worked" but sounded like silence

**Symptom:** Browser mic captured your voice fine (verified by dumping the raw
PCM and playing it back). Deepgram received it. But Deepgram never returned
transcripts.

**Root cause:** Mic worklet downsample math produced correct µ-law bytes
whose *values* were near-silence — client-side voice-detection ran on
byte-not-near-0xFF-or-0x7F (any non-silence byte counts as voice), so it
LOOKED like voice reached Deepgram, but the actual amplitude was 0.24% of
full scale. Deepgram's VAD threshold requires real signal.

**Fix:** Add 10× gain in worklet before µ-law encoding. Laptop mics
routinely deliver signal at -40dB. Deepgram needs -20dB or better.

**Class hint:** verification checks that pass on "shape looks right" but
never inspect actual signal magnitude; instrumentation lying because it
measures the wrong thing.

---

### 9. Mic-hears-speaker feedback loop

**Symptom:** Agent said "How can I help you?" → 1 second later STT reported
"Hello? How can I help you?" as a new user turn → brain replied to itself →
loop. Idle-followup fires, agent says it, mic hears it, treats it as user
speaking, kills the followup, loops forever.

**Root cause:** No echo suppression. Laptop speaker → laptop mic → Deepgram
→ turn manager → brain → speaker. Classic acoustic feedback loop.

**Fix (partial):** Gate mic upload while `agentSpeaking` flag is True; unset
via `onPlaybackCaughtUp` with 250ms tail delay. Breaks barge-in but stops
the loop.

**Class hint:** local-loopback assumptions that break on speaker-mic hardware;
"mic is always on" architecture needs explicit gating.

---

### 10. Small STT fragment triggers full intent detection

**Symptom:** User's turn broken into two: agent gets just the word
"Schedule" as a full turn. Kernel's regex intent classifier keyed on
"Schedule" → adds `book_appointment` task with slot `trigger_text="Schedule"`.
LLM then hallucinates values ("Alex") to fill the slots because it has to
respond to something.

**Root cause A:** Intent classifier fires on any keyword match without
requiring a minimum surrounding context.
**Root cause B:** LLM prompt doesn't include "if the user gave you nothing to
work with, ASK for it, don't hallucinate."

**Fix:** Minimum utterance length for intent registration; stricter tool-arg
schemas that refuse to accept invented values.

**Class hint:** downstream systems over-committing on upstream fragments;
LLM hallucinating slot values because it lacks a "clarify first" escape hatch.

---

### 11. Deepgram consumer received messages but nothing was forwarded

**Symptom:** Consumer's raw messages were bytes-non-empty (400+ bytes each,
20+ messages), yet ledger showed 0 STT events.

**Root cause:** In the raw messages, most had `transcript: ""` (blank Results
frames sent by Deepgram between real speech). Our consumer only forwarded
`if text` — silently dropped everything. Blank frames were the majority.

**Fix:** Not a bug per se — but the debug logs made it LOOK like nothing was
flowing. Need better observability: log message-count-by-type, not just
non-empty payloads.

**Class hint:** "silent success" — code that correctly filters noise, but the
filter looks like a failure to a debugger.

---

### 12. Google Cloud VPL degradation labels leaking into TTS audio

**Symptom:** Agent voice sounds "old-school TV, crispy." Not a code bug —
by design.

**Root cause:** `_get_telephony_tts` returned ElevenLabs with
`output_format=ulaw_8000` (Twilio-tier phone quality) for the browser widget
too. Browser upsampled 8kHz → 48kHz with linear interpolation. Sounded like
a landline.

**Fix:** Change TTS output_format to `pcm_16000`. Encode to µ-law only at
the Twilio transport layer, not at the intelligence layer.

**Class hint:** wire-format decisions leaking upward into intelligence code;
"lowest common denominator" defaults poisoning quality for callers who
don't have that limitation.

---

### 13. TTS output format detection branch-explosion (nearly)

**Symptom:** I was about to write `if browser_call: use pcm_16000 else use
ulaw_8000` inside the actor. User rejected this as bad design.

**Root cause:** Correct architecture is transport-agnostic actor + transport
adapter. Detection-branching in intelligence code = smell.

**Fix:** Not fully implemented yet — but the principle is: `send_audio(pcm,
rate)` on the base session, subclasses override for their wire format.

**Class hint:** "runtime-detect-and-branch" in domain logic — reliable sign
that the layering is wrong.

---

### 14. Widget audio-worklet cache

**Symptom:** Applied a fix to `mulaw-worklet.js`, restarted server, tested
in browser — old behavior. Applied the fix 3 times before realizing it was
cached.

**Root cause:** Browsers cache AudioWorklet modules more aggressively than
regular JS. Even `Cmd+Shift+R` doesn't always bust it if the mimetype is
right.

**Fix:** `touch` files server-side to update mtime → new ETag → cache invalidates.

**Class hint:** cache assumptions for static assets that differ per asset type;
your "hard reload" doesn't reload everything.

---

### 15. Voice cutting off on natural pause

**Symptom:** User said "I want to know about the doctors" with a mid-sentence
pause → agent replied after only "I want to know". Deepgram endpointing +
turn manager + semantic guard all failed to preserve the full utterance.

**Root cause:** Every layer had a timeout shorter than a natural human pause
(~1.5s+ for gathering thoughts). Endpointing 1200ms, TurnManager confirm
400ms, semantic guard buffers only if final ends on conjunction. Real
speech routinely ends on nouns/verbs mid-thought.

**Fix (pending):** Give the buffer a max-hold with graceful flush (1500ms).
Add smarter completeness scoring — trailing question mark = definitely done,
trailing period after a short fragment = probably not.

**Class hint:** cascade of independent timeouts each tuned to a different
optimism level; real speech violates the assumptions of every layer.

---

### 16. Missing `debug_live` fanout callback thread-safety

**Symptom:** N/A — landed correctly first try but was a subtle risk.

**Root cause (avoided):** Subscribers to `CallEventLog.write` fanout can be
in different threads (SQLite writer sometimes off-loop). Naive
`asyncio.Queue.put_nowait` from a non-loop thread → RuntimeError.

**Fix:** Use `loop.call_soon_threadsafe(_enqueue_nowait, event)` in the
subscriber.

**Class hint:** cross-thread asyncio interactions in server code; the risk
lurks in every observer / fanout pattern.

---

### 17. Config change didn't propagate

**Symptom:** Set `TWILIO_SIGNATURE_ENFORCE=false` in `.env`. Server still
rejected Twilio requests with 401. Confirmed env var was set. Restarted
server. Same 401.

**Root cause:** `TWILIO_SIGNATURE_ENFORCE` was read via `os.environ.get()`
inline in the middleware, NOT through Pydantic settings. `.env` was loaded
by Pydantic. Two config-loading paths, only one saw the value.

**Fix:** Route all config through Pydantic settings, or launch server with
the env var on the CLI.

**Class hint:** dual config-loading systems (Pydantic + raw os.environ) that
diverge; `.env` values that appear set but don't reach the code that reads them.

---

### 18. Log-info lines invisible in server log

**Symptom:** Added `log.info("...")` in the actor. Ran the app. Nothing
appeared in `/tmp/uvicorn.log`. Confusion about whether code was even
executing.

**Root cause:** Uvicorn's default log config configures its own loggers
(`uvicorn.access`, `uvicorn.error`) but doesn't attach any handler to the
application's root logger or arbitrary sub-loggers. Our `logging.getLogger(__name__)`
had no handler → messages went nowhere.

**Fix (temp):** Switch critical diagnostic messages to `print(..., flush=True)`.
Real fix: configure the root logger at startup.

**Class hint:** logging configuration that "silently drops most log calls"
because handlers aren't attached to your loggers by default.

---

### 19. Playback ledger tied to Twilio's µ-law byte math

**Symptom:** After switching TTS to PCM 16kHz, ledger's `duration_ms`
calculation went wrong (chunks were 4x-longer than reported → mark_ack timing
skewed → heard-text reconciler drifted).

**Root cause:** `duration_ms = int(len(mulaw) / 8)` hard-coded the µ-law-8kHz
byte rate. Format change broke the math.

**Fix (partial):** New formula for PCM 16kHz: `bytes / 32`. Real fix: pass
`sample_rate` and `bytes_per_sample` through `AudioChunk`, compute duration
from those.

**Class hint:** magic numbers derived from one wire format baked into
higher-layer bookkeeping; single-format assumptions that break under any
new transport.

---

### 20. Rate limits + provider silently returning empty replies

**Symptom (earlier sessions):** LLM replies came back empty (no text, no error,
just an empty payload). Brain flowed through, TTS synthesized nothing,
agent said nothing.

**Root cause:** Groq rate-limit responses came back as 200 OK with an empty
choices array. Only the router's cool-down fired eventually. But that first
call silently produced no reply.

**Fix:** Validate LLM responses have non-empty content; treat empty content
as an error and fall over to the next provider.

**Class hint:** vendors returning "success with garbage" instead of clean
error codes; success-checking that only looks at HTTP status.

---

### 21. Test WebSocket receive was blocking, not polling

**Symptom:** Wrote an integration test that opened both audio + debug
WebSockets and drained them. Test hung to timeout every time.

**Root cause:** `starlette.testclient.WebSocket.receive_json()` is BLOCKING
with no timeout arg in some versions. My test loop assumed short-timeout
polling like `websockets` library.

**Fix:** Use live `uvicorn` + `websockets` client for genuinely async
integration tests. TestClient is fine for sync HTTP, awful for WebSockets.

**Class hint:** test-doubles behaving subtly differently from production
behavior; testing async code with sync test doubles.

---

### 22. Idle-followup timer fires against agent's own voice

**Symptom:** Agent finishes speaking → `_arm_idle_followup` starts 15s
timer → mic picks up ambient noise or echo → `_on_stt_final` fires with
noise → `_cancel_idle_followup` runs → but the brain also fires because
it saw a "user turn" → agent talks back → new idle timer → same
loop → "Anything else I can help you with?" repeats.

**Root cause:** Timer cancellation happens on ANY user turn, including
false positives from echo. No confidence threshold.

**Fix (pending):** Only cancel idle followup if the STT confidence >
threshold OR the text has >N words. Blank frames or single-word "echo"
fragments shouldn't count.

**Class hint:** cleanup logic triggered by noisy input signals; state
machines that don't distinguish "real user activity" from "sensor noise."

---

## Bug-class hints I've spotted (my draft — verify/refine)

- **Async init races** (#1, #2, #16)
- **Silent state flags** designed for edges but used with continuous input (#3, #7, #22)
- **Guard misfires** rejecting legitimate work (#4, #17)
- **Vendor black-boxes** — 200-with-garbage, silent tier restrictions (#5, #20)
- **Observability gaps** on orthogonal execution paths (#6, #11, #18)
- **Signal-quality invisible to shape-checks** (#8)
- **Layering violations** — wire concerns bleeding into logic (#12, #13, #19)
- **Cache staleness** for uncommon asset types (#14)
- **Cascading independent timeouts** (#15)
- **Test doubles subtly differ from production** (#21)
- **Echo/loopback assumptions** breaking on real hardware (#9, #22)

## About the codebase

- **Language:** Python 3.11 + FastAPI + Starlette WebSockets + JS (browser widget, no build step)
- **STT:** Deepgram Nova-3 streaming
- **TTS:** ElevenLabs (currently pcm_16000)
- **LLM router:** Groq (llama-3.3-70b) → Cerebras → Mistral → Gemini → NVIDIA
- **Dialogue kernel:** custom evidence-backed slot filler + task graph + propose→confirm→commit
- **Turn management:** semantic turn manager with EAGER_END_OF_TURN / END_OF_TURN / INTERRUPTION / BACKCHANNEL / etc.
- **Multi-tenancy:** SQLAlchemy with auto-filter listener + tenant-guard event
- **Transport:** Twilio Media Streams + custom browser widget (µ-law 8k phone / PCM 16k browser)
- **Business context:** dental clinic receptionist (Smile Dental Clinic sample profile with 8 services + 4 doctors)

## Files worth focusing on

- `apps/api/app/routes/twilio_actor.py` — the actor session, ~1300 lines, does too much
- `packages/runtime/turn_manager.py` — 7-event semantic turn taxonomy
- `packages/runtime/streaming_stt_bridge.py` — Deepgram producer/consumer bridge
- `apps/api/app/providers/stt/deepgram_stt.py` — recently patched (async iterator → recv loop)
- `apps/api/app/db/tenant_guard.py` — recently patched (whitespace normalization)
- `packages/core_agent/kernel_wiring.py` — where the dialogue kernel plugs into the brain
- `apps/call-stream/*` — browser widget (session.js, audio-pipe.js, mulaw-worklet.js)
