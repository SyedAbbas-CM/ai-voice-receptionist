# MASTER PRIORITY TODO — 2026-08-20

> **New source of truth.** Synthesized from the current codebase, working notes, implementation plan, market research, speed/network research, three humanness rounds, historical audits, TTFT benchmark, and real-call transcripts.
>
> **Priority:** leverage × ease × evidence. Older documents remain evidence, but this file wins when priorities conflict.
>
> **Goal:** a receptionist that responds quickly, sounds naturally concise, can be interrupted, preserves exact facts, completes business actions safely, and continues work across phone/SMS/WhatsApp.
>
> **Voice-loop engineering target:** normal caller-end → first useful audible speech toward **500–700 ms p50**, while tracking p95 and task correctness.

# 1. EXECUTIVE ARCHITECTURE

The research now converges on:

```text
Caller
→ Twilio call-long WSS
→ Deepgram call-long WSS
→ ConversationState
→ ConversationNextActionPolicy
→ fast LLM verbalizer
→ complete useful sentence
→ ElevenLabs call-long multi-context WSS
→ Twilio
```

Underneath it:

```text
Customer
→ BusinessTask
→ BusinessOutcome
→ Business NextActionPolicy
→ ActionScheduler
→ Outbox
→ Calendar / CRM / SMS / WhatsApp
→ Outcome again
```

**Important:** there are two NextAction policies:
- `ConversationNextActionPolicy`: what conversational move to make next this turn.
- `T-SP6 Business NextActionPolicy`: what durable business action/follow-up happens next.

Do not merge them.

# 2. VERIFIED CURRENT STATE

## 2.1 Already shipped — do not reopen without regression evidence
- Multi-call isolation verified at **n=10 simultaneous WebSockets**.
- `T4a` lock-ownership fix.
- `T3.6` K1/double-response fixes.
- Exact date/time prompt rules.
- Booking-confirmation truth rules.
- Phone handling and compliance/hallucination guardrails.
- `T-SP1` narrow SemanticPlan integration.
- OpenAI Fast service tier.
- OpenAI `prompt_cache_key` + 24h retention + cache telemetry.
- Full production tool-schema warmup.
- Groq shared HTTP/2 client + native streaming.
- Twilio `clear`.
- Raw Twilio μ-law forwarding to Deepgram.
- ElevenLabs `ulaw_8000`.
- Response-cache infrastructure.
- Playback generated/sent/heard/cleared ledger.
- Flux provider implementation, though still OFF.

## 2.2 Load-bearing prompt sections
Current file: `packages/core_agent/prompt.py`.

Preserve the meaning of:
- current date/time: ~15–27
- TIME HANDLING: ~261+
- HALLUCINATION GUARDRAILS: ~302–322
- BOOKING CONFIRMATION: ~324–333
- PHONE NUMBER HANDLING: ~335–358
- COMPLIANCE / SAFETY: ~360+
- SemanticPlan contract

Future prompt compression should remove duplication around them, not rewrite them.

## 2.3 LLM latency baseline
Evidence: `50_BENCH_llm-ttft-bench-2026-08-20_012206.md`.

| Provider | Median first token |
|---|---:|
| OpenAI `gpt-4o-mini` normal, full prompt | **1534 ms** |
| OpenAI Fast, full prompt | **772 ms** |
| Groq OSS20B, small prompt | **485 ms** |

Groq full-prompt behavior was not cleanly production-viable in that run, so it is a candidate **fast lane**, not a justified global replacement.

## 2.4 Prompt is still large
Direct current-code inspection:
```text
SYSTEM_TEMPLATE ≈ 22,094 chars / 384 template lines
old TTFT benchmark prompt ≈ 24,609 chars
```
The humanness rewrite improved style but did **not** finish prompt compaction.

## 2.5 Output cap is still flat
Normal hot paths still use `max_tokens=200`:
- `packages/core_agent/brain.py:453-455`
- `packages/core_agent/brain.py:515-518`
- `packages/core_agent/brain.py:857-862`

Newest research supports speech-act-specific caps instead.

## 2.6 ElevenLabs currently reconnects per synthesis
Current phone settings:
```text
eleven_flash_v2_5
ulaw_8000
stability=0.5
similarity_boost=0.75
```
Code:
- `apps/api/app/providers/tts/elevenlabs_tts.py:123-203`
- `apps/api/app/routes/twilio_actor.py:2916-2927`

`ws_stream_synthesize()` creates a fresh `websockets.connect(...)` for each synthesis. Newest research recommends **one multi-context ElevenLabs socket per phone call**, with one logical context per assistant generation.

