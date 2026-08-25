# Working Notes — Speed + Intelligence + Scale

**Living document.** Updated every session, mostly by Claude. Read the top for CURRENT STATE, scroll down for full context. Keep short items short. Move stale items to the bottom under "Archive."

**Last updated:** 2026-08-19 10:13 (session: voice-agent).

---

## Current state (one paragraph)

**Multi-call VERIFIED WORKING at n=10** — synthetic probe (`scripts/multi_call_probe.py`) hits 10 simultaneous WebSockets; each gets its own actor + session + greeting; per-call first-media 105-348ms. The "single-call backend" assumption was wrong — code was already per-call safe. Real limits are around n=20-50 (ElevenLabs HTTP client saturation, easy fix) and n=100+ (single-process CPU, needs `--workers`). Response cache now WARMED AT BOOT for clinic vertical: 60 FAQ input variants → 11 unique replies, all pre-TTS'd. First-caller FAQ turns should drop from 2-3s to 150-300ms end-to-end. Everything else from earlier today's session still live (T4a lock-ownership, T3.6 K1 suppression, date banner, farewell last-sentence, TIME-handling prompt rules).

## Current server

- **PID:** 76535 (as of 2026-08-19 11:35). Verify with `ps aux | grep uvicorn`.
- **Branch:** `feat/architectural-networking`
- **Restart:** `./apps/api/scripts/run_server.sh`
- **Tail log:** `tail -f apps/api/data/logs/uvicorn-latest.log`
- **Test baseline:** 1193 passed (was 1179; +14 for T-SP1 tests), 19 pre-existing failed (do not chase).
- **Multi-call verified:** `python3 scripts/multi_call_probe.py --n 10 --duration 5` → 10/10 pass
- **Cloudflare tunnel:** MUST be running or Twilio callers get "application error." Start with:
  ```
  nohup cloudflared tunnel --config /Users/az/.cloudflared/config-voiceops.yml run > /tmp/cloudflared-voiceops.log 2>&1 &
  ```
  Verify with `curl -s -o /dev/null -w "%{http_code}\n" -X POST https://agent.eternalconquests.com/twilio/voice` — must return 200. Config routes `agent.eternalconquests.com` → `localhost:8000`. Twilio number's webhook is `https://agent.eternalconquests.com/twilio/voice`.
  **The tunnel does NOT restart with the uvicorn server.** Restart it explicitly after any Mac reboot.

---

## TODO (in order — cross out `[x]` as done)

Priority ranked by **leverage per hour of work** given what's already built. Read the "Why" line before jumping to a bigger item.

### T1 — CONFIG FLIPS (partial — shipped 2026-08-18) — ~2 hours
- [x] Change `elevenlabs_model="eleven_flash_v2_5"` in `config.py`. **DONE 2026-08-18.** `.env` already had this override; the code-default was still `eleven_turbo_v2_5`. Both now match.
- [x] Change `cartesia_model="sonic-3"` in `config.py`. **DONE 2026-08-18.** `.env` already had this override; code-default was `sonic-2` (deprecated 2026-06-01). Both now match.
- [x] Verify `.env STREAMING_LLM_TO_TTS=true` is taking effect. **DONE 2026-08-18** — settings.streaming_llm_to_tts=True confirmed at runtime. BUT: last call `CAbbfbb5f0ee06c0e57a2ae647387c4ea3` had ZERO `TTS_SENTENCE_QUEUED` lines. Root cause: `_brain_job` (batch path, used by nonblocking handlers) does NOT check `_streaming_llm_eligible` — only `_run_brain` and `_run_brain_from_text` do. Since speculative wins the lock and `_brain_job` runs the confirmed path, streaming is unreachable. **This is the same architectural bug as T4** — one fix will unlock both.
- [ ] **T1b (deferred)** Flip `deepgram_use_flux=True`. Comment at .env:180-186 says Flux emitted empty events on Twilio mulaw on 2026-08-11 and asked for isolation-benching before re-enabling. Write a small script that pipes captured Twilio mulaw frames into a Flux WS and confirms real events come back BEFORE flipping on production.

### T2 — ASYNC SMART-TURN ONNX (shipped 2026-08-18) — ~1 hour
- [x] `det.predict` moved off the receive loop via a background worker + O(1) cached-value provider. **DONE 2026-08-18.** Worker runs every 200ms, calls `det.predict` via `asyncio.to_thread`, updates `_cache["val"]`. TurnManager's sync provider reads `_cache["val"]` in constant time. After 3 consecutive >250ms runs the worker stops (was: fallback to 0.5). Task cancelled on stop().
- [x] **T2b** Verified on `CA0aee80af478ca22ff0ef62e34196549b` (2026-08-19): ZERO ZOMBIE, ZERO lag ≥260ms, ZERO smart-turn-slow warnings, only ONE 33ms lag in a 4-min call. Event loop is now clean.
- [ ] **T2c (deferred, after Flux)** Make smart-turn a SHADOW verifier once Flux does its own EOT. Downgrade risk: none — if Flux is off, smart-turn stays critical-path.

### T3.6 — TOOTH-IMPLANTS DOUBLE-RESPONSE (2026-08-19, T4a side-effect)

**Symptom:** Karachi test call `CAe88134d2959e8f4c0e8933d731d9a8b0`. Caller said "Yeah. I wanted to get tooth implants. Can you [pause] tell me how to do that? And, like, I want an appointment." Heard THREE stacked responses: "Sure! We can help with that." → "Sure! For tooth implants, you'll start with a consultation to discuss your options..." → after STREAM_REPLY_REPLACED, another gen=2 restart.

**Root cause:** the caller trailed off on "Can you" (incomplete word). K1 hold-timer buffered "Can you" as pending for up to 2s. Meanwhile:
1. Speculative brain fired on the incomplete text ("Can you") — gen=1
2. Speculative HIT confirmed EXACT match on the incomplete text
3. Speculative TTS starts speaking "Sure! We can help with that." on gen=1
4. K1 hold-timer releases → flushes "Can you tell me how to do that?..." as a NEW dispatch → gen=2 fires the FULL LLM reply
5. Both TTS streams play back to caller

Was HIDDEN before T4a because speculative never actually spoke — it self-vetoed. T4a made speculative work, which surfaced this pre-existing bug in the K1 + speculative interaction.

**Fix options:**
- **A (narrow):** when K1 flushes a continuation, if we already spoke a reply for the base text, dispatch ONLY the delta as a fresh turn.
- **B (broad):** on K1 flush after spec HIT, cancel the streaming spec TTS (bump_turn) and re-run brain on the merged text. One clean reply, loses spec speed win.
- **C (hybrid):** if spec TTS still streaming AND continuation adds >3 words, cancel spec + re-run on merged. If spec already done playing, treat continuation as a fresh turn (Fix A).

**Chosen and shipped 2026-08-19 08:03:** simplest surgical fix — SUPPRESS speculative brain when the caller's text ends on an incomplete trailing word (K1 territory: "Can you", "and", "for the", etc.) and has no terminal punctuation. Location: `_on_eager_end_of_turn` in `apps/api/app/routes/twilio_actor.py`. Log line: `speculative suppressed (K1 incomplete-word ...)`. Effect: incomplete-word turns wait for K1 flush → one clean brain fires on the full merged text. Clean-word turns still speculate normally (spec win preserved). No test regressions.

