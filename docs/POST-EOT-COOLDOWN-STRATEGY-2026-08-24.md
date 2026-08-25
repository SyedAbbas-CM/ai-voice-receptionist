# Post-EOT Cooldown Strategy — Decision Doc for ChatGPT Audit

**Date:** 2026-08-24
**Author:** networking-claude (with ChatGPT assist)
**Status:** ACTIVE — awaiting ChatGPT analysis + user decision
**Sibling docs:** `VOICE-AGENT-2.5S-BREAKTHROUGH-AUDIT-2026-08-23.md`, `VOICE-AGENT-SUB-1.5S-RD-ROADMAP-2026-08-23.md`

## Context

Earlier tonight ChatGPT's audit identified two `asyncio.sleep(2.0)` calls in `twilio_actor.py` firing AFTER Deepgram Flux already committed the turn. This was the "2.5s plateau" cause — every model/STT/TTS win we shipped got soaked up by fixed application-side waits.

I shipped an aggressive fix: on the Flux path, both waits become **0ms**. Bounced live on PID 86078.

The user then asked a sharper question:

> "how is this handled by real services? if we say alright or uhuh between these early fires it could be good and responsive but if its a cut out speech from what looks like a bigger thing the user is saying and we say something big we could cut off the user speaking"

**This exposes the design tension:** 0ms is great when the caller is genuinely done (99% of turns) but risks cutting them off when they're mid-dictation and pause (1% but very memorable — phone numbers, addresses, spelling their name).

## What real services do

From `livekit.com/blog/adaptive-interruption-handling` and Vapi/Retell/Bland public docs:

### LiveKit — Adaptive Interruption Handling

Two models running in parallel:
- **Endpointing** ("did they finish?") — semantic. Deepgram Flux is this class.
- **Interruption/backchannel** ("if they spoke while agent talks, is it real or backchannel?") — trained on real conversation audio.

Published tuning knobs:
- `backchannel_boundary.start_cooldown = 1000ms` — adaptive detection suppressed for first 1s, VAD-only fallback (prevents missed real barge-ins)
- `backchannel_boundary.end_cooldown = 1000ms` — late STT transcripts still counted as caller turn (prevents overlap-loss)

**Key quote:** "Backchanneling includes short listener cues such as 'uh-huh,' 'okay,' or 'right' that indicate attention but don't require a response. By filtering these out, the agent avoids unnecessary turn switches caused by brief acknowledgments, incidental sounds, or background noise."

### Vapi — public reliability number

"Vapi's turn-detection model gets it right about 90% of the time, which is the line where the call stops feeling robotic."

**No production service claims 100%.** The other 10% is exactly the user's cut-off concern.

### Retell — the reputational winner

"Retell wins in naturalness testing, with the difference coming from latency tuning and turn detection, not the underlying voice model. Retell's barge-in handling makes it feel faster than the latency numbers suggest."

They win by **feel**, not by TTFT numbers — which suggests the cooldown/backchannel strategy matters more than raw speed.

### Industry consensus pattern: "ack early, act late"

Reply generation split into two phases:

**Phase 1 — Backchannel / filler layer**
- Fast, cheap, always safe
- Utterances: "mmhmm", "gotcha", "one sec", "uh huh"
- Fires on Eager EndOfTurn
- Never commits to anything the caller could contradict

**Phase 2 — Action layer**
- Slower, gated
- The actual answer or booking
- Waits for Final EndOfTurn AND no TurnResumed in cooldown window

Applied to phone dictation:
```
"0333"                                    [Flux Eager EndOfTurn]
    ↓
Agent says "uh huh" (safe filler, no commit)
    ↓
"...5244772"                              [Flux TurnResumed → Final EndOfTurn]
    ↓
Agent commits with full "0333 5244772"
```

## The four options for the receptionist codebase

### Option A — Structured-only cooldown (500ms)

Add a 500ms cooldown after Flux Final EndOfTurn on structured-ask turns only. Non-structured turns keep the current 0ms.