## 2.7 Twilio `clear` already exists
Code: `apps/api/app/routes/twilio_actor.py:4587-4596`.

Remaining task is to prove:
```text
caller interrupts
→ LLM generation cancelled
→ active ElevenLabs context cancelled
→ local speech queue stops
→ Twilio clear sent
→ stale audio does not leak
→ caller gets floor
```

## 2.8 Zero-transcode is partly already shipped
Inbound:
```text
Twilio μ-law 8 kHz → raw μ-law → Deepgram
```
Code: `packages/runtime/streaming_stt_bridge.py:128-157`.

Do not schedule a broad codec rewrite. Remove only conversions proven redundant on the real phone hot path.

## 2.9 Flux exists but stays experimental
Code:
- `apps/api/app/providers/stt/deepgram_flux_stt.py`
- `apps/api/app/core/config.py:158-168`

Current: `deepgram_use_flux=False`.

Historical issues:
- prior Twilio μ-law Flux test produced empty events;
- Flux language coverage does not include Urdu.

Therefore benchmark it; do not blindly enable it.

## 2.10 TTS slider tweaking is not the main humanness fix
Current `stability=0.5 / similarity=0.75` is already near a sensible ElevenLabs starting region.

Four stock voice IDs suggested by earlier research were tested against the authoritative API and all failed. Do not reuse them. Sarah remains the known working voice.

Future voice replacement must use IDs actually available in the account/API. Do not build a voice-cloning subsystem now.

## 2.11 Real-call ground truth
Recent p50 caller-end → first agent audio:

| CallSid | p50 |
|---|---:|
| `CAa8d6d3d6751eea6856cb18b53c0ed7c2` | **1.53 s** |
| `CA0aee80af478ca22ff0ef62e34196549b` | **1.59 s** |
| `CA813939979953915430efb2bb492ffa4e` | **1.69 s** |
| `CAa27d06e06da8060f182fe26841777ed1` | **1.70 s** |
| `CAe88134d2959e8f4c0e8933d731d9a8b0` | **2.17 s** |
| `CAbbfbb5f0ee06c0e57a2ae647387c4ea3` | **2.77 s** |
| `CA6a8777572ad6ea6d0e4dbd33d85a379e` | **3.33 s** |
| `CA53ba57a40c33197af3febd05f6243a65` | **4.35 s** |

Most important call: `CAa8d6d3d6751eea6856cb18b53c0ed7c2`.
- ~93 sec / 13 turns
- p50 1.53 s
- explicit caller feedback: robotic and needs more speed

Recorded stage estimates from that investigation:
```text
STT ~350–700 ms
ElevenLabs first byte ~290–310 ms
pre-Fast OpenAI first token ~1900–2200 ms
```

# 3. GUIDING PRINCIPLES
1. **Conversation behavior beats decorative humanization.** Timing, concise contingent responses, correction, interruption, and state matter more than “um/uh” or exaggerated emotion.
2. **Treat one call as one realtime session.** Keep provider sessions call-long where supported.
3. **Policy decides; LLM verbalizes.** Move the decision about what to do next out of free-form language generation.
4. **Measure causal latency.** EOT → model → first complete useful sentence → TTS → Twilio matters more than isolated TTFB.
5. **Build durable business state.** `Customer → BusinessTask → Outcome → NextAction → Scheduler → Outbox` is the reusable commercial layer.

# 4. SUPERSEDED / KILLED RECOMMENDATIONS

| Old recommendation | Current decision | Why |
|---|---|---|
| New ElevenLabs WS per sentence | **Killed** | Newer provider research supports call-long multi-context. |
| One ElevenLabs WS per assistant turn | **Superseded** | One WS/call; logical context per turn. |
| Fix robotic sound mainly with stability/similarity | **Killed as primary strategy** | Current values already reasonable; behavior/timing dominate. |
| Talia/Chelsea/Maisie/Jade IDs | **Killed** | 4/4 failed authoritative API verification. |
| Frequent random disfluencies | **Killed** | Can sound scripted/less intelligent. |
| Full `utterance_id` refactor now | **Held** | Only if same-generation multi-fire still exists. |
| Another global LLM provider swap | **Superseded** | OpenAI Fast already roughly halved full-prompt TTFT. |
| Implement Twilio `clear` | **Already done** | Verify complete cancellation chain instead. |
| Rewrite Twilio→Deepgram μ-law path | **Already done** | Raw μ-law forwarding exists. |
| Global `max_tokens=120` | **Superseded** | Use speech-act caps. |
| Predict generic “Sure!/Perfect” openers | **Deprioritized** | Risks repetitive robotic speech. |
| OpenAI Realtime now | **Deferred** | Replaces hard-chosen STT/TTS stack; cheaper wins remain. |
| Voice cloning now | **Deferred** | No present product requirement. |
| HubSpot now | **Deferred** | Build generic CRM contract + GHL first. |