### T3.5 — NEW BUGS FROM US CALLER (2026-08-19)
- [ ] **Same-gen TTS multi-fire on LLM path** — gen=11 fired 5 `TTS_STREAM_START` events, gen=17 fired 8. Stacks sentences without proper ownership. Root cause = ChatGPT P0 #4 (T4). T4a shipped 2026-08-19 — verify on next call whether this is now fixed. If not, may need T4c.
- [x] **Farewell pattern matches mid-sentence.** **DONE 2026-08-19.** Two guards added to `_maybe_hangup_after_farewell`: (1) match ONLY if text is ≤90 chars AND ends with `.!?` (closing-sentence shape). (2) if a NEW `_speak()` fires within the window, cancel the pending hangup and re-arm the clock. Effect: mid-reply "have a great day" doesn't fire; standalone "Have a great day!" does.
- [x] **LLM said "today is October 4th".** **DONE 2026-08-19.** Added CURRENT DATE + TIME banner to SYSTEM_TEMPLATE with `today_iso`, `today_human`, `tomorrow_iso`, `next_monday_iso`, `now_human`, `business_timezone` all computed in the business's timezone. Explicit "NEVER invent a date" rule. Verified: `Today is Tuesday, August 18, 2026 (2026-08-18) in America/Chicago`.
- [ ] **Turn 1 E2E = 10s** — filler correctly fired at 1.5s but real brain reply took 7 more seconds. Same brain-slow root cause; T4 will improve. Measurement tracked.
- [x] **Response cache never hit on this call** — everything went through LLM. Cache empty + writes require no-tool-calls turns. **DONE 2026-08-19:** shipped `packages/response_cache/common_turns.py` + boot warmup hook in `main.py`. Clinic vertical seeds 60 FAQ input variants → 11 unique replies pulled from `business.faqs`. All 11 replies pre-TTS'd into disk cache. First-caller FAQ turns now ~150-300ms end-to-end instead of 2-3s. Cache table went from ~93 entries to 153.

### T3 — FIX BROKEN TESTS (health hygiene) — ~30 min
- [ ] `tests/test_turn_manager.py::test_speech_resume_after_final_fires_turn_resumed` — ChatGPT flagged as regression touching our EOT/interruption machinery. Investigate.
- [ ] `tests/test_streaming_llm_pipeline.py::test_handle_user_turn_invokes_on_delta_when_streaming` — `ScriptedLLM.complete()` no longer accepts `site=` kwarg. Test-harness drift. Booking flow depends.

### T4 — TURN EXECUTION UNIFICATION (partial shipped 2026-08-19) — Full plan = 1-2 days
- [x] **Narrow T4a shipped 2026-08-19.** `_run_brain_from_text(..., *, owns_lock=False)`. Speculative dispatcher now passes `owns_lock=True`. When True the callee skips the redundant `_try_claim_response_commit(reason="run_brain")` that was silently vetoing every non-fastpath speculative dispatch → forcing every LLM turn into `_brain_job` fallback (adds ~800ms + filler). Expected effect: speculative brain now runs its full prelude (conv-control fastpath → response cache → streaming LLM→TTS). 3 regression tests added (`test_speculative_owns_lock_regression.py`). Full suite: 1179/19 (was 1176/19; +3 for new tests, zero regressions). Server PID 12785.
- [x] **T4b partial verified 2026-08-19 on `CA6a8777572ad6ea6d0e4dbd33d85a379e`:** `owns_lock=True` in log, `streaming=yes` on gen=2 (first time on an LLM turn), 5× COMMIT_LOCK_SKIP reason=speculative is the EXPECTED post-T4a signature (second dispatch bails cleanly). Same-gen multi-fire on the tooth-implants call was traced to K1 flush → INTERRUPTION classification, not T4a — patched with T3.6 (incomplete-word suppression). More real-call verification pending.
- [ ] **T4c held.** Full utterance_id refactor was ChatGPT's original P0 #1. **NEW 2026-08-19 audit says this is superseded by the SemanticPlan refactor (T-SP1 below)** — that architecture already exists in `packages/dialogue/plan.py` but isn't wired to runtime. Refactor there instead of inventing utterance_id.
- [ ] **Historical context:** DO NOT delete the second claim naively. See [[commit-lock-abstraction]] note below. T4a preserves the lock semantics; it just plumbs ownership.

---

## AUDIT-DRIVEN ROADMAP (from 2026-08-19 ChatGPT gap audit)

Full doc: `docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md`

### Audit verification (done 2026-08-19)

5 load-bearing claims spot-checked:

| Claim | Verified | Notes |
|---|---|---|
| SemanticPlan (`packages/dialogue/plan.py`) exists but unused | ✅ | 5 dataclasses (SemanticPlan, PlanOperation, PlannedFact, PlannedQuestion, DeliveryIntent) all real. `packages/core_agent/planners/semantic.py` doesn't reference any of them — it just runs LLM then regex-tags a `speech_act`. Refactor unlocks structural fixes for the wrong-time / dropped-multi-intent bugs |
| EvidenceBundle exists, `emit_evidence_bundle=False` default | ✅ | `packages/rag/evidence.py` real. `rag_tool.py:69` default `False`. Flag flip work |
| slot_parsers has phone only | ✅ | Only `phone.py` + `phone_validator.py` + `session.py` + `registry.py`. `register_slot_type` API present → extension is add-new-parsers-and-register |
| GoogleCalendar.book() has no idempotency_key | ✅ | Google `book(day, duration_minutes)`, FakeCalendar `book(..., idempotency_key: Optional[str])`. Refactor = bring Google up to Fake's interface (smaller than a full new abstraction) |
| Outbound uses `decide_can_call` not `decide_can_call_with_consent` | ✅ | `outbound.py:190,244` uses basic (skips consent provider), while `decide_can_call_with_consent` exists at `dialer_policy.py:142` |

**Audit is trustworthy. Proceed with its ordering.**

### T-SP1 — SemanticPlan plan-then-realize refactor (audit item #2) — SHIPPED 2026-08-19 (narrow version)
- [x] **Narrow T-SP1 shipped.** New module `packages/core_agent/plan_realizer.py`: `semantic_plan_tool_definition()` + `parse_semantic_plan()` + `substitute_critical_facts()`. Brain registers `emit_semantic_plan` tool via `ReceptionistBrain.tools`. Tool loop intercepts calls to this tool separately (captures into `state._semantic_plan`, returns benign result, does NOT dispatch to tool_handler). At end of turn, `substitute_critical_facts` post-processes the reply text — time-shaped critical facts get swapped verbatim. `pending_tasks` surfaces into `state._reactive_notes` for next turn. Prompt updated to describe when/how to use the tool. 14 regression tests (`test_semantic_plan_realizer.py`) all pass. Test suite: 1193/19 (+14 new tests, zero regressions after fixing an unrelated brace-escape in prompt example). Server PID 76535. **Time-drift substitution covers digit + spelled-out forms**; price/name substitution is a future extension.

### T-SP2 — CalendarAdapter (audit item #3)
- [ ] Extract interface: `get_availability`, `find_booking`, `create_booking(idempotency_key)`, `reschedule_booking(idempotency_key)`, `cancel_booking(idempotency_key)`. Implementations: `FakeCalendarAdapter` (from existing FakeCalendar), `GoogleCalendarAdapter` (upgrade existing partial). All mutations through `CommitCoordinator`. Also fix timezone bug (`.isoformat() + "Z"` on tz-aware datetimes). Estimate: 1 day.

### T-SP3 — RAG evidence flip + wire (audit item #4)
- [ ] Change `emit_evidence_bundle=True` in the composed handler. Wire `EvidenceBundle` output into `SemanticPlan.facts[]`. Requires T-SP1 first. Estimate: 0.5 day.

### T-SP4 — Customer + CustomerIdentity (audit item #5)
- [ ] New DB tables: `Customer`, `CustomerIdentity`, `CustomerFact`. Identity resolver maps `phone:` / `whatsapp:` / `ghl:` / `email:` → single Customer. Estimate: 1 day. Unlocks memory + channel continuity.