**Trigger:** prior agent utterance contains phone/name/spell/address/email keywords (same detection as the original 2000ms wait, just shorter).

**Impact math:**
- Structured turns: 2000ms → 500ms = ~1500ms savings
- Non-structured turns: unchanged from current PID 86078
- Phone-dictation edge case: 500ms is enough for Flux to detect resumed speech and cancel

**Effort:** ~5 min. Single conditional in `_flush_pending_turn_after_window`.

**Risk:** low. Cooldown is a safety net; if caller was genuinely done, 500ms is barely felt. If they pause > 500ms, we cut them off — but Flux's own `eot_timeout_ms=3000` catches longer pauses, so this is bounded.

**Downside:** still application-side timer, not driven by expected input. A 500ms cooldown on "one" (they said "one hundred") is silly; on "0333" it's necessary. Doesn't distinguish.

---

### Option B — Dynamic cooldown driven by policy-declared expected input

The proper LiveKit-style architecture:

```
NextActionPolicy tells STT: expected_input = PHONE
    ↓
Deepgram runtime reconfigure: eot_threshold = 0.85, timeout = 3500ms
    ↓
Caller dictates phone
    ↓
Flux waits longer before final EndOfTurn on this specific turn

Next turn: expected_input = YES_NO
    ↓
Deepgram runtime reconfigure: eot_threshold = 0.5, timeout = 1000ms
    ↓
Caller says "yeah"
    ↓
Instant commit
```

**Impact math:**
- Structured turns: adaptive, likely 300-800ms cooldown depending on input type
- Yes/no turns: 0ms
- Free-form turns: current 0ms

**CORRECTION (voice-agent review 2026-08-24):** the expected-input signal ALREADY exists in the scaffold:
- `packages/dialogue/next_action_policy.py:176` — `ConversationNextAction.requested_slot: Optional[str]` populated on ASK_SLOT branches with "phone"/"name"/"email"/"date"/"time"/"service"
- `packages/dialogue/plan.py:126` — `SemanticPlan.expected_next_input: list[str]` carries labels like "accept_slot"/"reject_slot"/"request_alternative"

So Option B is NOT blocked on voice-agent's scaffold — the data model is there. What's needed:
1. Voice-agent side: ~15-line shim exposing `requested_slot` to twilio_actor (feature-flagged same as A1)
2. Networking side: Deepgram runtime Configure call keyed on the slot type

**Effort:** voice-agent 2-3h + networking 4-6h = full-day sprint, not a 5-min interim.

**Bench-before-ship blocker:** Deepgram's runtime Configure API spec exists per Flux docs but no repo test yet. Need to verify: (a) cold behavior, (b) warm behavior, (c) mid-turn behavior, (d) packet-loss behavior BEFORE relying on it. See `docs/BENCH-BEFORE-SHIP-2026-08-23.md`.

**Risk:** medium until bench proves the Configure API is reliable.

**Downside:** Option A + POST_EOT_HOLD_MS is a safer path to the same win with fewer moving parts. B is architecturally right but has more surfaces to verify.

---

### Option C — Keep 0ms + add backchannel filler on Eager EndOfTurn

The "ack early, act late" pattern.

Fire a safe backchannel ("mmhmm" / "uh huh" / "gotcha") on Flux **Eager** EndOfTurn — before Final. Real answer still fires on Final.

Applied to phone dictation:
- Caller says "0333"
- Flux Eager EndOfTurn
- Agent: "mmhmm" (short, safe, never commits to any content)
- Caller says "5244772"
- Flux TurnResumed → Final EndOfTurn
- Agent: "Got it — 0333 5244772, confirming?"

**Impact math:**
- All turn types: 0ms to committed action (current)
- Perceived responsiveness: **higher** — caller hears agent acknowledging within ~200ms even during mid-dictation pauses
- Cut-off risk: **zero** — backchannel doesn't commit to anything, so even if caller resumes, we haven't said anything wrong

**Effort (naive):** ~2-3 hours for random-from-set picker. **Effort (correct):** ~1 week — see voice-agent's revision below.