# 5. 🔴 CRITICAL — THIS WEEK, ~≤10 ENGINEERING HOURS

## P1 — `NEW-EL-CALL-SESSION`: one ElevenLabs multi-context WS per call
**Estimate:** 3–4h  
**Leverage:** very high  
**Evidence:** `21_SPEED...:35-68`; `33_HUMANNESS_RESPONSE3...:1028-1043`.

Current:
```text
assistant turn → ws_stream_synthesize() → connect → speak → close
```
Target:
```text
CALL START → open multi-context socket once
TURN N → context N → complete useful sentence(s) → finish context
BARGE-IN → cancel active context
CALL END → close socket
```

Likely files:
- `apps/api/app/providers/tts/elevenlabs_tts.py`
- `apps/api/app/routes/twilio_actor.py`
- possibly `packages/runtime/call_actor.py`

Suggested call-owned contract:
```python
ElevenLabsCallSession.open()
create_context(context_id)
send_text(context_id, text)
close_context(context_id)
cancel_context(context_id)
close()
```

Constraints:
- no process-global call state;
- keep Flash v2.5;
- keep `ulaw_8000`;
- feed complete/prosodically useful sentences, not arbitrary token fragments.

**Done when:** a 10-turn real call shows one TTS WS handshake/call, distinct contexts, no reconnect/turn, clean context cancellation, no stale audio, and no naturalness regression.

## P2 — `NEW-GEO-US-EAST-A-B`: geography experiment
**Estimate:** ~2h  
**Leverage:** high  
**Evidence:** `21_SPEED...:24-29`, `264-271`, `340-350`.

Run exact same app/config:
```text
A = current Pakistan/local+tunnel
B = simple US-East Linux host
```
Same scripted phone calls.

Compare:
```text
EOT→first useful audio p50/p95
STT stage
LLM stage
TTS first audio
Twilio first send
```

**Decision:** if material, US-East becomes demo default. If negligible, deprioritize geography and stop speculating about it.

## P3 — `T-SP-SPEED-EXTRA-B2`: speech-act token budgets
**Estimate:** 1–1.5h  
**Evidence:** `33_HUMANNESS_RESPONSE3...:393-409`.

Replace flat 200 with one centralized policy:
```text
ACKNOWLEDGE       20
CLARIFY           32
ASK_SLOT          40
TOOL_PREAMBLE     32
DIRECT_ANSWER     48
BOOKING_PROPOSAL  64
FINAL_CONFIRM     80
COMPLEX          120
EMERGENCY         96
```
Implement `token_budget_for_speech_act(act)`. Unknown fallback ≈80.

Never truncate safety/emergency text or required booking confirmations.

**Done when:** routine token counts fall materially, first-complete-sentence/whole-turn latency improves, and golden booking/safety tests remain correct.

## P4 — `T-SP-SPEED-EXTRA-F`: compact production prompt
**Estimate:** ~2h  
**Leverage:** high

Current ≈22,094 chars. Initial target:
```text
22k → ~12–15k
```
Do not force 8k if correctness degrades.

Remove first:
- duplicate conversational rules;
- verbose rationale;
- examples teaching the same pattern;
- repeated persona content;
- implementation-history prose;
- instructions guaranteed elsewhere.

Preserve time/date, phone, booking-confirmation, hallucination, compliance/safety, SemanticPlan.

**Done when:** full production prompt + actual tool schemas show measurable TTFT/input improvement with no critical correctness/humanness regression.

## P5 — `T-SP-RELIABILITY-3`: verify same-generation TTS ownership
**Estimate:** ≤15m.

Run one fresh 5+ LLM-turn call. Count `TTS_STREAM_START` per generation.

Expected:
```text
<=1 intended TTS stream per speech generation
```
If clean, keep `T-SP-SPEED-3`/full utterance-ID refactor deferred. If not, reopen with trace evidence.

## P6 — `T-SP-RELIABILITY-4`: prove response cache on a real phone call
**Estimate:** ≤15m.

Ask hours, address, insurance, parking, services, then repeated variants. Verify `RESPONSE_CACHE HIT` and end-to-end improvement.

# 6. 🟡 NEXT — 2–4 WEEKS

## P7 — `NEW-CONVERSATION-POLICY`: turn-level ConversationState + NextActionPolicy
**Estimate:** 1–2 days  
**Leverage:** extremely high.