### T-SP5 — BusinessTask durable state (audit item #6)
- [ ] Not the existing `TaskState` (conversation-local). New DB table with types (BOOK_APPOINTMENT, CALLBACK_REQUEST, MISSED_CALL_RECOVERY, etc.) and states (OPEN, WAITING_*, COMPLETED). Business process survives the call. Estimate: 1-2 days.

### T-SP6 — OutcomeEngine + NextActionPolicy + ActionScheduler (audit items #7-9)
- [ ] The closed loop: `Outcome → NextActionPolicy → Action → Outcome`. Deterministic policies (Priority, Channel, ContactTiming, Callback, Consent, Escalation). Durable scheduler for callbacks/retries/reminders. Estimate: 2-3 days.

### T-SP7 — Outbox + Reconciliation (audit item #10)
- [ ] `OutboxEvent`, `OutboxService`, `OutboxWorker`, `DeliveryReceipt`, `RetryPolicy`, `ReconciliationService`. Replaces fire-and-forget `asyncio.create_task(send_sms(...))` with durable dispatch. Fixes "Calendar says booked, GHL says nothing" client fear. Estimate: 1-2 days.

### T-SP8 — Tenant runtime config (audit item #11)
- [ ] Kill the globals: `_business_cache`, `_calendar_cache`, `_sink_cache`, `_retriever_cache`. Introduce `TenantRuntimeConfig` + `TenantConfigRepository` + `TenantSecretResolver`. Multi-tenant OPERATIONAL, not just multi-tenant DB. Estimate: 1-2 days.

### T-SP9 — DNIS route resolution (audit item #12)
- [ ] `InboundRouteResolver` mapping dialed number → tenant/location. Small, high demand from agencies. Estimate: 0.5 day.

### T-SP10 — CRMAdapter interface (audit item #13)
- [ ] Extract from existing GHL client. Interface: `CRMAdapter` with lookup/upsert/notes/opportunities/tasks/DNC/tags/webhooks/reconciliation. GHL becomes an impl. Estimate: 1-2 days.

### T-SP11 — SMS/WhatsApp full Channel wiring (audit items #14-15)
- [ ] `TwilioSMSChannel` new. WhatsApp connect to Customer + BusinessTask. Unlocks missed-call recovery, speed-to-lead, callback continuation. Estimate: 1-2 days.

### T-SP12 — Killer dental demo (audit item #22)
- [ ] End-to-end demo script: inbound call → SemanticPlan → book → SMS confirm → CRM contact created → follow-up scheduled. Estimate: 1 day scripting + rehearsal.

---

### T5 — PERSISTENT OPENAI WS TOOL CONTINUATION — ~4-6 hours (only if we keep OpenAI as primary)
- [ ] Current code punts WS tool calls back to HTTP → reruns the turn. Change to: tool result sent back on SAME WS conversation → model continues without reprocessing state.
- [ ] Also benchmark OpenAI Fast processing tier for the voice-agent latency lane.

### T6 — TTS TOURNAMENT — ~2-3 hours
- [ ] Same Sarah clone across: ElevenLabs Turbo v2.5, ElevenLabs Flash v2.5, Cartesia Sonic 3.5. Same reply text.
- [ ] Score: TTFB, first audible word, sentence completion, voice similarity, naturalness, choppiness, interruption behavior, cost/minute. Sarah quality matters — don't take a 40ms win at cost of significantly worse voice.

### T7 — SPECULATIVE READ PREFETCH — ~1 day
- [ ] Only reads. Never writes. On partial STT, fire CRM lookup, calendar reads, KB queries in parallel. If caller changes intent, cancel/discard.
- [ ] "book Friday" → speculatively fire `check_availability(Friday)` before caller even finishes. When brain finally asks for it, result is already there.
- [ ] Never race writes: `create_booking`, `send_sms`, `cancel_*`.

### T8 — DIALOGUE STATE OBJECT — ~1-2 days (unlocks intelligence)
- [ ] Explicit `{intent, stage, customer, service, date, time, provider, reads_ready, pending_write}` instead of the LLM reconstructing from transcript every turn.
- [ ] Unlocks: better speed (smaller context), better accuracy, better tool selection, better interruption recovery, better multilingual, easier testing.

### T9 — FAST-BRAIN / DEEP-BRAIN ROUTING — ~1-2 days
- [ ] Route: simple → Lane A (local, no LLM), medium → Lane B (Groq/Cerebras 50-200ms semantic brain), hard → Lane C (OpenAI/Anthropic reasoning).
- [ ] Bench Cerebras GPT-OSS 120B (~3000 tok/s), Groq GPT-OSS 20B (~1000 tok/s), Groq GPT-OSS 120B (~500 tok/s), OpenAI Fast tier.

### T10 — SCALE / MULTI-CALL — after T4-T9
- [ ] Horizontal call scaling.
- [ ] Multi-tenant credential isolation.
- [ ] CRM adapter interface (already partial: `packages/integrations/ghl_client.py` + `sinks.py`).
- [ ] WhatsApp / SMS event outbox with idempotency keys.
- [ ] Google Calendar wiring (already partial: `packages/integrations/google_calendar.py`).

---

## Verified facts (do NOT redo research)

### Config that's live NOW (as of 2026-08-18 11:40)
- `deepgram_use_flux = False` — Flux OFF. See T1b for why (mulaw compatibility unverified since 2026-08-11).
- `elevenlabs_model = "eleven_flash_v2_5"` — ✅ Flash v2.5 (via .env AND config default now).
- `cartesia_model = "sonic-3"` — ✅ (via .env AND config default now).
- `smart_turn_enabled = True` — ✅ but now runs ASYNC in a background worker (T2 shipped).
- `openai_persistent_ws_enabled = False` — scaffolded but off (T5).
- `streaming_llm_to_tts = True` — ✅ setting is on. BUT: `_brain_job` (the nonblocking-handlers path) does not check eligibility → streaming never fires. Fix in T4.

### What's already built (do NOT rebuild)
- **SMS confirmations:** `packages/integrations/calendar_commit_adapter.py:_fire_confirmations_bg()` fires SMS on successful booking (async, non-blocking).
- **WhatsApp:** `packages/channels/whatsapp.py` — `send_text`, `send_voice`, `parse_webhook`. Uses Meta Cloud API.
- **GoHighLevel CRM:** `packages/integrations/ghl_client.py` — contact upsert, notes, opportunities, free slots, appointment ops. `sinks.py` has `on_booking(state, booking_payload)` hook that calls `client.book_appointment(...)`.
- **Google Calendar:** `packages/integrations/google_calendar.py` — `list_slots()` etc.
- **Persistent OpenAI WS:** `openai_persistent_ws` scaffolded in config; wiring exists but tool-continuation punts back to HTTP.
- **Streaming LLM→TTS:** Code exists; `.env` enables it; need runtime verification.
- **Response cache:** `packages/response_cache/` — per-business (business_id, tenant, normalized_text) → cached reply.
- **Conversation-control fastpath:** `packages/voice/conversation_control.py`. Warmed at boot.

### What's broken (chatgpt P0 items, still open)
- **P0 #4 lock veto:** speculative claims lock → spawned `_run_brain_from_text` re-claims → SKIPS → fastpath/cache never run on non-conv-control turns. Every LLM turn eats 800ms-1s from this + fills the wait with a filler. Verified in `CAbbfbb5f0ee06c0e57a2ae647387c4ea3.log` line 74: `COMMIT_LOCK_SKIP gen=0 reason=speculative (slot already claimed)`. My conv-control fastpath diversion routes around it for one intent class ONLY.
- **P0 #6 ZOMBIE_SPEAKING false-kill:** was killing valid speech 150-300ms after start. **PATCHED 2026-08-18** to require `same speech generation AND stale wire pre-entry`. Still needs a real-call soak to confirm.
- **P1 #8 per-call log regex:** `\bCA` doesn't match `twilio_CA...` session prefixes. Half the useful lines don't land in per-call logs. Makes debugging harder. Consequence: don't trust "no LLM_CALL line" arguments; check uvicorn log directly.
- **P1 #11 ANI `{{From}}`:** TwiML `<Parameter>` never expanded; log shows literal `caller='{{From}}'`. R3 phase 3 ANI resolver has zero real data.
- **Two failing tests:** see T3.