**REVISION (voice-agent review 2026-08-24):** the 2-3h estimate was for a naive random-from-set backchannel picker. That would make the agent feel WORSE not better because listeners are exquisitely sensitive to backchannel repetition.

Real receptionists vary backchannels based on:
- **Caller affect** — frustrated caller → more "I understand" / less "uh huh"
- **Turn count** — first 3 turns warmer, later turns shorter
- **Content signal** — technical answer → "gotcha", emotional → "of course"
- **Recency** — never repeat same backchannel back-to-back
- **Timing signal** — semantic Eager EndOfTurn, NOT pure VAD silence (mid-word acks feel patronizing)

None of that machinery exists in the codebase today. Doing it right is a ~1 week HUMANNESS project, not a speed knob.

**Guardrail if we ever pursue C:**
- Fire ONLY on Flux Eager EndOfTurn (semantic), never on pure VAD silence
- AND only when transcript so far ends on a partial-slot pattern (digits without punctuation, name-half like "my name is Ab...", address-like tokens)
- This restricts backchannels to exactly the dictation edge case and skips them on normal conversational turns

**Risk if we ship the naive version:**
- False-positive filler on non-caller sounds (cough, background) → agent says "mmhmm" to noise
- Repeated backchannels on legit long utterances → agent sounds patronizing
- Backchannel repetition ("mmhmm... mmhmm... mmhmm... yes... mmhmm") → uncanny-valley robot

**Downside:** touches audio-out path AND humanness architecture. Real services do this — LiveKit + Retell both have public backchannel systems that took engineering teams weeks to tune.

**Voice-agent's position (2026-08-24):** defer C until we can do backchannels properly. Not a "one afternoon" project.

---

### Option D — Ship current 0ms, accept 10% edge case

Do nothing more. My 0ms fix ships as-is. Race condition on long-dictation pauses accepted as known limitation.

**Impact math:**
- All turn types: 0ms to committed action
- 90% of turns feel great
- 10% (phone/address dictation with pauses) risk cut-off

**Effort:** 0. Already shipped on PID 86078.

**Risk:** the 10% that gets cut off is the MOST memorable turn types — phone numbers, spelled names, addresses. High salience per event.

**Downside:** user complaints on exactly the "structured slot" flows where receptionist agents are most tested.

---

## Missing observability: `POST_EOT_HOLD_MS`

Regardless of which option, we should ship the `POST_EOT_HOLD_MS` metric ChatGPT's earlier audit recommended:

```
POST_EOT_HOLD_MS = time(brain_dispatch) - time(Flux Final EndOfTurn)
```

If it ever exceeds 500ms, log why:
- `structured_ask`
- `k1_incomplete_word`
- `commit_lock_held`
- `speech_gate_hold`
- `continuation_merge`

Without this, we can't verify any option's win. This is unblocked and I should ship it now regardless.

## Questions for ChatGPT

1. **Does the LiveKit architecture (adaptive interruption + backchannel_boundary cooldowns) map cleanly to Deepgram Flux + our stack, or does it assume LiveKit's specific speech track model?** Concretely: does Flux's `TurnResumed` fire fast enough (<200ms) after caller resumes to cancel an in-flight speculative brain job?

2. **Is Option A (500ms cooldown on structured turns only) a valid interim before Option B (dynamic policy-driven), or does it introduce its own class of bugs?** The concern: it re-introduces a fixed application timer of the exact category we just deleted.

3. **On Option C — how do real services SELECT the backchannel utterance without an LLM?** Is it random from a set, based on prior agent speech (avoid repeating), based on caller emotional signal, or something else?

4. **On backchannel timing — do LiveKit/Retell fire on Eager EndOfTurn (semantic) or purely on VAD silence (acoustic)?** The former is more selective (only when Flux thinks a turn boundary is likely); the latter is more responsive (fires within 100-200ms of any pause).