This is **not T-SP6**.

It chooses the next spoken move:
```text
ACKNOWLEDGE
CLARIFY
ASK_SLOT
ANSWER
TOOL_PREAMBLE
PROPOSE_SLOT
CONFIRM_ACTION
REPAIR_MISHEAR
ESCALATE
END_CALL
```

Compose existing state instead of inventing another transcript/state system:
```python
ConversationDecisionState(
    conversation_phase,
    caller_affect,
    caller_style,
    urgency,
    known,
    missing,
    tool_pending,
    requires_confirmation,
    pending_tasks,
)
```
Return:
```python
ConversationNextAction(
    action,
    requested_slot=None,
    tool=None,
    delivery_intent=None,
    max_tokens=None,
    must_include_facts=[],
)
```

Then the LLM receives a decided action and verbalizes it.

Keep T-SP1/SemanticPlan as grounded fact/task input.

**Done when:** golden calls show one useful move/turn, one question by default, fewer “Perfect/Absolutely” loops, shorter speech, and unchanged task/tool correctness.

## P8 — `T-SP-SPEED-2`: Nova-3 vs Flux, then EagerEndOfTurn
**Estimate:** 0.5–1 day.

Sequence:
1. replay recorded Twilio μ-law into Flux;
2. prove normal transcripts are non-empty;
3. Nova vs Flux with eager speculation OFF;
4. enable EagerEndOfTurn only after that;
5. measure false cuts + extra speculative LLM calls.

Research starting point:
```text
eager threshold ≈0.4
final EOT ≈0.7
```
Current config ≈0.5/0.7. A/B, do not blindly copy.

Adopt only for supported languages if recognition and p50/p95 improve. Keep Nova for Urdu/unsupported languages.

## P9 — `NEW-INTERRUPTION-E2E`: prove barge-in through full playout chain
**Estimate:** 2–3h.

Prove:
```text
caller interruption
→ current model cancelled
→ ElevenLabs context cancelled
→ local queued speech discarded
→ Twilio clear
→ playback ledger records cleared-vs-heard
→ new caller turn begins
```
**Done when:** caller interrupts a deliberately long answer after ~500ms and hears no stale continuation.

## P10 — `T-SP-SPEED-4`: policy-driven fast-brain routing
**Estimate:** ~1 day.

Use lanes, not global provider replacement.

**Lane A — deterministic/no LLM**
- response cache
- fixed greeting
- safe state-derived response

**Lane B — fast model**
- simple clarification
- short paraphrase of pre-decided action
- low-risk language realization

**Lane C — capable model**
- tool planning
- ambiguity
- difficult correction
- multi-step/sensitive reasoning

OpenAI Fast remains baseline. Groq is a fast-lane candidate.

Log:
```text
speech_act
lane
provider
model
first_token_ms
fallback_reason
```

Promote only if successful-task latency improves without more tool/time/booking errors.

## P11 — `T-SP-SPEED-EXTRA-E`: Predicted Outputs, benchmark only
**Estimate:** ~1h.

Do not broadly predict “Sure!” / “Perfect,”. That can improve TTFT and worsen humanness simultaneously.

Test only after ConversationNextActionPolicy. Prefer a useful authoritative prefix such as a known time/date. Kill if repetition rises or semantic accuracy drops.

## P12 — `T-SP2`: production CalendarAdapter
**Estimate:** ~1 day  
**Commercial leverage:** extremely high.

Contract:
```python
get_availability(...)
find_booking(...)
create_booking(..., idempotency_key)
reschedule_booking(..., idempotency_key)
cancel_booking(..., idempotency_key)
```

Likely files:
- `packages/integrations/calendar_adapter.py`
- `packages/integrations/google_calendar.py`
- `packages/integrations/calendar_commit_adapter.py`

Requirements:
- `CommitCoordinator` for authoritative writes;
- business-local timezone;
- configurable hours;
- idempotent create/reschedule/cancel.

**Done:** real call produces exactly one real Google Calendar event at correct local time and safely modifies/cancels it.

## P13 — `T-SP3`: EvidenceBundle → SemanticPlan
**Estimate:** 4–6h.

Existing:
- `packages/rag/evidence.py`
- SemanticPlan

Flow:
```text
retrieved evidence
→ EvidenceBundle
→ PlannedFact(source=rag:<chunk>)
→ spoken realization
```
Use first for price/service/policy/business-fact answers.

**Done:** exact values survive conversational phrasing and remain source-traceable.

