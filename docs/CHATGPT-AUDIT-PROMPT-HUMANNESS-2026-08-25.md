# ChatGPT Audit Prompt — Humanness / Voice-Agent Behavior
**Bundle:** `receptionist-codebase-2026-08-25_1312-audit-2026-08-25.zip` (same bundle as backend audit)
**Scope:** everything the CALLER experiences on the phone. Turn-taking, prompt quality, ack policy, hangup, barge-in, voice consistency, semantic-plan wiring, disclosure phrasing.
**Explicitly OUT OF SCOPE:** backend security, tenant isolation, DB, CRM writes, HIPAA infra (those live in `docs/CHATGPT-AUDIT-PROMPT-BACKEND-2026-08-25.md`).

---

## Paste this into ChatGPT along with the zip:

You are auditing a production Twilio-based voice receptionist SaaS for **humanness on live calls**. Python + FastAPI + Deepgram Flux STT + OpenAI LLM (gpt-4o-mini) + ElevenLabs Flash v2.5 TTS + Twilio Media Streams.

The core question: **would a real caller distinguish this from a human receptionist within the first 3 turns?** If not, what specific code/prompt/wiring changes would move it closer?

I care about behavior in the wild, not code aesthetics. Rank findings **P0 / P1 / P2** by "how much this hurts the caller's perception per turn." Concrete file:line citations required. Every finding must be either a code delta, a prompt delta, or a wiring change — never "add a review process."

---

## Context you need

**What already exists:**
- `packages/core_agent/prompt.py` — the receptionist persona + rules (17k chars, cache-optimized)
- `packages/core_agent/brain.py` — the tool loop that handles each user turn
- `packages/core_agent/next_action_synthesizer.py` — deterministic post-tool renderers (booking confirmation + slot proposal from `check_availability`)
- `packages/dialogue/next_action_policy.py` — scaffold for the runtime controller (feature-flagged off by default via `settings.next_action_policy_enabled`)
- `packages/dialogue/plan.py` — SemanticPlan type with `requires_deterministic_template()`
- `packages/dialogue/reducer.py` — dialogue-state reducer
- `apps/api/app/routes/twilio_actor.py` — 6165 lines, turn-taking + barge-in + hangup + speech-commit gate + one-gen-lock
- `packages/voice/conversation_control.py`, `filler.py`
- `packages/runtime/streaming_stt_bridge.py`, `turn_manager.py`
- `packages/slot_parsers/` — StructuredInputSession + phone/name/email registry (phone wired, others deferred)
- `packages/compliance/jurisdiction.py` — recording-consent state map, boot audit
- `packages/compliance/pii.py`, `tcpa.py`