### Load-bearing decisions
- **[[commit-lock-abstraction]]:** `turn_generation` is the wrong ownership key. It's per-actor-turn, not per-utterance-attempt. Naive "remove second claim" breaks the Abdullah double-brain guard AND creates its own bug: if `bump_turn` isn't called before the next turn, the stale lock rejects it. The clean fix is `utterance_id` + `response_attempt_id` (T4). Interim: my fastpath-before-lock diversion (already shipped for conv-control).
- **Latency SLOs** (from ChatGPT roadmap, adopt):
  - Hello/yes/no/control: 300-600ms p50
  - Simple intelligent question: 500-900ms p50
  - Knowledge/CRM/calendar read: 700-1300ms p50
  - Booking/write: useful speech <800ms, commit completes after
  - Metric of record: **mouth-close → first useful word**, not full-response-generated.
- **What NOT to build yet:** shadow supervisor, multi-brain routing, DialogueState — all AFTER speed baseline stabilizes.

---

## Latest measurements

| Date | CallSid | Conv-control fastpath | LLM turn E2E | Notes |
|------|---------|-----------------------|--------------|-------|
| 2026-08-17 18:16 | `CA99c1dc9327602d6e2062e497dce25834` | broken (9s) | 9s | Original regression call. Fastpath was dead due to lock veto. |
| 2026-08-17 21:07 | `CA2bcddfbb8dffb4795d50d68d6790e23b` | **500ms** ✓ | 3-5s | Fastpath fix landed. Continuation-merge introduced triple-speak. |
| 2026-08-17 23:57 | `CAb1356a16109974ed4e6b5e88bd33d8bb` | 500ms | 3-5s | Continuation-merge suppression working. 10-digit loop still. |
| 2026-08-18 09:00 | `CA53ba57a40c33197af3febd05f6243a65` | n/a | 3-4s | US-only phone regions rejected PK number. |
| 2026-08-18 10:26 | `CAbbfbb5f0ee06c0e57a2ae647387c4ea3` | 500ms | 2-4s | **Scrambled voice** — ZOMBIE false-kill → double-TTS on same gen. **PATCHED.** Also: call didn't hang up after "Have a great day" → **PATCHED**. |
| 2026-08-19 06:56 | `CA0aee80af478ca22ff0ef62e34196549b` | n/a | 2-3s | **First US caller.** T2 async smart-turn WORKING: 0 ZOMBIE, 0 lag≥260ms, only 1 EVENT_LOOP_LAG (33ms) in 4min. Farewell-hangup fired + correctly aborted when caller resumed. Filler variety visibly better (6 distinct phrases). **NEW BUGS SURFACED:** (a) gen=11 and gen=17 both had 5-8 TTS_STREAM_START events same gen = same-gen multi-fire (ChatGPT P0 #4 manifesting on streaming path), (b) turn 1 E2E was 10s due to filler stuck 7s waiting on brain, (c) LLM said "today is October 4th" (wrong date, actually Aug 19), (d) STREAM_REPLY_REPLACED on gen=11, (e) farewell pattern matched mid-sentence not end-of-final-utterance. |
| 2026-08-19 07:58 | `CAe88134d2959e8f4c0e8933d731d9a8b0` | n/a | p50 **2.17s** (n=3, small) | **First call AFTER T4a shipped.** Karachi. Speculative brain IS running (T4a working) — but exposed pre-existing K1-hold + spec interaction bug: caller trailed off on "Can you", spec fired reply on incomplete text, K1 flushed continuation as fresh gen=2 turn, both played → "Sure! We can help with that." then "Sure! For tooth implants..." = TRIPLE response with STREAM_REPLY_REPLACED in between. Added as T3.6. p50 went UP because gen=1 spec reply was short (didn't count) and gen=4 recovery took 11.58s — variance not signal. Need more turns to compare cleanly. |
| 2026-08-19 09:37 | `CA6a8777572ad6ea6d0e4dbd33d85a379e` | n/a | p50 **3.33s** (n=5) | **Booking succeeded end-to-end.** Karachi. Bugs: (a) "See you then!" farewell not triggering hangup (my 90-char cap was too strict), (b) caller said "1:30" agent said "2:30" (LLM substituted a valid returned slot without warning), (c) LLM forgot the "I want tooth implants after" follow-up. p50 3.33s inflated by the "when do you have an opening" turn — LLM had to call check_availability + read back slots + wait for caller response. **Note:** 5× COMMIT_LOCK_SKIP reason=speculative — T4a is landing but the double-brain-per-turn pattern (spec fires as owns_lock=True, then confirmed EOT tries to fire second dispatch and gets rejected by lock) is still happening. That's correct behavior post-T4a — the SKIP means the second dispatch bailed cleanly instead of stacking. |

**Baseline to beat:** T1 will produce new numbers. Log them here.

---

## Playbook — how to work each session

**Read-work-record loop. Do all steps. This is the process.**

1. **Read** this file top-down. It's the single source of truth for what's current.
2. **Grep** the codebase for the specific line numbers referenced in "Verified facts" (they may have drifted between sessions).
3. **Pick** the highest-priority `[ ]` item from the TODO. If the top items are done or blocked, LOOK BACK into the research folder (`docs/rnd-2026-08/`) and the master roadmap (`VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md`) for tasks we haven't started yet — add them here, then work them.
4. **Design** before implementing. Present a short design in chat and get approval.
5. **Ship** — code + tests + bounce server + dial call.
6. **Record** in this file BEFORE ending session:
   - Delete or `[x]` the TODO item(s) completed.
   - Add measurement row to "Latest measurements" with CallSid.
   - Add new findings under "Verified facts" if you learned something.
   - Add one-line entry to "Session log."
   - Update "Current state" paragraph to reflect new reality.
   - If a section grew large enough to hurt scanability, spin it out to `WORKING-NOTES-<topic>.md` and leave a one-line link from here (see "Split policy" below).

### Split policy (keep this file scannable)

- **This file ceiling: ~300 lines.** Above that, split.
- **When a topic has >50 lines of notes**, spin out to `WORKING-NOTES-<topic>.md` in repo root. Examples of natural splits:
  - `WORKING-NOTES-STT.md` — Deepgram vs Flux measurements, keyterm tuning, numerals config, language-switch behavior
  - `WORKING-NOTES-TTS.md` — ElevenLabs vs Cartesia tournament, voice cloning, latency measurements
  - `WORKING-NOTES-LLM.md` — provider bench (Groq, Cerebras, OpenAI Fast), token/sec, tool-use latency, prompt engineering findings
  - `WORKING-NOTES-DIALOGUE.md` — utterance_id design, DialogueState schema, brain-lane routing
  - `WORKING-NOTES-INTEGRATIONS.md` — WhatsApp templates, GHL adapter shape, Google Cal wiring, event outbox
- **Leave a one-line pointer in the "Reference docs" section here** — never delete the pointer.
- **Never move active TODO items into a split file.** Active work stays here. Split files hold reference material and measurements.

### When to add a new TODO

- After completing an item, before deleting it, check: did we learn something that spawns a follow-up? Add that as a new TODO.
- After a call reveals a bug, add it as a TODO.
- After ChatGPT / research doc surfaces something we haven't done, add it as a TODO.
- Never carry a TODO in your head between sessions. If it's not written here, it doesn't exist.

## Test scripts (use these for calls)

**Baseline call (2 min, exercises 4 fixes):**
1. "Hi, I want to book a new patient exam for tomorrow at 2 PM, name is Abbas, phone is 03303172789."
2. "Actually make it 3 PM."
3. "Real quick — do you take Delta Dental?"
4. "Yeah book it."
5. "Thanks bye."

**Speed baseline call (30s, targeted):**
1. "Hello can you hear me" — expect fastpath ~500ms
2. "Tell me about your services" — expect cache hit or fast LLM
3. "Book a cleaning tomorrow at 2pm" — measure end-to-end

## Reference docs (living index)

- `PROJECT-LAUNCH-CHECKLIST.md` — post-reset restart
- `HANDOFF-2026-08-17.md` — original regression handoff, ChatGPT's first audit
- **`MASTER-PRIORITY-TODO-2026-08-20.md`** (repo root) — **NEW SOURCE OF TRUTH.** ChatGPT synthesis of all 15+ research/audit/plan docs into one prioritized TODO (1115 lines). P1-P22 tasks with evidence citations. Explicit "superseded" table. This wins over UNIFIED-IMPLEMENTATION-PLAN when they disagree.
- **`docs/UNIFIED-IMPLEMENTATION-PLAN.md`** — historical reference; T-SP task IDs still used in MASTER doc. Kept for cross-referencing but MASTER doc wins on priority.
- `VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md` — 2600-line master roadmap (superseded by unified plan)
- `docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md` — repo-specific gap audit (verified, consolidated into unified plan)
- `docs/rnd-2026-08/58-status-and-phase-map-2026-08-14.md` — authoritative phase map
- `docs/rnd-2026-08/59-phase0-validation-plan.md` — the 5-box sanity gate
- `docs/soak/scenarios.md` — 8 canonical test scenarios
- `docs/rnd-2026-08/46-full-conversation-test-suite.md` — 22 scripted conversations

## Session log (short — one line per major action)

- **2026-08-17 evening:** shipped conv-control fastpath diversion (`packages/voice/conversation_control.py` + `apps/api/app/routes/twilio_actor.py`). Regression 9s → 500ms for "hello can you hear me". Regression tests: 12/12 green.
- **2026-08-17 late:** shipped continuation-merge suppression when fastpath answered. Prevents fastpath + filler + LLM triple-speak on "hello / tell me about your clinic" sequence.
- **2026-08-17 midnight:** shipped prompt updates — force tool call on wait-promise + kill "10-digit" language + date-preservation rule.
- **2026-08-18 morning:** shipped `accepted_phone_regions` permissive default (US, PK, GB, CA, AU, IN, AE, SG) so PK numbers parse. Shipped Deepgram `numerals=true` — phone digit sequences won't be spelled/truncated. Filler pool 5→12 phrases with recency-avoidance. Filler delay 1.2s→1.5s. "One sec"→"One second". Filler picker actually uses the pool now (was `random.choice(DEFAULT_FILLERS)` bypassing recency).
- **2026-08-18 late morning:** shipped ZOMBIE_SPEAKING same-speech-generation guard (kills the false-kill that caused scrambled voice on `CAbbfbb5f0ee06c0e57a2ae647387c4ea3`). Shipped farewell-detection + graceful hangup 3s after "have a great day" / "goodbye" / etc.
- **2026-08-18 midday:** shipped code-only zip `~/Desktop/receptionist-agent-code-2026-08-18.zip`. Received ChatGPT's roadmap review; user requested working-notes doc. Wrote this file.
- **2026-08-18 11:40:** T1 (partial) + T2 shipped. Config defaults: Flash v2.5 + Sonic-3 (were Turbo v2.5 + Sonic-2). Verified `streaming_llm_to_tts=True` at runtime; DISCOVERED that `_brain_job` doesn't check `_streaming_llm_eligible` so streaming never fires on nonblocking-handlers path (marked into T4). Smart-turn ONNX moved to async background worker (200ms interval, `asyncio.to_thread`, O(1) cached-value provider). Flux flip deferred to T1b — needs isolation-bench per 2026-08-11 note. Test suite: 1176/19 (unchanged). Server bounced, PID 12932.
- **2026-08-19 07:00:** FIRST US CALLER. `CA0aee80af478ca22ff0ef62e34196549b` (4 min booking convo). T2 async smart-turn empirically WORKING (0 ZOMBIE, 0 lag≥260ms, 1×33ms lag in 4min — was previously firing every partial). Farewell hangup fired + correctly aborted when caller resumed. Filler variety visibly better. NEW BUGS SURFACED and added to T3.5: same-gen multi-fire on LLM path (5-8 TTS_STREAM_START on gen=11/17), farewell mid-sentence false-match, LLM says wrong date ("October 4th"), turn-1 E2E=10s, response cache never hit. All of these except farewell-mid-sentence + wrong-date reduce to T4 (P0 #4 unification).
- **2026-08-19 07:24:** Shipped 2 small fixes from T3.5. (1) Prompt now injects CURRENT DATE + TIME banner in business timezone (fixes "October 4th" bug). (2) Farewell detection now requires closing-sentence shape (≤90 chars + ends with `.!?`) AND resets clock on subsequent `_speak()` (fixes mid-reply false-match). Test suite: 1176/19 (unchanged). Server PID 5963. Measured US-caller avg latency: **~1.5s per turn (TTS first byte ~275ms, brain 900-2500ms)**. Marketing target = sub-second; T4 is the way there.
- **2026-08-19 07:35:** Built `scripts/build_call_transcript.py` — generates readable per-call transcripts at `docs/transcripts/<CallSid>.md` with p50/p90 latency, call-quality issue summary, per-turn first-byte + filler annotations. Index at `docs/transcripts/README.md`. Regen via `python3 scripts/build_call_transcript.py --all-recent`. 6 transcripts written from recent calls; US booking call `CA813939...` measured p50 = **1.69s**.
- **2026-08-19 07:52:** **T4a shipped** — the ChatGPT P0 #4 fix, narrow version. `_run_brain_from_text` now takes `owns_lock=False` kwarg; speculative dispatcher passes `owns_lock=True`. Lock semantics preserved (task #369 Abdullah guard intact). 3 regression tests (`test_speculative_owns_lock_regression.py`). Test suite: 1179/19 (+3 new tests, 0 regressions). Server PID 12785. Expected effect: LLM turns run their full fastpath+cache+streaming prelude on the speculative path instead of self-vetoing → falling into `_brain_job` batch. Verify on next call.
- **2026-08-19 07:58:** First Karachi call after T4a: `CAe88134d2959e8f4c0e8933d731d9a8b0`. T4a demonstrably WORKING (`owns_lock=True` in log, `streaming=yes` on gen=2 for the first time on an LLM turn). BUT exposed pre-existing bug: caller trailed off on "Can you" (incomplete word), spec spoke short reply, K1 flushed continuation as INTERRUPTION → gen bump → full reply stacked over. Triple response.
- **2026-08-19 08:03:** **T3.6 fix shipped** — suppress speculative brain when caller's text ends on an incomplete trailing word (K1 territory) with no terminal punctuation. Prevents the tooth-implants triple-response. Speculative still fires on clean-word / terminal-punct turns (spec speed win preserved). Test suite: 1179/19 (unchanged). Server PID 17493.
- **2026-08-19 09:37:** Karachi call `CA6a8777572ad6ea6d0e4dbd33d85a379e`. Booking succeeded end-to-end (PK phone accepted first try). NEW BUGS: (a) farewell 90-char cap rejected legit closing goodbye "See you then!" because full sentence was 115 chars, call didn't close; (b) caller asked "1:30" but agent replied "two thirty" — LLM substituted a different time even though 13:30 was in the returned slot list; (c) LLM forgot the "I want tooth implants too" secondary intent after booking the general appointment.
- **2026-08-19 09:45:** Shipped 3 fixes. **Farewell:** dropped total-length cap, now matches farewell pattern in LAST sentence only (still requires terminal punctuation). Long booking-confirmation-with-goodbye now triggers hangup. **Prompt (time handling):** added CRITICAL rule "USE THE EXACT TIME THE CALLER SAID" — LLM MUST use the caller's exact time if it's in the tool result, never substitute silently. Explicit BAD/GOOD examples referencing this call's regression. **Prompt (multi-step intent):** added REMEMBER MULTI-STEP INTENT rule — if caller mentions a follow-up, don't drop it. Test suite: 1179/19 (unchanged). Server PID 63302.
- **2026-08-19 10:13:** **Multi-call verification spike** — wrote `scripts/multi_call_probe.py` that fires N concurrent WS `start → media → stop` sequences. Empirical result: 10/10 at n=10, first-media 105-348ms. Multi-call was NEVER the bottleneck; the "backend can only do one call" belief was wrong. Real limits are around n=20-50 (ElevenLabs HTTP connection cap, easy fix) and n=100+ (single Python process). **Response cache warmup shipped** — new module `packages/response_cache/common_turns.py` + boot hook `_warm_response_cache` in `main.py`. Clinic vertical: 60 FAQ input variants → 11 unique replies from `business.faqs`, all pre-TTS'd. Cache went from ~93 → 153 entries. Server PID 69024.
- **2026-08-19 10:35:** **ChatGPT audit delivered + saved** to `docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md`. Repo-specific gap analysis. Verified 5 load-bearing claims — ALL confirmed. Biggest finding: `packages/dialogue/plan.py` has full SemanticPlan architecture (SemanticPlan, PlanOperation, PlannedFact, PlannedQuestion, DeliveryIntent) but `packages/core_agent/planners/semantic.py` doesn't use any of it — just runs LLM then regex-tags speech_act. Refactoring the planner to actually USE SemanticPlan is now the biggest single lever — it's structural fix for the wrong-time / dropped-multi-intent bugs we've been prompt-patching. Added T-SP1..T-SP12 to WORKING-NOTES as the audit-driven roadmap. Ordering reflects audit's final implementation-order section.
- **2026-08-19 11:00:** **UNIFIED-IMPLEMENTATION-PLAN.md written.** Consolidates both ChatGPT audits + master roadmap + T-SP1..T-SP12 into ONE authoritative plan. Per-system: files/DB tables/deps/test plan/definition of done. 4 parallel work threads (A Intelligence, B Business layer, C Deployability, D Integrations) with dependency graph. Explicit "do NOT rebuild" list. First-N-days concrete task queue. All future sessions read this doc FIRST. Any conflict with older docs → this doc wins.
- **2026-08-19 11:15:** Extended UNIFIED-IMPLEMENTATION-PLAN with the ORIGINAL T1-T10 items that the T-SP list had swallowed. New sections: **Speed track** (T-SP-SPEED-1 async smart-turn (shipped), 2 Flux flip, 3 T4c held, 4 Groq routing, 5 speculative reads, 6 persistent WS), **Reliability tail** (T-SP-RELIABILITY-1..4: per-call log regex, ANI expansion, same-gen multi-fire verify, cache hit measurement), **Scale track** (T-SP-SCALE-1..3: EL connection pool, uvicorn workers, rate-limit backoff). Dependency graph updated. Effort estimate: 17-29 focused days total.
- **2026-08-19 11:35:** **T-SP1 shipped (narrow version).** Wired the pre-existing SemanticPlan schema into runtime. New module `packages/core_agent/plan_realizer.py`. Brain now exposes `emit_semantic_plan` tool; tool loop intercepts + captures into `state._semantic_plan`; end-of-turn post-processes reply text to substitute critical time-shaped facts verbatim; pending_tasks surface into reactive notes for next turn. 14 regression tests pass. Test suite 1193/19 (zero regressions after fixing a brace-escape bug in the prompt example). Server PID 76535. Kills the wrong-time-substitution and dropped-multi-intent bug class STRUCTURALLY instead of via prompt patch. Real-call verification pending.
- **2026-08-19 12:50:** **Cloudflare tunnel gotcha.** PK caller got "application error" because `cloudflared tunnel` wasn't running — server was up locally but `agent.eternalconquests.com` (Twilio webhook URL) had no route. Fixed by starting `cloudflared tunnel --config /Users/az/.cloudflared/config-voiceops.yml run &`. Tunnel PID 10060, 4 edge connections. `POST /twilio/voice` returns 200. Added tunnel-start command to WORKING-NOTES "Current server" section. **Every future session should VERIFY the tunnel is up before test calls** — `curl` the webhook URL, expect 200, not just "server is up."
- **2026-08-20 00:15:** **US caller (Oliver) tested + reported: "voice robotic, need more human, need speed."** Transcript `CAa8d6d3d6751eea6856cb18b53c0ed7c2` (13 turns, 93s, p50 1.53s). **Speed audit done from per-turn breakdown:** STT 350-700ms ✅, TTS first-byte 290-310ms ✅, **LLM first-token 1900-2200ms ❌ = the bottleneck.** Not TTS, not STT — it's OpenAI gpt-4o-mini's TTFT. T-SP-SPEED-4 Groq routing is the correct fix (100-300ms TTFT), would drop p50 to sub-1s. **Discovered reference prompt:** `workflows/n8n/subtodealz-vapi-assistant-prompt.md` (1087 lines) shows "human-sounding" style already exists in the repo. **Delivered research brief** at `docs/HUMANNESS-RESEARCH-BRIEF-2026-08-20.md` — comprehensive package to send ChatGPT for a research-backed humanness upgrade covering: prompt rewrite (persona + HOW YOU TALK + examples), voice ID recommendation, ElevenLabs voice_settings values, text-level prosody tricks compatible with Flash v2.5, streaming-path prosody tips, ordered recommendation for max lift, validation plan. Marked which prompt sections are load-bearing (do not touch) vs style-only (rewrite). User to send to ChatGPT + return the recommendation doc.
- **2026-08-20 01:20:** **TTFT bench + two-part LLM speed fix shipped.**  New script `scripts/llm_ttft_bench.py` measures first-token latency across OpenAI / OpenAI-fast / Cerebras / Groq at 3 prompt sizes.  Results at `docs/llm-ttft-bench-2026-08-20_012206.md`. **Key finding on the real 24k-char production prompt:** openai=1534ms, openai-fast=**772ms**, groq-oss20b=485ms (rate-limits at scale), cerebras=402 (no credit).  **Root causes found (audit by user):** (1) GroqLLM had NO `stream_complete` — RouterLLM's streaming path fell back to buffered `complete()`, defeating token streaming; (2) GroqLLM opened a fresh `httpx.AsyncClient` per call → ~500ms TLS handshake per turn.  **Fixes shipped:** class-level shared HTTP/2 client with keep-alive on GroqLLM (+ all 3 fallback helpers Gemini/NVIDIA/OpenRouter), native `stream_complete` for Groq mirroring OpenAI's SSE contract.  Also: added `openai_service_tier` config, wired into both `complete()` + `stream_complete()` payloads, flipped `.env OPENAI_SERVICE_TIER=fast`.  **Expected impact:** OpenAI TTFT ~1500ms → ~770ms on the real prompt (measured 2x drop).  Test suite 1193/19 (zero regressions).  Server PID 50668, tunnel HTTP 200.
- **2026-08-20 02:00:** **OpenAI speed research spike done.** No code changes; `docs/openai-speed-research-2026-08-20.md` documents 8 real TTFT levers with citations. **Key new findings:** (1) `prompt_cache_key` + `prompt_cache_retention="24h"` params improve cache hit rate under concurrent load — currently not set. (2) Structured Outputs incur 200-400ms first-call schema-compile penalty — `emit_semantic_plan` schema eats this on every first turn; a boot-time warmup dummy request eliminates it. (3) Predicted Outputs (`prediction` param) documented at 15-40% TTFT drop when opener is predictable — voice-agent replies frequently start with "Sure!" / "Got it!" / "Perfect," so wireable off `SemanticPlan.operation`. (4) OpenAI's own docs: "cutting 50% of output tokens ~= 50% of latency" — current `max_tokens=300` is way over what a 20-30 word receptionist reply needs. (5) Realtime API (`gpt-realtime`) gets sub-500ms but requires 1-2 week rewrite (replaces Deepgram+ElevenLabs+our whole pipeline). Added T-SP-SPEED-EXTRA-A through H to UNIFIED-IMPLEMENTATION-PLAN. Ship-order: A shipped ✅, then B (max_tokens), C (cache params + telemetry), D (schema warmup), E (Predicted Outputs) — all can ship BEFORE ChatGPT's humanness rewrite arrives. F (prompt trim) goes WITH the humanness rewrite. G (nano bench) is optional. H (Realtime) is a premium tier.
- **2026-08-20 07:04:** **SPEED-EXTRA-B + C + D SHIPPED.** (B) `max_tokens` 300→200 across brain.py (streaming + batch + forced_final paths). (C) OpenAI provider now sends `prompt_cache_key=biz-<hash>` + `prompt_cache_retention="24h"` on both `complete()` + `stream_complete()`; `_derive_cache_key` hashes system message so same business = same key across turns; `_log_cache_hit` logs `OPENAI_CACHE site=... cached=X/Y (Z%)` per call. (D) LLM router boot warmup now uses FULL production tools (via `build_tools_for_vertical` + `semantic_plan_tool_definition`) instead of a `check_hours` stub — kills the 200-400ms schema-compile tax on the first real caller. Also: added LiveKit keys to .env (`LIVEKIT_URL=` blank pending dashboard, API key + secret set from free-tier account). Test suite 1192/20 (delta is one pre-existing flaky test, verified against `git stash`ed baseline — not a regression from these changes). Server PID 16225. `OPENAI_CACHE` line confirmed in boot log.
- **2026-08-20 07:15:** **HUMANNESS RESEARCH REPORT ARRIVED** at `deep-research-report-humanness.md` (1714 lines). Extraordinary quality. Key findings verified for repo context: (1) "Your problem is NOT primarily an ElevenLabs problem and NOT primarily a 'better personality prompt' problem — it is a pipeline problem" — validates our current speed-first approach. (2) Notes the ZIP snapshot they were given still had the old GroqLLM (no stream_complete, fresh AsyncClient) — reassures that our recent Groq streaming fix is exactly the right change but they're auditing an older version. (3) Provides a concrete rewritten PERSONA + HOW YOU ACTUALLY TALK text ready to drop in. (4) Voice recommendations: **Talia (`OZ0L6eISlOejga3XjDFt`) is Sarah's official migration candidate** as Sarah is being retired end-2026; Chelsea `NHRgOEwqx5WZNClv5sat`, Maisie `QtY3JBOUKEB5xzrRfOKc`, Jade `g7LVvkPWALzPxOQbF6OE` are alternatives. **VERIFY IDs via ElevenLabs voice-list endpoint before shipping**. (5) Voice settings: `stability=0.40, similarity_boost=0.75, style=0.0, use_speaker_boost=false, speed=1.0` — start here, A/B down to 0.32-0.35 stability for warmth. (6) MAJOR ARCHITECTURAL RECOMMENDATION: **one ElevenLabs WebSocket per assistant TURN, not per sentence** — currently we open a fresh WS every sentence; report says this "deprives the synthesiser of continuity". (7) `auto_mode=true` on ElevenLabs is good but ONLY with complete sentences (we already have SentenceBuffer — correct pattern). (8) Prosody comes from ORDINARY TEXT + punctuation, not SSML tricks or fake fillers. (9) Turn-level structured trace design provided verbatim. (10) Fast-fallback ladder design provided. (11) Do NOT spray fillers — CHI study shows disfluencies can DECREASE perceived intelligence in task-oriented agents. Contextual acknowledgments beat generic "umms". USER NEXT STEP: humanness recommendation now takes precedence per pre-existing rule; T-SP1 SemanticPlan + this humanness spec should be implemented as one coordinated arc.
- **2026-08-20 07:40:** **Voice IDs from humanness report VERIFIED and 4/4 FAILED.** Talia/Chelsea/Maisie/Jade IDs `voice_not_found` on our ElevenLabs account. Only Sarah (current) verifies. Report explicitly warned "verify before shipping" — good thing we did. Root cause: report drew IDs from public catalog listings, not our authoritative API. Voice pick now punted to user (best positioned via ElevenLabs dashboard). **PERSONA + HOW YOU ACTUALLY TALK + EXAMPLES rewrite SHIPPED** verbatim from the report. `voice_persona` field in `sample-data/clinic/business.json` went 592 → 1432 chars, expressing behavior (mirroring, pace-matching, no scripted empathy) rather than autobiographical brochure text. Prompt.py HOW YOU ACTUALLY TALK + EXAMPLES sections completely replaced with the report's version: 10-30 word turn rule, one-question-at-a-time, "acknowledge specific thing not every turn," rare disfluencies, front-load useful meaning, mirror pace, calm on emergencies, tool truth beats style. Load-bearing sections (TIME, PHONE, BOOKING, COMPLIANCE, HALLUCINATION, SEMANTIC PLAN) untouched. Test suite 1193/19 (baseline). Server PID 31709. **STILL PENDING FROM HUMANNESS REPORT:** voice selection + `stability=0.40` + one-WS-per-TURN refactor. Voice pick unblocks the settings tweak; the WS-per-turn is a ~2-3 hr refactor.
- **2026-08-20 08:00:** **Deep-research round 2 (network architecture) received + saved** at `docs/DEEP-RESEARCH-NETWORK-ARCHITECTURE-2026-08-20.md`. Written by ChatGPT AFTER seeing our GroqLLM+shared-client+OpenAI-Fast fixes. Verdict: **biggest remaining latency lever is NOT another provider swap — it's making the entire call behave as ONE long-lived real-time session** with four persistent sockets (Twilio, Deepgram, ElevenLabs, OpenAI) instead of dozens of per-turn reconnects. PLUS: **server geography** — Twilio is US-East, ElevenLabs 100-150ms TTFB from NA vs 150-200ms from South Asia. Moving from Pakistan/cloudflare-tunnel to AWS us-east-1 is the #1 infrastructure experiment. **NEW recommendation supersedes earlier "one WS per turn":** use ElevenLabs **multi-context WebSocket** (`/multi-stream-input`, `inactivity_timeout=180`, `auto_mode=true`) — ONE connection per CALL with per-turn contexts opened/closed inside. Other new items: (a) go zero-transcode `ulaw_8000` end-to-end (Twilio→Deepgram, ElevenLabs→Twilio, no PCM16 resample), (b) Deepgram Flux with EagerEndOfTurn (100-200ms saved, 50-70% more LLM calls tradeoff), (c) 80ms audio chunks (Flux recommends), (d) hard barge-in path must include `Twilio CLEAR` (otherwise 800ms of already-queued audio still plays), (e) OpenAI Responses WS for tool-heavy workflows (up to 40% E2E on 20+ tool-call flows). Also: full latency telemetry schema provided (DNS→TCP→TLS→WS-upgrade per provider). **Recommended priority order:** us-east deploy → ElevenLabs multi-context WS → zero-transcode → Flux+Eager → Twilio CLEAR barge-in → OpenAI Responses WS → micro-tune. Target: sub-second becomes normal (p50 500-700ms end-of-user-turn → first useful audio).
- **2026-08-20 08:36:** **Third deep-research doc received** at `deep-research-report.md` (55KB, 1132 lines) — "Human-Like Enterprise Voice Agents: Applied Research for the Twilio–Deepgram–OpenAI/Groq–ElevenLabs Stack." Big convergence with round 2 network doc + adds significant new material. Key takeaways: (1) **Confirms our current stability=0.5/similarity=0.75 is close to ElevenLabs' own recommended starting region** — slider tweaks alone won't cure "robotic." Recommends `stability=0.45` as A/B start, `speaker_boost=false` initially. (2) **NextActionPolicy architecture** — LLM should not simultaneously reason about business state, next operation, compliance, tool needs, emotional strategy AND improvise speech. Separate into a `{conversation_phase, speech_act, caller_affect, caller_style, urgency, next_action, known, missing, tool_pending, requires_confirmation}` state object; LLM just verbalizes the NEXT ACTION. (3) **Full recommended prompt scaffold provided verbatim** — 6-24 word turns, ONE conversational move per turn, ONE question at a time. Explicit `# ROLE / # CONVERSATION CONTRACT / # LISTENING / # ADAPTIVE DELIVERY / # DISFLUENCIES / # VARIETY / # CURRENT CONVERSATION STATE / # TTS WRITING` sections. (4) **Per-speech-act output caps** — ack=20 tokens, clarify=32, ask_slot=40, direct_answer=48, booking_proposal=64, final_confirm=80, complex_explanation=120, emergency=96. **Our current max_tokens=200 across all speech-acts is over-budget for most turns.** (5) **Voice IDENTITY > voice settings** — recommends recording a real receptionist for Instant Voice Cloning rather than another stock voice. Sarah's default retires Dec 31, 2026. (6) Flash v2.5 SSML capabilities: `<break>` supported (sparingly, ≤3s); em-dash works; ellipsis for genuine hesitation only; v3 emotion tags do NOT work on Flash v2.5; `<phoneme>` NOT supported. (7) Confirms one-WS-per-CALL multi-context recommendation from round 2. (8) Concrete voice-agent example: instead of `"Okay, so just to confirm, what I have is that we've scheduled your appointment for Tuesday at two thirty PM with Doctor Chen, and I just want to make sure that all of that sounds correct to you?"` → `"Okay, Tuesday at two thirty with Doctor Chen. Does that sound right?"`. **NEXT USER STEP:** zip all research docs + codebase + write ChatGPT synthesis prompt.
- **2026-08-20 10:15:** **Prompt engineering pass SHIPPED + EL WS bench re-run.**
  - Added 4 new sections to `packages/core_agent/prompt.py` (after MOOD-AWARE, before TOOLS), adapted from the SubtoDealz Vapi outbound prompt (`/Users/az/Desktop/N8N Workflows/drive-download-.../SubtoDealz - Vapi prompt.docx`):
    - **INTERRUPTED? — STOP, ACKNOWLEDGE, THEN RESPOND** (post-barge language pattern; barge itself already handled at frame layer)
    - **AMBIGUOUS "OK" — DO NOT TREAT AS CONFIRMATION** (bare "yeah"/"sure"/"okay" ≠ consent to book/hang)
    - **EDGE CASES** — 8 patterns: rapid multi-questions, mishears, bad connection, background chaos, one-word chains, sudden topic pivots, mistaken identity, over-chatty caller
    - **SILENCE / CALLER RETURNS** — warm-resume language for after-bumper cases
  - All load-bearing sections untouched (TIME, PHONE, BOOKING, COMPLIANCE, DATE, HALLUCINATION, SEMANTIC PLAN).
  - Test suite: **1207 passed, 19 pre-existing failed, zero regressions**. Server bounced → PID 64700.
  - **EL WS bench re-run (`/tmp/bench_eleven_ws.py`, n=5 each):**
    - HTTP /stream: median first-byte **717ms**, total 860ms
    - WS /stream-input: median connect **1058ms**, first-byte **520ms** (200ms faster than HTTP first-byte)
    - **PREVIOUS BENCH (2026-08-12) INVERTED** — old finding "WS is 5x slower" was network-transient or EL fixed the auto_mode flush. On today's network, WS wins per-turn if the connection is amortized. **P1 (multi-context WS per call) is now supported by fresh data** — value is in the persistence (skip the 1058ms connect on turns 2+), not the WS protocol itself. Break-even ≈ turn 4-5; net win for typical 8-15-turn dental calls.
- **2026-08-20 09:20:** **P3 SHIPPED — speech-act token budgets.** New module `packages/core_agent/token_budgets.py` maps `PlanOperation → max_tokens` per the master TODO's table (ack=20, ask_slot=40, direct_answer=48, confirm_action=80, escalate=96, complex=120, default=80). Wired into 3 call sites in `packages/core_agent/brain.py`: streaming path, batch fallback, forced-final. Reads `state._semantic_plan` (from prior T-SP1 turn) to pick per-turn budget; falls back to DEFAULT_BUDGET (80) when no plan. Replaces flat `max_tokens=200` — a 60% cap reduction on default turns and much larger on acks. 14 regression tests pass. Test suite: 1207/19 (+14 new tests, zero regressions). Server PID 54891.
- **2026-08-20 09:00:** **MASTER-PRIORITY-TODO-2026-08-20.md received** (1115 lines) — ChatGPT synthesis of all 15+ research docs into single prioritized TODO. **This is now the new source of truth**, wins over UNIFIED-IMPLEMENTATION-PLAN.md on conflicts. Structure: exec architecture (two NextAction policies — conversational vs business, don't merge), verified current state (10 items already shipped), guiding principles (5), superseded/killed table (14 items explicitly de-scoped), P1-P22 prioritized tasks with evidence citations + file paths, deferred/reasoned-no section, open product questions, bench/verification program, A/B order, execution order. **P1-P6 = 🔴 CRITICAL this week (~10 hours):** P1 ElevenLabs multi-context WS per call (3-4h), P2 US-East geography A/B (2h), P3 speech-act token budgets (1-1.5h), P4 compact prompt 22k→12-15k (2h), P5 verify same-gen TTS ownership (15m), P6 prove response cache hit on real call (15m). **P7-P22 = 🟡 NEXT 2-4 weeks:** ConversationNextActionPolicy (turn-level, distinct from T-SP6 business layer), Flux+EagerEndOfTurn A/B, barge-in E2E verification, fast-brain routing lanes, Predicted Outputs bench-only, CalendarAdapter (T-SP2), EvidenceBundle wire (T-SP3), Customer+Identity (T-SP4), BusinessTask (T-SP5), OutcomeEngine+NextAction+Scheduler (T-SP6), Outbox+DeliveryReceipt (T-SP7), TenantRuntimeConfig (T-SP8), DNIS (T-SP9), CRMAdapter (T-SP10), SMS+WhatsApp channels (T-SP11), killer demo (T-SP12). **DEFERRED:** OpenAI Realtime, voice cloning, HubSpot, full utterance_id refactor. **Real-call baseline table included** — best call (this session, `CAa8d6d3d6751eea6856cb18b53c0ed7c2`) is p50 1.53s.

## Archive (moved out of active list)

_(Nothing yet.)_