## P14 — `T-SP4`: Customer + CustomerIdentity
**Estimate:** ~1 day.

Persistent:
```text
customers
customer_identities
customer_facts
```
Resolver:
```text
phone / WhatsApp / email / GHL ID → same Customer
```
**Done:** same person across channels resolves to one customer.

## P15 — `T-SP5`: durable BusinessTask
**Estimate:** 1–2 days.

Do not mutate conversation-local TaskState into persistence.

Examples:
```text
BOOK_APPOINTMENT
RESCHEDULE
CALLBACK_REQUEST
MISSED_CALL_RECOVERY
QUOTE_FOLLOWUP
```
**Done:** customer can hang up and continue the same task later.

## P16 — `T-SP6`: BUSINESS OutcomeEngine + NextActionPolicy + Scheduler
**Estimate:** 2–3 days.

Example:
```text
BOOKED
→ SMS confirmation now
→ reminder before appointment
→ CRM sync now
```
or:
```text
NO_ANSWER
→ wait
→ SMS
→ retry later
```

Policies:
- priority
- channel
- timing
- consent
- callback
- escalation

**Done:** follow-up is deterministic and durable rather than dependent on LLM memory.

## P17 — `T-SP7`: Outbox + DeliveryReceipt + Retry + Reconciliation
**Estimate:** 1–2 days.

Required:
```text
OutboxEvent
OutboxService
OutboxWorker
DeliveryReceipt
RetryPolicy
ReconciliationService
```

Protect against:
```text
Calendar succeeds
CRM fails
SMS fails
```

**Done:** authoritative booking remains correct, failed side effects persist/retry, unresolved drift is visible.

## P18 — `T-SP8`: tenant runtime config
**Estimate:** 1–2 days.

Introduce:
```text
TenantRuntimeConfig
TenantConfigRepository
TenantSecretResolver
```

**Done:** simultaneous tenants can use different profiles, calendars, CRM credentials, KBs and channels without restart/credential bleed.

## P19 — `T-SP9`: DNIS routing
**Estimate:** ~0.5 day.

```text
Twilio To/DNIS
→ tenant
→ location
→ TenantRuntimeConfig
```

High value for agency/multi-location deployments.

## P20 — `T-SP10`: CRMAdapter, GHL first
**Estimate:** 1–2 days.

Do not rebuild GHL. Put current operations behind a generic contract covering:
- customer lookup/upsert
- notes
- opportunity/task update
- tags
- DNC/consent where relevant
- reconciliation hooks

No HubSpot implementation yet.

## P21 — `T-SP11`: shared SMS + WhatsApp business state
**Estimate:** 1–2 days.

SMS:
- inbound
- outbound
- delivery receipts
- STOP handling
- customer resolution

WhatsApp:
- connect existing transport to Customer/BusinessTask/NextAction/Outbox
- do not build another transport

**Done:** phone-booked customer can reschedule via SMS/WhatsApp against same durable task.

## P22 — `T-SP12`: killer dental demo
**Estimate:** ~1 day.

Flow:
```text
phone call
→ natural fast conversation
→ availability
→ safe booking
→ real Google Calendar
→ Customer
→ BusinessTask
→ SMS confirmation
→ GHL update
→ reminder scheduled
→ later SMS/WhatsApp reschedule
→ reconciliation
```

Include one clean barge-in:
```text
Agent: "I've got openings at—"
Caller: "Actually, afternoon only."
Agent stops.
Agent: "Gotcha — afternoon. Two thirty or four?"
```

**Done:** three complete rehearsals without manual intervention, then one 4–6 minute recording.

# 7. 🟢 LATER

## L1 — `T-SP-SPEED-6`: OpenAI Responses persistent WS
Only after US-East, call-long TTS, prompt compaction and speech-act budgets.

Benchmark a tool-heavy booking flow:
```text
Fast SSE
vs
Responses WS + Fast
```
Keep only if real end-to-end booking latency improves enough to justify lifecycle complexity.

## L2 — `T-SP-SPEED-5`: speculative read prefetch
After CalendarAdapter/cancellation are stable.

Allowed:
```text
availability read
CRM/customer lookup
KB lookup
```
Never speculate writes:
```text
booking
cancellation
SMS
CRM mutation
```

## L3 — `T-SP-SCALE-1`: capacity after new TTS architecture
Re-estimate connection limits after one TTS socket/call. Do not optimize the obsolete per-synthesis model.

## L4 — `T-SP-SCALE-2`: multi-worker scaling
Current n=10 is enough for demos. Trigger only on measured CPU saturation or client concurrency requirements; then test n=30/n=50.