**What already shipped this month:**
- Random-dates hallucination fix via SlotProposalRenderer (LLM can't speak times not in `check_availability` result)
- "Grown-up misfire" — CHILD CALLERS rule requires 2+ signals
- POST `/twilio/status` route + logging
- HubSpot / FollowupSink CRM writes
- Never-say-AI identity rule (redirects rather than admits/denies)
- Semantic acknowledgment policy in prompt (match ack to content type; no-ack during dictation)
- Deterministic hangup via Twilio REST + shortened farewell delay 4s→2.5s
- Recording-notice text: "This call may be recorded for quality" when enabled
- AI-disclosure text: "You're speaking with our automated receptionist" (matches never-say-AI rule)

**What real callers still complain about (verbatim from user):**
- "doesn't hang up" (may be fixed with today's Twilio REST call termination — verify)
- "doesn't acknowledge sentences" (partial-fix via ack policy rewrite — verify)
- "shouldn't say it's an AI when asked" (fixed via prompt policy)
- "not using its intelligence" — feels like a fast chatbot despite having reactive-brain / next-action-policy / dialogue-state infrastructure sitting mostly dormant
- Times: sometimes says "today" when it means tomorrow, or invents times not in the calendar (fixed 2026-08-25 via SlotProposalRenderer — verify)
- Grown-up trigger fires on adult callers with the wrong signals (rule tightened 2026-08-25 — verify)

---

## Audit dimensions

### 1. TURN-TAKING & BARGE-IN
- Does `twilio_actor.py` gate the greeting on caller-speech-received OR a bounded timer (whichever first), so a caller who dials + immediately speaks doesn't get talked over?
- Two-stage barge-in (duck → confirmed-interrupt vs backchannel): calibrated correctly? False positives (agent ducks on background noise)? False negatives (agent talks over a real interrupt)?
- Farewell hangup delay (currently 2.5s post-Twilio-REST): too long, too short, or just right? Compared to what real receptionists do.
- Structured-input holds (K1 skipped on Flux, 500ms cooldown on Flux for phone/name/address dictation) — right primitives? What's still missing?
- `SpeechCommitGate` — holds WAIT_PROMISE / ACTION_CONFIRMATION until tool receipts fire. Does the release logic actually match what the caller expects to hear?

### 2. ACKNOWLEDGMENT POLICY
- Current prompt tells the LLM to vary acks and never emit a standalone ack sentence. Is that enough? Where does it fail?
- Backchannel language ("mmhmm", "uh huh") — the audit says these should fire on Eager EndOfTurn during long caller utterances. Should the agent produce them at all? If yes, what's the right signal and what's the right selector (random from set vs affect-aware vs recency-aware)?
- No-ack-during-dictation rule — implemented in prompt. Does the LLM actually respect it, or does it need a deterministic gate?
- Semantic acks ("Ah, I see" vs "Got it" vs "Yeah") — implementable via prompt alone, or does it need `NextActionPolicy` running LIVE to decide?

### 3. PROMPT / PERSONA
- Read `packages/core_agent/prompt.py` end-to-end. Which rules will the LLM ignore under load? Which rules contradict each other? Which are redundant with `next_action_synthesizer.py` renderers?
- Length constraints ("yes-no 3-8, quick answer 10-20, booking confirm 25-40") — does gpt-4o-mini actually respect them, or does it trend long?
- No-reflex-openers rule ("Sure!", "Okay,", "Perfect!") — LLM tell rate? Is a sanitizer needed?
- Persona field (`voice_persona: str`) — how much does it actually shape output? Should there be more structure ("warm/professional/concise" is thin).
- "Never speak internal descriptions" rule — implemented after a real leak. Are there other classes of meta-describe still leaking?
- Adaptive delivery (6 affect states in prompt) — does the LLM actually route between them, or does it always default to neutral?

### 4. HALLUCINATION SURFACE
- `SlotProposalRenderer` deterministically renders `check_availability` slots. Are there OTHER tool paths where the LLM freeforms and could hallucinate?
- Booking confirmation renderer — active. What about `find_existing_appointment`, `cancel_appointment`, `reschedule_appointment`?
- FAQ lookups (`lookup_faq` returning "no_match") — does the LLM invent an answer or fall back gracefully?
- Business hours / address / phone — the prompt tells the LLM to use `{phone}` `{address}` verbatim; does that hold, or does it drift?

### 5. NEXT-ACTION-POLICY WIRING
- Scaffold exists but flag is default False. What breaks if we flip it True on live traffic?
- `render_from_semantic_plan()` entry point exists but no upstream planner emits `SemanticPlan` per turn — dead code path or intentional preparation?
- ChatGPT audit view: is the current architecture a coherent phased plan, or is it Chesterton's Fence with unclear ownership?
- What's the minimum wiring to make one turn type (say, ASK_SLOT for phone) fully deterministic end-to-end?

### 6. STT / EOT TUNING
- Deepgram Flux `eot_threshold` currently config-set. Should this be adaptive per turn type (`requested_slot=phone` → longer threshold; `requested_slot=yes_no` → shorter)?
- Structured-input cooldown 500ms on Flux path — right value or arbitrary? What's the empirical justification?
- Speculative brain (fires on Eager EndOfTurn, kills on TurnResumed) — how often does it save latency vs how often does it waste tokens?

### 7. TTS EXPRESSIVENESS
- ElevenLabs Flash v2.5 has a Humanness Index score of 68/100 (Vapi's blind test). The user is considering Vapi/Elliot for its 88/100 Grok TTS backend but doesn't want to migrate telephony.
- Is there any way to get expressiveness closer to human WITHOUT swapping providers? Voice settings (stability / similarity / style), sentence-splitting for prosody, SSML tags Flash supports?
- Filler pool warmup exists — are the filler clips actually appropriate ("mmhmm" / "let me check") or robotic?
- Is there a way to detect "the agent sounds robotic" without a real caller?

### 8. DISCLOSURE / IDENTITY POLICY
- Never-say-AI rule shipped 2026-08-25. Utah §13-77-103 requires disclosure when directly asked. The current redirect ("I'm the virtual receptionist for {business_name}") — does it satisfy Utah? Does it satisfy California SB 1001?
- Recording notice ("This call may be recorded for quality") — is this legally sufficient in all two-party consent states, or does specific wording matter per state?
- If a caller in Illinois asks "is this recorded?" — must we disclose actively, not just at greeting?

### 9. EDGE CASES REAL CALLERS HIT
- Caller says "hold on" and puts phone down for 30 seconds — how does the agent handle silence? Timeout too aggressive? Too passive?
- Caller sneezes / coughs mid-sentence — does STT interpret it as a full utterance?
- Caller with strong accent — do phone/name parsers still work?
- Caller who talks fast + interrupts every partial — does the agent thrash?
- Caller who never gives their name and just books — does the flow gracefully continue?
- Caller who says "actually, I need to reschedule" mid-booking — does the agent switch contexts?
- Caller who says "let me call you back" — does the agent politely end without pushing?

### 10. RELIABILITY UNDER STRESS
- What happens on token cost spike (LLM slow) — does the caller hear silence or a filler?
- What happens on TTS provider blip — silence, then recovery, or apologetic message?
- What happens on Deepgram Flux going down — fallback to Nova-3 works?
- What happens on 3+ concurrent calls to one Twilio number — real capacity or dropped?

---

## Output format

For each finding:

```
[Pn] <one-line title>
File: <path:line> OR "prompt.py § <section>"
Symptom (caller-visible): <what the caller experiences>
Root cause: <one sentence>
Fix (concrete): <2-4 lines of code, a specific prompt delta, or a wiring change>
Verification: <how to check the fix worked — what to listen for on a real call>
```

Rank findings globally. Give a **"first-3-turns list"** — items that hurt the caller's perception in the opening 3 exchanges (the highest-value fixes).

Skip P2 findings entirely if there are more than 10 P0/P1s. Depth over breadth.

## What NOT to audit this round

Backend security, tenant isolation, DB retention, CRM outbox, admin flows — those live in `docs/CHATGPT-AUDIT-PROMPT-BACKEND-2026-08-25.md`. If those come up here, redirect: "That's out of scope — focus on the caller experience."

---

## Follow-up prompts (after initial audit)

**Turn-taking drill:**
> For each turn-taking finding, describe how a real receptionist handles that exact scenario. Not "a chatbot should" — a specific script of what a human dental receptionist WOULD say + WHEN they'd say it. Then map that to a code delta.

**Prompt drift:**
> Read prompt.py from top to bottom. List every rule where you believe gpt-4o-mini has a >20% chance of ignoring it under load. Rank by caller impact. For each, tell me if it's fixable by moving the rule earlier in the prompt, by adding a deterministic sanitizer, or by wiring it into next_action_synthesizer.

**Vapi/Elliot comparison:**
> Assume we're testing Vapi Elliot V2 on the same clinic prompt tomorrow. What SPECIFIC behavioral gaps do you predict Vapi would still have that we could match or exceed with our stack (given Deepgram Flux + gpt-4o-mini + ElevenLabs Flash + our reactive-brain architecture)? What would Vapi genuinely do better?

**A/B test design:**
> Design an A/B test that measures humanness without needing a real caller pool. Score turn-taking, acknowledgment appropriateness, hallucination rate, and "feels like a person" perception. Can use LLM-as-judge but must be reproducible.

**Retell parity:**
> Retell wins reputation battles on naturalness. What SPECIFIC features (from public docs) explain their edge, and which of those could we implement in <1 week each? Rank by impact-per-hour.

**Prompt compaction:**
> prompt.py is 17k chars. What can be removed without changing behavior? What can be moved from prompt to code (deterministic renderer)? Target: <10k chars.

---

## Reference documents I want you to read INSIDE the bundle

- `packages/core_agent/prompt.py` — the persona + rules
- `packages/core_agent/next_action_synthesizer.py` — deterministic renderers
- `packages/dialogue/next_action_policy.py` — the scaffold
- `apps/api/app/routes/twilio_actor.py` — the huge behavior file (~6165 lines, sorry)
- `docs/BENCH-BEFORE-SHIP-2026-08-23.md` — our discipline for tunable-knob changes
- `docs/VOICE-AGENT-SUB-1.5S-RD-ROADMAP-2026-08-23.md` — the A1-A36 roadmap of planned changes
- `HUMANNESS-RECOMMENDATION-2026-08-20.md` — earlier humanness analysis
- `deep-research-report-humanness.md` — deeper humanness research

Skim the docs for CONTEXT of what's been tried. Don't repeat previous conclusions unless you're overriding them with new evidence.
