# Second audit (2026-08-04) — Intelligence, human behavior, technical differentiation

**Source:** external auditor (ChatGPT), delivered post-Sprint 9 alongside
the technical audit that produced `AUDIT_RESPONSE_3.md`.

**Character of this audit:** architectural, not defect-hunting.  Where
audit 3 fixed integration seams, this one calls for a new *intelligence
kernel* the agent runs against.  Twelve tracks, each a real subsystem.

**Verdict quoted verbatim:**

> VoiceOps currently has an **advanced orchestration shell around a
> conventional tool-calling chatbot**.  Most of the sophisticated
> components either operate after the important decision has already
> been made, observe behavior without controlling it, add another
> classifier or model call without changing the underlying state
> architecture, or exist beside the live pipeline rather than being
> authoritative within it.

We accept this framing.

---

## The twelve tracks

Kept in the auditor's original order.  Each track is a real Sprint-10+
work item; task IDs below are our internal work-tracker references.

### 1. `CallState` is not a dialogue state → **Conversation State Kernel**

Present `CallState` is an analytics summary (transcript + one intent +
extracted fields + sentiment + status).  Cannot represent corrections,
partial acceptance, slot provenance, tool-vs-caller-vs-profile source,
committed vs proposed action, unresolved questions.

**Deliverable:** `packages/dialogue/{state,evidence,reducer,policy,
temporal,plan,commit}.py` with `SlotEvidence` + `TaskState` + a
deterministic reducer.  LLM proposes state patches; application code
validates and applies them.

**Acceptance test:** "Tuesday at ten — no, scratch that, Thursday at
four." leaves Tuesday `superseded`, Thursday `explicit`, no booking
until confirmation.

### 2. Semantic planner is annotation, not planning → **Semantic Plan Protocol**

Current planner wraps ReceptionistBrain, waits for full reply, classifies
speech act post-hoc.  True planner decides BEFORE wording: task, missing
info, ask/answer/verify/propose/commit, allowed facts, forbidden claims,
expected next input.

**Deliverable:** structured plan JSON schema, then a realizer that
generates one concise utterance from it.  Deterministic templates for
critical actions (booking confirmations).

### 3. Tool calling is proposal-then-guard → **Propose → Confirm → Commit**

Write guard is compensating for an unsafe architecture (probabilistic
validation around probabilistic proposal).  Four-phase protocol:
proposal (with evidence_ids), confirmation (scope = which fields caller
accepted), commit (idempotency key), verification (read back external
result, speak from committed data).

### 4. Multi-intent structurally impossible → **Task Graph**