## L5 — `T-SP-SCALE-3`: coordinated provider rate limiting
Trigger when real concurrent traffic creates 429 cascades/cooldown storms.

## L6 — missed-call recovery
After Customer/BusinessTask/NextAction/Scheduler/Outbox/SMS, this becomes a small policy flow instead of bespoke architecture.

## L7 — speed-to-lead
Build on:
```text
Customer/lead → BusinessTask → consent → scheduler → voice/SMS → outcome
```
Do not revive old outbound architecture by patching it.

## L8 — voice migration A/B
When replacement becomes necessary:
1. enumerate voices from authoritative ElevenLabs account/API;
2. shortlist verified candidates;
3. identical scripts;
4. blind PSTN A/B;
5. compare naturalness + latency.

Only then tune stability, e.g. research A/B around `0.38 / 0.45 / 0.52`. Current `0.50` is already reasonable.

# 8. ⚫ DEFERRED / REASONED NO

- **OpenAI Realtime as primary stack:** no now; replaces Deepgram/ElevenLabs and current architecture still has cheaper wins.
- **Custom voice-cloning product:** no current requirement.
- **HubSpot:** no until generic CRMAdapter + GHL prove the abstraction.
- **Microsoft Calendar:** no until requested; Google first.
- **Full utterance-ID refactor:** only if same-gen multi-fire returns.
- **Random filler/disfluency engine:** no; existing filler is sufficient for legitimate waits.
- **Kubernetes/distributed platform:** no; first customers matter more than 1,000-call infrastructure.
- **Endless provider tournaments:** no; every benchmark must answer a concrete routing/cost/capability decision.

# 9. OPEN PRODUCT QUESTIONS

1. **Demo hosting:** local/tunnel vs simple US-East VPS vs AWS us-east-1? Decide from P2 measurements.
2. **Vertical:** dental-only or dental-first? Recommendation: **dental-first, vertical-neutral core**.
3. **Groq:** how aggressive? Recommendation: OpenAI Fast correctness baseline; Groq only in proven fast lanes.
4. **Sarah replacement:** when? Recommendation: after session/policy work, but before provider retirement risk becomes urgent.
5. **Persistent OpenAI WS:** default later? Do not decide until earlier session/network work makes marginal benefit measurable.

# 10. BENCH + VERIFICATION PROGRAM

## 10.1 Golden calls
**G1 FAQ:** “What time do you close Friday?”  
Measure cache, direct-answer length, latency, unnecessary acknowledgement.

**G2 booking:** caller provides service/date/time/name/phone.  
Measure exact-time preservation, tool path, booking truth, confirmation.

**G3 correction:** “No, I said two thirty, not three thirty.”  
Measure repair, SemanticPlan authority, no defensive filler.

**G4 frustration:** caller complains previous appointment was moved.  
Measure adaptive delivery and useful empathy without chirpy scripts.

**G5 interruption:** agent begins long answer, caller barges in.  
Measure cancellation, TTS context, Twilio clear, playback ledger, recovery.

**G6 ambiguity:** caller trails off / STT loses phrase.  
Measure clarification and no invented intent.

**G7 emergency/compliance:** measure safety priority and no output-cap truncation.

**G8 multi-intent:** “Book a cleaning tomorrow, and I also want to ask about implants afterward.”  
Measure pending-task preservation.

## 10.2 Per-turn timing events
Record:
```text
caller_last_audio
STT: last_audio_sent / eager_eot / final_eot
LLM: request / first_byte / first_token / first_complete_sentence
LLM: provider / model / input_tokens / cached_tokens / output_tokens
TTS: context_created / text_sent / first_audio / cancelled
Twilio: first_audio_sent / clear_sent / mark_ack
```

Optional:
```text
Deepgram connect
ElevenLabs connect
OpenAI/Groq connection reuse
provider region
```

## 10.3 Keep metric families separate
**Speed:** p50/p95 EOT→useful audio, first complete sentence, total reply.  
**Interaction:** false interruptions, stop latency, repeated acknowledgement, questions/turn, words/tokens/speech act.  
**Correctness:** booking success, exact time/date, false confirmation, tool recovery, dropped multi-intent.  
**Voice:** naturalness, warmth, clarity, pronunciation, seams/choppiness.  
**Business:** task completion, downstream delivery, unresolved reconciliation.

# 11. A/B ORDER

Change one major dimension at a time:
```text
1. flat 200-token cap vs speech-act budgets
2. per-synthesis TTS socket vs call-long multi-context
3. local/tunnel vs US-East
4. Nova-3 vs Flux
5. current provider strategy vs fast-brain lanes
6. stability 0.50 vs ~0.45-range tuning
```