5. **Is there a lower-risk pattern I'm missing?** For example: fire backchannel on Eager EndOfTurn but ONLY if the transcript so far ends on a partial-number pattern (has digits without terminal punctuation)? That would restrict backchannels to exactly the dictation case and skip them on normal conversation.

6. **What's the wrong answer here?** In your experience auditing voice agents, which of these four causes MORE user complaints — cutting off vs feeling slow vs feeling overly-chatty (constant backchannels)?

7. **Empirical asymmetry check:** in production voice agents, what's the observed complaint rate ratio between "cut me off" vs "too slow to respond" vs "kept saying uh-huh"? User's question is about #1; voice-agent's humanness-lens worry is that #3 in Option C would be worse. Data > intuition here.

8. **Is Deepgram's runtime Configure API idempotent and low-latency?** If it takes 200ms to apply and we call it every turn based on NextActionPolicy, that's 200ms round-trip added to every turn — kills the win from Option B. Bench this before committing to B.

## Joint recommendation — networking + voice-agent (2026-08-24)

Both sessions independently arrived at the same conclusion:

**Ship Option A this bounce (500ms structured cooldown) + POST_EOT_HOLD_MS observability metric, bundle with voice-agent's A1/A2 NextActionPolicy wiring (flag=off default).**

Reasoning:
- Solves the caller-cut-off concern immediately
- Preserves ~90% of the plateau-break win
- Adds observability so the next decision is data-driven
- Voice-agent's NextActionPolicy stays inert (flag=off) so we can measure networking-side fixes cleanly first, then flip humanness-side later
- Defers B until we can bench the Deepgram Configure API safely
- Defers C until we can do backchannels RIGHT (humanness project, not a knob)

**Defer Option B until:**
- Deepgram runtime Configure API is bench-verified (cold, warm, mid-turn, packet-loss)
- Verified the Configure call latency doesn't eat the win it enables (voice-agent's Q8)

**Defer Option C until:**
- We have POST_EOT_HOLD_MS data showing which turn types actually hit the cooldown
- We have budget for the ~1 week humanness project to do backchannels right (voice-agent's warning: naive random-from-set picker makes agents feel WORSE, not better)

## Composition note — K1 skip + Option A structured cooldown

Both protections coexist and cover disjoint failure modes:

- **K1 skip on Flux path** catches non-structured turns whose transcript ends on a connective (`and`/`but`/`or`/`before`/`after`/`of`). These are rare but real ("we can meet at" → K1 would hold 2s). Skip is correct on Flux because Flux's semantic EOT is authoritative for these.
- **Option A's 500ms cooldown** catches structured-ask turns (phone/name/address/email dictation) where the caller pauses mid-slot and Flux fires Eager. These are 10% of turns but the MOST memorable per-event.

Overlap only in the narrow case of a structured turn where the caller says "phone is uhh five five five" and pauses on a connective. In that case both fire; the cooldown wins (longer). Correct behavior.

**CRITICAL coupling — both must be Flux-gated symmetrically.** The K1 skip is gated on `settings.deepgram_use_flux` (only fires on Flux path). Option A's cooldown MUST be similarly gated: on Flux → 500ms, on Nova-3 → keep the original 2000ms. This makes the Flux-vs-Nova-3 switch atomic — one env flag flips both semantic-detection paths together, no half-configured state. If someone toggles `DEEPGRAM_USE_FLUX=false` for A/B or outage fallback, both cooldowns revert to Nova-3-appropriate values together.

Failure mode this prevents: shipping Option A ungated (500ms on both paths). If Flux goes down and we fall back to Nova-3, Nova-3 would then only get 500ms — regression on the exact class of turns the 2000ms was originally added to protect (CA7eb96fd trace: 3 separate turn commits over 5 sec on a phone dictation).

## Waiting on

- ChatGPT analysis of the 8 questions above
- User confirmation on which option to ship
- Then bundle-bounce: voice-agent's A1/A2 wiring (flag=off) + networking-side Option A (500ms structured cooldown, Flux-gated) + POST_EOT_HOLD_MS metric