`ExtractedFields.intent: Intent` is a single enum.  Adversarial suite
already includes multi-part callers ("book + insurance + claim
status").  Add `ConversationAgenda(tasks, active_task_id,
deferred_task_ids, completed_task_ids)`.

### 5. No temporal intelligence → **Temporal Resolution Service**

Model is expected to resolve "tomorrow", "next Tuesday afternoon",
"the earliest slot after work".  No dedicated subsystem, business
timezone not authoritative.

**Deliverable:** `packages/dialogue/temporal.py` that takes utterance +
current business time + timezone + business hours + prior temporal
context, returns range + resolution class + spoken confirmation.
Detects impossible/ambiguous (past date, closed day, DST, "next Friday"
ambiguity, year-boundary).

### 6. Mood awareness is text sentiment → **Acoustic Interaction State**

Text can't recover prosody.  Compute inexpensive per-turn features:
speech_rate_wpm, energy, energy_variance, pause_count, longest_pause,
interruption_count, repeated_phrase_count, asr_mean_confidence,
vocal_tension_score, background_voice_probability.  Present as
interaction signals not psychological truth.

### 7. VPL overdesigned relative to input → **Delivery Plan from semantic + acoustic state**

VPL supports 12 dimensions; performance planner only emits 5 and gets
weak inputs (reply text + coarse act).  Feed it structured input:
speech_act, critical_spans (dates/times/names get emphasis), caller
state (frustration/urgency/rate), interaction state (interrupted,
repeating), voice profile.  Mostly deterministic; LLM only for
phrase-level annotations.

### 8. Turn-taking is the real moat → **Turn Manager**

Cloned voice quality breaks when the system interrupts wrong / speaks
over caller / treats "mhm" as new request / restarts full answer after
interruption.  One authoritative turn subsystem with events:
`EAGER_END_OF_TURN`, `TURN_RESUMED`, `END_OF_TURN`, `BACKCHANNEL`,
`INTERRUPTION`, `USER_REQUESTED_PAUSE`, `FALSE_INTERRUPTION`.
"Give me a second" → silence, not "Of course, how else can I help?".

Deepgram Flux exposes eager-end-of-turn + resumed-turn signals;
ElevenLabs has skip-turn; LiveKit distinguishes VAD from contextual
turn detection.

### 9. Playback must be dialogue truth → **planned / generated / heard triage**

Three texts: `planned_text`, `generated_audio_text`, `caller_heard_text`.
Only heard becomes history.  Chunk TTS at clause/word boundaries with
aligned start_ms/end_ms.  On interruption: clear + timestamp → last
complete word → replace assistant transcript turn with heard text →
record unsaid remainder as cancelled → next plan decides re-delivery.

**Called out as "genuine technical moat if implemented correctly."**

### 10. LLM router is availability, not intelligence → **Capability-Aware Routing**

RouterLLM tries providers by rank until one responds — but models differ
in tool-call reliability, structured output, latency class, multilingual
support.  Booking can silently switch to a weaker model mid-call.

**Deliverable:** `ModelCapabilities` per model, `approved_operations`
per model, route by operation (dialogue-state patch → structured-output
model; booking commit → strongest tool model; tone realization → fast
cheap model; RAG rerank → dedicated reranker).

### 11. Native S2S is a benchmark lane, not source of truth → **A/B harness**

Two interchangeable runtimes: (A) cascade [Deepgram Flux → kernel →
Cartesia/ElevenLabs]; (B) native [OpenAI Realtime or Gemini Live →
VoiceOps action gateway].  Native must still send action proposals
through the kernel — must not own business truth.  A/B measures:
response onset, interruption recovery, tool correctness, name/number
accuracy, caller preference, cost per completed task.

### 12. RAG must produce evidence, not prose

Output = evidence bundle: `{answerability, claims[{claim, source_id,
source_span, relevance, freshness}], unsupported_parts}`.  Semantic
planner decides how to speak.  Enables claim-level grounding, mixed
supported/unsupported answers, freshness handling, contradiction
detection, auditable replies, better escalation.

### 13. No learning loop → **Failure Intelligence Pipeline**

For every failed/corrected call, store: failure_type, first_bad_turn,
state_before, state_after, model, prompt_version, tool_schema_version,
audio_condition, human_correction.  Auto-cluster into 10 categories,
each maps to a different fix.  Do NOT solve every failure by expanding
the system prompt.

---

## Track A-E summary (what to build first)

The auditor grouped the twelve tracks into five build-order tracks:

**Track A — Intelligence kernel:** evidence-backed slots + task graph +
deterministic reducer + supersession + semantic plan schema.  Replace
the extractor as operational state; keep for analytics.

**Track B — Safe action engine:** tool classification (read/propose/
commit/compensate), action IDs, idempotency keys, evidence references
on write args, scoped caller confirmation, verify external before
speaking success, cancel/reschedule as first-class.

**Track C — Realtime interaction:** persistent streaming STT per call,
partial+final events through actor, semantic end-of-turn + resumed,
LLM streaming as clauses, TTS chunk streaming, clause-level ledger,
reconcile history to heard text.

**Track D — Human delivery:** acoustic interaction features, delivery
plan from semantic + acoustic state, silence + skip-turn, phrase-level
handling for dates/names/numbers/bad-news, prosodic continuity across
TTS chunks, evaluate voice under telephone compression not studio.

**Track E — Evaluation lab:** deterministic state + tool-sequence
assertions, recorded 8kHz telephone fixtures, ASR substitutions +
noisy-line scenarios, interruption-at-word-N tests, repeated-run
distributions (not single LLM-judge scores), human ratings from real
receptionists, cascade-vs-native A/B on identical calls.

---

## What NOT to build

- Another vertical
- More static prompt examples
- Another regex classifier
- More provider adapters
- More feature flags
- A larger VPL schema
- Another dashboard
- Another LLM judge
- More generic RAG backends

> "Those increase visible breadth while preserving the same intelligence ceiling."

---

## The strongest potential moat (verbatim)

> The most defensible version of this project is not:
> "We connected Twilio, an LLM and a cloned voice."
>
> It is: **A provider-neutral conversation runtime that knows what the
> caller meant, what they corrected, what evidence supports every
> action, what they actually heard, and exactly when an external action
> became real.**
>
> That combines five difficult systems:
> 1. Evidence-backed dialogue state
> 2. Transaction-safe actions
> 3. Semantic turn-taking
> 4. Heard-text memory
> 5. Audio-grounded evaluation

---

## Sprint 10 planning direction

This audit becomes the **Sprint 10 architecture spec**, not just a
task list.  A dedicated `docs/rnd-2026-08/39-intelligence-kernel-spec.md`
lands as the design doc.  Tracks A + B (weeks 1-2) are highest-leverage
and don't require streaming to work.  Track C (weeks 3-4) needs Deepgram
Flux + Cartesia WS.  Tracks D + E interleave.

Do NOT block on this audit before phone testing the current build.
Sprint 9 + Audit 3 P0 fixes are shipped and tested; a call today is
worthwhile to establish baseline for Sprint 10 measurements.