Do not combine voice + Flux + prompt + transport + stability in one experiment.

# 12. EXECUTION ORDER

## Immediate
1. `T-SP-RELIABILITY-3`
2. `T-SP-RELIABILITY-4`
3. `T-SP-SPEED-EXTRA-B2`
4. `T-SP-SPEED-EXTRA-F`
5. `NEW-EL-CALL-SESSION`
6. `NEW-GEO-US-EAST-A-B`

**Stop and inspect measured results.**

## Conversation speed/control
7. `NEW-CONVERSATION-POLICY`
8. `T-SP-SPEED-2`
9. `NEW-INTERRUPTION-E2E`
10. `T-SP-SPEED-4`
11. optional `T-SP-SPEED-EXTRA-E`

## Core intelligence/integration
12. `T-SP3`
13. `T-SP2`

## Durable business system
14. `T-SP4`
15. `T-SP5`
16. `T-SP6`
17. `T-SP7`

## Tenant/integration layer
18. `T-SP8`
19. `T-SP9`
20. `T-SP10`
21. `T-SP11`

## Commercial proof
22. `T-SP12`

Then missed-call recovery, speed-to-lead, and only the next customer-requested CRM/calendar integration.

# 13. DEPENDENCY MAP

```text
RELIABILITY-3/4
├─ SPEED-EXTRA-B2
├─ SPEED-EXTRA-F
└─ NEW-EL-CALL-SESSION
   └─ NEW-INTERRUPTION-E2E

NEW-GEO-US-EAST-A-B ─ independent

NEW-CONVERSATION-POLICY
├─ SPEED-4
└─ optional Predicted Outputs

Flux A/B ─ after baseline

T-SP1 ✅
└─ T-SP3

T-SP4
└─ T-SP5
   └─ T-SP6
      └─ T-SP7

T-SP8
└─ T-SP9

T-SP2 ──────────────┐
T-SP10 ─┐           │
T-SP7 ──┴─ T-SP11 ─┼─ T-SP12
T-SP4/5/6 ─────────┘
```

# 14. DO-NOT-REBUILD
Preserve and extend:
- `packages/runtime/call_actor.py`
- `packages/runtime/turn_manager.py`
- `packages/runtime/playback_ledger.py`
- `packages/dialogue/state.py`
- `packages/dialogue/reducer.py`
- `packages/dialogue/commit.py`
- `packages/dialogue/plan.py`
- `packages/core_agent/plan_realizer.py`
- `packages/core_agent/speech_commit_gate.py`
- `packages/rag/evidence.py`
- `packages/slot_parsers/`
- `packages/integrations/ghl_client.py`
- `packages/integrations/google_calendar.py`
- `packages/channels/whatsapp.py`
- `apps/api/app/providers/`
- `packages/observability/`
- `packages/response_cache/`
- `packages/voice/filler.py`

Wrap/connect/upgrade these. Do not create parallel systems.

# 15. FIRST PRODUCTION MILESTONE

Voice:
```text
p50 EOT→first useful audible speech: 500–700 ms engineering target
p95 explicitly tracked
```

Conversation:
- one useful move per normal turn;
- usually one question;
- no mechanical acknowledgement loops;
- short routine replies;
- clean correction;
- clean interruption.

Correctness:
```text
0 false booking confirmations
0 known date/time drift regressions
0 phone-digit-count loops
0 safety/compliance regressions
```

Business:
```text
real calendar mutation
persistent Customer
persistent BusinessTask
durable follow-up
Outbox retry
reconciliation
SMS/WhatsApp continuation
```

# 16. EVIDENCE MAP

**Speed:** `50_BENCH_llm-ttft-bench-2026-08-20_012206.md`
- OpenAI full 1534 ms
- OpenAI Fast full 772 ms
- Groq small 485 ms

**Real calls:** `51_TRANSCRIPTS/*.md`
- recent p50 ≈1.53–4.35 s
- Oliver call p50 1.53 s + explicit robotic/speed complaint

**Network/session:** `21_SPEED_DEEP-RESEARCH-NETWORK-ARCHITECTURE-2026-08-20.md`
1. US-East A/B
2. ElevenLabs multi-context WS/call
3. zero-transcode verification
4. Flux
5. hard interruption
6. OpenAI Responses WS later

**Humanness:** `33_HUMANNESS_RESPONSE3_deep-research-report.md`
- turn-level NextActionPolicy
- short turns
- one move/question
- speech-act caps
- timing/interruption
- session-long TTS
- voice identity > slider micro-tuning

**Commercial:** market docs repeatedly converge on:
```text
Customer
BusinessTask
Outcome
NextAction
Scheduler
Outbox
Calendar
CRM
SMS/WhatsApp
tenant routing
```

# 17. NON-GOALS FOR THIS ARC
Do not spend the next month on:
- another pile of LLM providers;
- random stock voices;
- voice-cloning product;
- HubSpot;
- Microsoft Calendar;
- Kubernetes;
- advanced SIP;
- broad outbound campaign UI;
- EHR/PMS integrations;
- OpenAI Realtime rewrite;
- acoustic backchannel research;
- local-model infrastructure for its own sake.

# 18. SOURCE-OF-TRUTH POLICY

Statuses:
```text
[ ] TODO
[~] IN PROGRESS
[x] SHIPPED + VERIFIED
[!] SHIPPED, REAL-CALL VERIFICATION PENDING
[-] KILLED / SUPERSEDED
[?] BENCH REQUIRED
```

After each task:
1. update status here;
2. record exact code paths;
3. record CallSid where applicable;
4. record before/after metrics;
5. record regressions/disproved assumptions;
6. change priority when evidence changes.

Voice-loop task completion requires real-call evidence or deterministic end-to-end proof, not merely compiling code.

# 19. IMMEDIATE CLAUDE CODE QUEUE

```text
READ docs/MASTER-PRIORITY-TODO-2026-08-20.md AS THE NEW SOURCE OF TRUTH.

Older priority ordering is superseded where it conflicts.

1. T-SP-RELIABILITY-3
   Fresh 5+ turn call.
   Verify <=1 intended TTS_STREAM_START per speech generation.
   If clean, keep full utterance_id refactor deferred.

2. T-SP-RELIABILITY-4
   FAQ-heavy real call.
   Verify RESPONSE_CACHE HIT and E2E latency.

3. T-SP-SPEED-EXTRA-B2
   Replace flat max_tokens=200 with one speech-act budget policy:
   ACK 20 / CLARIFY 32 / ASK_SLOT 40 / TOOL_PREAMBLE 32 /
   DIRECT_ANSWER 48 / BOOKING_PROPOSAL 64 / FINAL_CONFIRM 80 /
   COMPLEX 120 / EMERGENCY 96.
   Never truncate required safety/booking confirmation text.
   Add behavior tests.

4. T-SP-SPEED-EXTRA-F
   Measure current production prompt.
   Remove duplication/style prose.
   Preserve TIME, PHONE, BOOKING, HALLUCINATION,
   COMPLIANCE/SAFETY, SEMANTIC PLAN.
   Benchmark old/new full prompt with real tools.

5. NEW-EL-CALL-SESSION
   Replace per-synthesis ElevenLabs WS creation with one multi-context
   WS owned by the call.
   One context per assistant speech generation.
   Cancel active context on barge-in.
   Keep Flash v2.5 + ulaw_8000.
   Send complete sentences/prosodic units.
   Add lifecycle/cancellation tests.
   Verify real multi-turn call.

6. NEW-GEO-US-EAST-A-B
   Experiment only.
   Deploy identical app/config to simple US-East Linux.
   Compare scripted calls local/tunnel vs US-East.
   Record p50/p95 + stage timing.

STOP AND REPORT MEASURED RESULTS.

Then:
7. NEW-CONVERSATION-POLICY
8. T-SP-SPEED-2
9. NEW-INTERRUPTION-E2E
10. T-SP-SPEED-4
11. T-SP3
12. T-SP2
13. T-SP4→T-SP7
14. T-SP8→T-SP11
15. T-SP12

DO NOT:
- invent another dialogue state system;
- replace SemanticPlan;
- weaken load-bearing prompt rules;
- create another WhatsApp transport;
- rebuild Google Calendar from scratch;
- implement Twilio clear again;
- build HubSpot;
- build voice cloning;
- switch away from ElevenLabs Flash v2.5;
- rewrite the stack around OpenAI Realtime;
- build Kubernetes pre-emptively.
```

# 20. FINAL FIVE PRIORITIES

1. **Make one phone call one persistent realtime session.**
2. **Choose the next conversational action before asking the LLM to phrase it.**
3. **Make ordinary speech dramatically shorter through speech-act budgets and prompt compaction.**
4. **Build durable `Customer → BusinessTask → Outcome → NextAction → Outbox` state.**
5. **Accept changes only when real-call metrics/end-to-end tests show better speed, correctness, interruption behavior, or business completion.**

This is the shortest path from the current sophisticated demo to a fast, human-feeling, repeatedly sellable voice-agent system.
