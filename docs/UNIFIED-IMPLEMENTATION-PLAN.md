# Unified Implementation Plan

**Consolidates:** all ChatGPT audits + master roadmap + WORKING-NOTES.md T-SP1..T-SP12 into ONE authoritative plan. Any conflict between older docs and this one — this doc wins.

**Read this first every session.** WORKING-NOTES.md tracks per-session progress; this doc tracks THE PLAN itself.

**Last unified:** 2026-08-19.

**Sources this consolidates:**
- `docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md` (repo-specific gap audit — verified)
- `VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md` (2600-line market roadmap)
- ChatGPT follow-up message 2026-08-19 ("systems architecture blueprint" — text only, no attached file)
- ChatGPT earlier turn-unification audit ("cache-only patch is not the P0 architectural fix")
- All shipped fixes documented in `WORKING-NOTES.md` session log

---

## Guiding principles (from ChatGPT audits, ratified 2026-08-19)

1. **The call-runtime kernel is DONE.** CallActor, DialogueState, TurnManager, CommitCoordinator, SpeechCommitGate, STT/TTS abstraction, structured-input framework — all exist and are strong. **DO NOT REBUILD ANY OF THIS.**
2. **The business-operating layer is MISSING.** Customer, BusinessTask, OutcomeEngine, NextActionPolicy, ActionScheduler, Outbox. This is what turns "voice agent demo" into "sellable business system."
3. **Intelligence bugs are structural, not prompt-shaped.** The wrong-time-substitution and dropped-multi-intent bugs I've been prompt-patching this week are consequences of SemanticPlan existing in `packages/dialogue/plan.py` but not being wired to runtime. Fixing the wire is a structural fix; prompt patches are symptomatic.
4. **Extend the seams that exist.** Slot parsers already have a `register_slot_type` API — add EmailParser/DateParser as registrations, don't build a new Structured Input framework. GHL client already implements the operations — wrap it as `CRMAdapter` implementation, don't rewrite.

---

## The closed loop

This is the target system architecture. Each arrow is a code seam.

```
                           Customer  ←────────────────┐
                              │                        │
                              ▼                        │
                        BusinessTask                   │
                              │                        │
                              ▼                        │
                     NextActionPolicy                  │
                              │                        │
                ┌─────────────┼───────────────┐        │
                ▼             ▼               ▼        │
              Voice          SMS          WhatsApp     │
                └─────────────┼───────────────┘        │
                              ▼                        │
                          CallActor                    │
                          (existing)                   │
                              │                        │
                              ▼                        │
                       DialogueState                   │
                          (existing)                   │
                              │                        │
                              ▼                        │
                        SemanticPlan                   │
                        (wire-up needed)               │
                              │                        │
                              ▼                        │
                     CommitCoordinator                 │
                          (existing)                   │
                              │                        │
                              ▼                        │
                   Authoritative Tools                 │
                              │                        │
                              ▼                        │
                       CommitResult                    │
                              │                        │
                              ▼                        │
                     OutcomeEngine                     │
                              │                        │
                              ▼                        │
                     NextActionPolicy ─────────────────┘
                     (closes the loop)
```

Existing pieces (call-runtime kernel) are in a black box everything above just USES.
New pieces (business layer) are the outer ring.

---

## System inventory (12 systems, ordered by dependency)

Each system lists:
- **Exists?** what's already in the repo
- **What to build**
- **Files touched** — concrete paths
- **DB tables** — new tables (if any)
- **Depends on** — which earlier systems must ship first
- **Test plan** — unit + integration + real-call verification
- **Definition of done** — how we know it's ready to demo

### T-SP1 — Wire SemanticPlan into runtime

**Exists?** YES — `packages/dialogue/plan.py` has `SemanticPlan`, `PlanOperation`, `PlannedFact`, `PlannedQuestion`, `DeliveryIntent` fully specified. `packages/core_agent/planners/semantic.py` currently uses ZERO of it — just runs LLM then regex-tags speech_act.

**What to build:**
- Register `emit_semantic_plan` as a tool in `ReceptionistBrain.tools` (schema mirrors SemanticPlan).
- Update prompt to require LLM to call the tool with structured facts + questions + pending_tasks whenever the turn involves specific values.
- In `SemanticPlanner.plan(...)`: consume the plan; if the LLM's reply text contradicts a `PlannedFact(critical=True)`, substitute the plan's value into the reply (regex swap on the critical value). Log the substitution as `SEMANTIC_PLAN_SUBSTITUTION`.
- Surface `SemanticPlan.pending_tasks` into `CallState._reactive_notes` so next-turn prompt includes them.
- Fallback: if LLM doesn't emit the tool call, current behavior (no substitution, no pending_tasks tracking) — no regression.

**Files touched:** `packages/core_agent/brain.py` (add tool to tools list), `packages/core_agent/planners/semantic.py` (consume plan), `packages/core_agent/prompt.py` (add tool-use instruction), possibly a new small helper `packages/core_agent/plan_realizer.py`.

**DB tables:** none.

**Depends on:** T4a (shipped). Nothing else.

**Test plan:**
- Unit: `SemanticPlanner.plan(...)` with a mock LLM that returns a plan containing `PlannedFact(claim="1:30", critical=True)` and a reply string with "2:30" — assert the reply is post-processed to "1:30".
- Unit: mock LLM returns plan with `pending_tasks=["implant_consult"]` — assert `state._reactive_notes` contains the note.
- Integration: dial → say "book for 1:30 tomorrow" → verify log has `SEMANTIC_PLAN_SUBSTITUTION` if LLM drifts, or a match if it doesn't.
- Real call: same as integration.

**Definition of done:** on a call that says "book for 1:30 and I also want implants after", the agent (a) books 1:30 not 2:30 even if LLM tries to substitute, (b) mentions the implants follow-up after the general booking. Logs prove why.

**Estimate:** 4-8 hours (small version).

---

### T-SP2 — CalendarAdapter with idempotency + timezone

**Exists?** PARTIAL. `packages/integrations/fake_calendar.py` has `book(..., idempotency_key)` + `list_slots` + `reschedule`. `packages/integrations/google_calendar.py` has ONLY `is_available`, `list_slots`, `book` (no idempotency, no reschedule, no timezone-safe formatting).

**What to build:**
- Interface: `CalendarAdapter` protocol with `get_availability`, `find_booking`, `create_booking(idempotency_key)`, `reschedule_booking(idempotency_key)`, `cancel_booking(idempotency_key)`.
- Implementations: `FakeCalendarAdapter` (wraps existing), `GoogleCalendarAdapter` (upgrades existing).
- Timezone fix: `.isoformat() + "Z"` → proper `pytz.timezone(business.timezone).localize(...).isoformat()`. `list_slots` derives open/close from `business.hours` for the requested day, not hardcoded 9-5.
- Wire ALL mutations through `CommitCoordinator` (`packages/dialogue/commit.py`) — idempotency + double-booking prevention already exists there.

**Files touched:** `packages/integrations/calendar_adapter.py` (new interface), `packages/integrations/google_calendar.py` (upgrade), `packages/integrations/calendar_commit_adapter.py` (use adapter).

**DB tables:** none — CommitCoordinator's idempotency table already handles.

**Depends on:** none (independent of T-SP1).

**Test plan:**
- Unit: `GoogleCalendarAdapter.create_booking` with same idempotency_key twice → second call returns first booking, doesn't create duplicate.
- Unit: `list_slots` with a business having `friday: "07:30-15:00"` → returns 07:30-15:00 slots, not 09:00-17:00.
- Unit: timezone — book "2:30 PM" in America/New_York on a UTC server → Google Calendar API receives correct offset.
- Integration: cassette-recorded Google Calendar API test (use `vcrpy` or similar) — full book/reschedule/cancel loop.
- Real call: switch calendar_backend to google, book an appointment, verify it appears in a real Google Calendar with correct time.

**Definition of done:** live demo — dial in, book, prospect sees the booking appear in THEIR Google Calendar with correct timezone.

**Estimate:** 1 day.

---

### T-SP3 — Turn EvidenceBundle on + wire into SemanticPlan

**Exists?** YES — `packages/rag/evidence.py` has `EvidenceBundle`, `EvidenceClaim`, `Answerability`. Default `emit_evidence_bundle=False` in `packages/integrations/rag_tool.py:69`.

**What to build:**
- Change default to `True`.
- When `lookup_answer` returns an EvidenceBundle, transform its claims into `PlannedFact(source="rag:<chunk_id>")` entries in the SemanticPlan.
- Realizer's substitution logic (from T-SP1) treats RAG-sourced facts the same as caller-sourced facts (critical, no paraphrase for verbatim numbers/prices/dates).

**Files touched:** `packages/integrations/rag_tool.py`, `packages/core_agent/planners/semantic.py`.

**DB tables:** none.

**Depends on:** T-SP1.

**Test plan:**
- Unit: RAG returns `EvidenceBundle(claims=[EvidenceClaim(text="$185", source="chunk_42")])` → SemanticPlan has `PlannedFact(claim="$185", source="rag:chunk_42", critical=True)`.
- Integration: FAQ question "how much is a cleaning?" → agent quotes the number verbatim from the knowledge base, not a paraphrased approximation.
- Real call: same.

**Definition of done:** any factual FAQ answer that came from RAG is auditable back to the source chunk, and price/number claims are verbatim.

**Estimate:** 4-6 hours.

---

### T-SP4 — Customer + CustomerIdentity DB layer

**Exists?** NO — DB currently has `Tenant`, `ApiKey`, `IdempotencyRow`, `SessionRow`, `TranscriptRow`, `BookingRow`. No `Customer`, `CustomerIdentity`, `CustomerFact`.

**What to build:**
- New tables via Alembic migration:
  - `customers` (id, tenant_id, created_at, updated_at, display_name)
  - `customer_identities` (id, customer_id, channel [phone|whatsapp|email|ghl|crm], value, verified_at)
  - `customer_facts` (id, customer_id, key, value, source, updated_at)
- Service: `CustomerIdentityResolver.resolve(channel, value, tenant_id)` — returns Customer, creating one if none exists.
- Service: `CustomerMemoryService.remember(customer_id, key, value, source)` / `.recall(customer_id, key)`.

**Files touched:** `apps/api/app/db/models.py` (schema), `apps/api/alembic/versions/<new>.py` (migration), new `packages/customer/` package with resolver + service + tests.

**DB tables:** `customers`, `customer_identities`, `customer_facts`.

**Depends on:** none (foundation for T-SP5, T-SP6, T-SP11).

**Test plan:**
- Unit: resolve phone `+923301111111` twice → same customer_id. Then resolve whatsapp `+923301111111` → attaches identity to SAME customer (same phone digits).
- Unit: remember(customer, "preferred_provider", "Rosa") then recall → returns "Rosa".
- Integration: call from a phone number, hang up, call again → session sees `existing_customer_id`.
- Real call: same, verified in DB after two calls from same number.

**Definition of done:** two calls from the same caller number both attach to the SAME `customers` row; DB reflects both `session_ids` under one customer.

**Estimate:** 1 day.

---

### T-SP5 — BusinessTask durable state

**Exists?** NO. `DialogueState.TaskState` is conversation-local (dies with the call).

**What to build:**
- Table: `business_tasks` (id, tenant_id, customer_id, task_type [BOOK_APPOINTMENT|CALLBACK_REQUEST|MISSED_CALL_RECOVERY|...], status [OPEN|IN_PROGRESS|WAITING_CUSTOMER|WAITING_PROVIDER|COMPLETED|FAILED|CANCELLED], priority, created_at, due_at, context_json, authoritative_refs_json).
- Service: `BusinessTaskService.open(customer_id, task_type, context)` / `.update_status(task_id, status)` / `.list_open_for(customer_id)`.
- On call start: if `existing_customer_id`, load open BusinessTasks and inject into the brain's context.

**Files touched:** DB models + migration, new `packages/business_task/` package, `apps/api/app/routes/twilio_actor.py` (load-tasks-on-start hook).

**DB tables:** `business_tasks`.

**Depends on:** T-SP4 (Customer).

**Test plan:**
- Unit: open task → status becomes OPEN. Update → status transitions. List_open filters by customer + status.
- Integration: caller starts booking, agent asks phone, caller hangs up mid-booking → task saved as `WAITING_CUSTOMER`. Caller redials → agent picks up: "Hey Abbas, welcome back — we were mid-booking, want to finish?"
- Real call: same.

**Definition of done:** a customer starts a booking, hangs up, calls back an hour later, and the agent resumes the same booking task instead of starting over.

**Estimate:** 1-2 days.

---

### T-SP6 — OutcomeEngine + NextActionPolicy + ActionScheduler

**Exists?** NO. Outbound flows have some callback extraction but no generalized durable system.

**What to build:**
- **OutcomeEngine** — takes call end + tool results → emits typed `BusinessOutcome` (BOOKED, RESCHEDULED, CALLBACK_REQUESTED, NO_ANSWER, QUALIFIED, DID_NOT_QUALIFY, FAILED_TECHNICAL, etc). Stateless mapper.
- **NextActionPolicy** — takes `BusinessOutcome` + `Customer` + `BusinessTask` + tenant config → emits `NextActionDecision` (action, channel, execute_at, priority, reason[]). Composed of deterministic sub-policies: `PriorityPolicy`, `ChannelSelectionPolicy` (voice/SMS/WhatsApp), `ContactTimingPolicy` (TCPA hours + timezone), `CallbackPriorityPolicy`, `ConsentPolicy`, `EscalationPolicy`.
- **ActionScheduler** — durable table `scheduled_actions`. Worker polls, invokes `ActionExecutor` at `execute_at`. Executor dispatches to appropriate handler (place_call, send_sms, send_whatsapp, create_human_task).
- Closes the loop: after every execution, run OutcomeEngine on the result → NextActionPolicy again.

**Files touched:** new `packages/outcome/` + `packages/next_action/` + `packages/scheduler/` packages. DB migration for `scheduled_actions`.

**DB tables:** `scheduled_actions` (id, tenant_id, customer_id, task_id, action_type, payload_json, execute_at, status, attempts, last_error).

**Depends on:** T-SP4 (Customer) + T-SP5 (BusinessTask).

**Test plan:**
- Unit per policy: `ContactTimingPolicy` refuses to schedule 11pm call for a Texas number.
- Unit: OutcomeEngine maps `booked=true, tool_receipt=<id>` → `BusinessOutcome(BOOKED)`.
- Unit: NextActionPolicy given `BOOKED` outcome → decides `SEND_SMS_CONFIRMATION` immediately + `SEND_SMS_REMINDER` 24h before appointment.
- Integration: complete a booking → verify two rows appear in `scheduled_actions`, worker processes SMS immediately, second row waits.
- Real call: book an appointment → get SMS confirmation within seconds.

**Definition of done:** dial in, book, hang up, receive SMS confirmation with correct booking details. Also: a "call me tomorrow at 3" turn results in a real outbound call at that time.

**Estimate:** 2-3 days.

---

### T-SP7 — Outbox + Reconciliation

**Exists?** NO. Current code uses `asyncio.create_task(send_sms(...))` which is fire-and-forget — process crash after booking commit loses the SMS.

**What to build:**
- Table: `outbox_events` (id, tenant_id, event_type, payload_json, target_service [SMS|GHL|SLACK|WHATSAPP], status [PENDING|IN_FLIGHT|DELIVERED|FAILED|DEAD], attempts, next_attempt_at, last_error).
- Pattern: same DB transaction that commits the booking outcome ALSO inserts outbox_events. Committed atomically.
- Worker: `OutboxWorker` polls PENDING events, dispatches via appropriate adapter, records `DeliveryReceipt`. Retries with exponential backoff, moves to DEAD after N attempts.
- `ReconciliationService`: periodic job comparing Calendar bookings + GHL contacts + local BusinessTask.status — flags divergence.

**Files touched:** new `packages/outbox/`, DB migration. `packages/integrations/calendar_commit_adapter.py:_fire_confirmations_bg` becomes an outbox event insert instead of asyncio.create_task.

**DB tables:** `outbox_events`, `delivery_receipts`.

**Depends on:** T-SP4 + T-SP5 + T-SP6 (all business layer). Also T-SP2 CalendarAdapter for reconciliation.

**Test plan:**
- Unit: worker retries failed SMS 3× with backoff then marks DEAD.
- Integration: kill worker mid-delivery → restart → event delivers on next tick (idempotent).
- Integration: reconciler detects a BusinessTask.COMPLETED with no matching GHL contact → flags for manual review.
- Real call: complete booking, kill uvicorn between DB commit and SMS send, restart → SMS still gets sent.

**Definition of done:** demo — book, kill server mid-flight, restart, SMS still delivers.

**Estimate:** 1-2 days.

---

### T-SP8 — Tenant runtime config

**Exists?** DB tenancy exists (`tenant_id` on every row). Runtime tenancy DOES NOT — `_business_cache`, `_calendar_cache`, `_sink_cache`, `_retriever_cache` are process globals. `load_business()` reads one `BUSINESS_PROFILE_PATH`. WhatsApp uses `tenant_id = "default"`.

**What to build:**
- `TenantRuntimeConfig` dataclass: business_profile, integration_config, voice_config, policy_config.
- `TenantConfigRepository`: loads from DB or filesystem based on tenant_id.
- `TenantSecretResolver`: per-tenant API keys via env template + vault (start with env vars keyed by tenant_id).
- Kill the process globals. Replace `session_manager.load_business()` with `session_manager.load_business(tenant_id)`.

**Files touched:** `apps/api/app/core/session_manager.py`, new `packages/tenant_config/`, new DB table `tenant_configs`.

**DB tables:** `tenant_configs` (tenant_id PK, business_profile_json, integrations_json, voice_json, policies_json).

**Depends on:** none (independent).

**Test plan:**
- Unit: two tenants have different business names → concurrent turns for both return the correct name.
- Integration: fire 2 simultaneous calls to 2 different tenants → each hears its own greeting.
- Regression: existing single-tenant "default" behavior unchanged.

**Definition of done:** two tenants provisioned. Two callers dial the same server → each hears their own business's greeting.

**Estimate:** 1-2 days.

---

### T-SP9 — DNIS route resolution

**Exists?** Twilio already delivers `from_number` + `to_number` in the start event. No resolver code.

**What to build:**
- Table: `inbound_routes` (dialed_number PK, tenant_id, location_id, brand, rag_namespace, calendar_id, ghl_location_id, greeting_override).
- Service: `InboundRouteResolver.resolve(dialed_number)` → `InboundRoute` object.
- Wire into TwiML voice webhook: on incoming call, resolve → pass into TwilioActorSession as `tenant_id + route`.

**Files touched:** DB migration, new `packages/inbound_route/`, `apps/api/app/routes/twilio.py` (voice webhook), `apps/api/app/routes/twilio_actor.py` (accept route param).

**DB tables:** `inbound_routes`.

**Depends on:** T-SP8 (tenant runtime config).

**Test plan:**
- Unit: resolve `+15551234567` → returns configured route or None.
- Integration: two Twilio numbers point to the same server → dialing each hears different greetings (from route config).
- Real call: buy two Twilio numbers, provision two clinics, verify.

**Definition of done:** demo — one deployment serves 2+ clinic brands via different Twilio numbers.

**Estimate:** 0.5 day.

---

### T-SP10 — CRMAdapter interface

**Exists?** `packages/integrations/ghl_client.py` implements ~6 operations (upsert contact, add note, create opportunity, get free slots, book appointment).

**What to build:**
- Protocol: `CRMAdapter` with contact lookup/upsert, custom fields, tags, opportunity lookup/update, pipeline transitions, tasks, owner assignment, appointments, DNC, consent, workflow triggers, webhooks, two-way reconciliation.
- `GoHighLevelCRMAdapter`: wraps existing GHL client, extends with missing ops.
- Later impls (behind adapter): `HubSpotCRMAdapter`, `SalesforceCRMAdapter`, `ZohoCRMAdapter`, `GenericWebhookCRMAdapter`.

**Files touched:** new `packages/crm/` interface + adapter. Existing `sinks.py` becomes a thin wrapper.

**DB tables:** none (adapters call remote CRM APIs).

**Depends on:** T-SP4 (Customer — for identity mapping to CRM contact IDs).

**Test plan:**
- Unit per operation with mocked HTTP.
- Integration: cassette-recorded GHL API test.
- Real call: book appointment → GHL sandbox shows the contact + appointment + note.

**Definition of done:** switching from GHL to HubSpot is a config change (implementation swap), not a brain-code change.

**Estimate:** 1-2 days for GHL adapter refactor. HubSpot/SF are add-ons later.

---

### T-SP11 — Full SMS/WhatsApp Channel wiring

**Exists?** Transport for both exists — `packages/channels/whatsapp.py`, some SMS. But state doesn't cross channels.

**What to build:**
- `TwilioSMSChannel(Channel)` for inbound SMS.
- Route inbound WhatsApp/SMS messages through `CustomerIdentityResolver` → attach to existing `Customer`.
- Load open `BusinessTask` for that customer → resume in the appropriate channel.
- SMS/WhatsApp adapters registered with `OutboxWorker` as delivery targets.

**Files touched:** `packages/channels/twilio_sms.py` (new), `packages/channels/whatsapp.py` (identity resolution), `packages/outbox/adapters/` (delivery targets).

**DB tables:** none new.

**Depends on:** T-SP4 (Customer) + T-SP5 (BusinessTask) + T-SP7 (Outbox).

**Test plan:**
- Integration: WhatsApp message from a customer with an OPEN booking task → agent responds with "want to finish the booking we started on the call?"
- Real: text your Twilio number after a call → agent replies.

**Definition of done:** end-to-end: call starts booking, hangs up, customer texts, agent continues on SMS, agent sends confirmation via SMS.

**Estimate:** 1-2 days.

---

---

## Speed + reliability tracks (T-SP-SPEED-1..6)

These items were the original T1..T10 in WORKING-NOTES from earlier work.  They don't belong in the business-layer arc (T-SP1..T-SP11) but they DO need to ship — they're what makes each business-layer feature FAST + RELIABLE enough to demo.

Run these PARALLEL to Threads A-D above.  Most are small.

### T-SP-SPEED-1 — Async smart-turn ONNX (SHIPPED 2026-08-18)

**Exists?** SHIPPED — see WORKING-NOTES session log. Smart-turn moved to background worker, `asyncio.to_thread`, O(1) cached-value provider. 200ms poll interval. Verified on `CA0aee80af478ca22ff0ef62e34196549b` — zero ZOMBIE, zero 260ms lag spikes.

**Definition of done:** ✅ done.

### T-SP-SPEED-2 — Deepgram Flux flip (T1b deferred)

**Exists?** Config supports `deepgram_use_flux=True` but comment at `.env:180` says it emitted empty events on Twilio mulaw during 2026-08-11 test.  Currently OFF.

**What to build:**
- Isolation bench script: pipe captured Twilio mulaw bytes into a Flux WS directly, confirm real events come back.
- If bench passes: flip `.env` to `DEEPGRAM_USE_FLUX=true`.  Watch for false EOTs on 2-3 live calls.
- If Flux works: consider making smart-turn a SHADOW verifier (T-SP-SPEED-2b) since Flux has native EOT.

**Depends on:** none.

**Test plan:** offline bench script + 2-3 live calls comparing Nova-3 vs Flux for EOT accuracy.

**Definition of done:** Flux running in prod, EOT latency down (~260ms per Deepgram docs), false-EOT rate not worse than Nova-3.

**Estimate:** 2-3 hours (bench + monitored flip).

### T-SP-SPEED-3 — T4c full utterance_id refactor

**Exists?** T4a is shipped (owns_lock=True passes ownership through). ChatGPT's original P0 #4 was a bigger refactor introducing `utterance_id` + `response_attempt_id` separate from `turn_generation`.

**HELD.** Audit says T-SP1 (SemanticPlan wire-up) supersedes this — SemanticPlan enforces "one plan per turn" structurally, which achieves the same ownership discipline.  Only ship T-SP-SPEED-3 if T-SP1 doesn't fully eliminate same-gen multi-fire and lock-veto weirdness.

**Definition of done:** decide after T-SP1 lands — either "not needed, T-SP1 killed it" or "still needed, ship now."

**Estimate:** IF NEEDED, 1-2 days.

### T-SP-SPEED-4 — Groq/Cerebras fast-brain routing (was T9 fast-brain / deep-brain lanes)

**Exists?** Router provider abstraction exists (`app/providers/llm/router_llm.py`).  Groq + Cerebras keys configured.  Currently the router picks by cost/quality, not by turn intent.

**What to build:**
- Add a `_lane_hint` field to `SemanticPlan.delivery_intent` (or reuse existing hint).
- Simple decisions ("hello", "yes", "no", control turns) → Groq GPT-OSS 20B (~1000 tok/s).
- Medium complexity (FAQ answers via cache/RAG) → Groq GPT-OSS 120B or Cerebras (~500-3000 tok/s).
- Complex reasoning (multi-slot bookings, edge cases) → OpenAI/Anthropic.
- The router already has fallback + cooldown; we're only adding an explicit hint for the primary choice.

**Depends on:** T-SP1 (SemanticPlan wire-up provides the lane hint).

**Test plan:**
- Unit: given a `SemanticPlan(operation=ACKNOWLEDGE)` → router picks Groq.
- Real call: FAQ turn should log `provider=groq`, complex booking should log `provider=openai`.
- Measure: simple-turn E2E drops from ~1.5s to <900ms.

**Definition of done:** measurable p50 drop on the FAQ-heavy call cohort.

**Estimate:** 1 day.

### T-SP-SPEED-5 — Speculative read prefetch (was T7)

**Exists?** No. Read-only operations (check_availability, CRM customer lookup, RAG queries) fire only AFTER the LLM decides it needs them.

**What to build:**
- On partial STT text containing high-signal keywords ("book", "friday", "cleaning", customer name), speculatively fire the likely read tools IN PARALLEL with the brain.
- Results stashed in a per-turn `SpeculativeReadCache`.
- When brain requests a tool that's already in the cache → return immediately.
- If caller changes intent mid-utterance → discard cached reads.
- **NEVER speculate writes** — no book, create_booking, send_sms, cancel_*.

**Depends on:** T-SP1 (semantic plan tells us what reads to speculate) + T-SP2 (CalendarAdapter with safe read ops).

**Test plan:**
- Unit: partial "book for friday" → CalendarAdapter.get_availability called before brain finishes.
- Integration: measure end-to-end LLM turn latency for booking flows — target 400-800ms drop.
- Real call: same.

**Definition of done:** booking-flow p50 turn latency drops to <1000ms.

**Estimate:** 1-2 days.

### T-SP-SPEED-EXTRA — OpenAI-specific TTFT levers (added 2026-08-20)

Full research + citations: `docs/openai-speed-research-2026-08-20.md`. Ranked by impact × ease.

- [x] **SPEED-EXTRA-A: OpenAI Fast tier** — SHIPPED 2026-08-20. `.env OPENAI_SERVICE_TIER=fast`. Bench-measured 1534ms → 772ms TTFT on real prompt.
- [ ] **SPEED-EXTRA-B: max_tokens shrink** — `max_tokens=300` → `120` in streaming path. ~5 min. Cuts total-response ~50%. Do BEFORE ChatGPT's humanness rewrite so we know the baseline. Watch for reply truncation on booking-confirmation turns; if it clips, tag those as `speech_act=CONFIRM_ACTION` and let those use higher cap.
- [ ] **SPEED-EXTRA-C: Prompt-cache enhancements** — add `prompt_cache_key=<business_id>` param (routes to same backend, boosts hit rate under concurrent load) + `prompt_cache_retention="24h"` (extends TTL beyond default 5-10 min). Add `cached_tokens` telemetry to per-call log. ~30 min. Turns caching from "hopefully working" to "measured working."
- [ ] **SPEED-EXTRA-D: Warm structured-output schemas at boot** — send dummy request with our tool schemas (including `emit_semantic_plan`) at server boot. Eliminates the 200-400ms schema-compile penalty on the first turn of every call. ~30 min. Right after `router: initialized` in main.py warmup sequence.
- [ ] **SPEED-EXTRA-E: Predicted Outputs** — pass `prediction` parameter with likely opener when SemanticPlan.operation is GREET / ACKNOWLEDGE / CONFIRM / APOLOGIZE. Docs report 15-40% TTFT drop on hits. ~1 hour. Needs T-SP1 SemanticPlan to be actually firing (verify on next call first — currently the LLM ignores the tool in some turns).
- [ ] **SPEED-EXTRA-F: Prompt trim 24k → 8k chars** — do WITH ChatGPT's humanness rewrite. Cut redundant restatements + long EXAMPLES + duplicated PERSONA. 20-35% TTFT drop. Load-bearing sections (TIME, PHONE, BOOKING, COMPLIANCE, HALLUCINATION) MUST survive. Not before ChatGPT's rewrite arrives (risk of throwaway).
- [ ] **SPEED-EXTRA-G: Bench gpt-4.1-nano** — extend `scripts/llm_ttft_bench.py`. If it's meaningfully faster with acceptable tool-calling quality, wire it as fastpath. ~1 hour bench + variable wire-up.
- [ ] **SPEED-EXTRA-H: Realtime API (`gpt-realtime`)** — DEFERRED. 1-2 week rewrite. Reserve for "premium fast lane" tier if a specific client demands sub-500ms.

### T-SP-SPEED-6 — OpenAI persistent WS tool continuation (was T5)

**Exists?** Persistent WS scaffolded (`openai_persistent_ws_enabled` config flag), but tool calls currently fall back to HTTP → reruns the full turn (expensive).

**What to build:**
- Send tool results back on the SAME OpenAI Responses WS conversation.
- Use `previous_response_id` continuation model.
- Also benchmark OpenAI's Fast processing tier for the voice-agent latency lane.

**Depends on:** T-SP1 (so tool results feed back into a validated SemanticPlan, not raw text).

**Test plan:**
- Unit: mock WS → verify tool result sent on same conversation, no rerun.
- Real call: check_availability turn should NOT trigger a second full LLM call.

**Definition of done:** tool-heavy turns (booking flow with 2 tool calls) drop from 3-4s to 1.5-2s.

**Estimate:** 4-6 hours (only if OpenAI stays the primary LLM after Groq routing lands).

---

## Reliability tail (T-SP-RELIABILITY-1..4)

Small parallel items. Each is <1 hour.

### T-SP-RELIABILITY-1 — Per-call log regex fix (ChatGPT P1 #8)

**Exists?** `packages/observability/per_call_logger.py` regex `\bCA[0-9a-f]{32}\b` doesn't match `twilio_CA...` session prefixes.  Half of useful log lines (heard:, LLM_STREAM_*, brain-job) don't land in per-call logs — they only go to uvicorn.log.

**What to build:** fix regex to allow `twilio_` prefix: `\b(?:twilio_)?CA[0-9a-f]{32}\b`. Or match on session_id substring.

**Test plan:** unit test the regex on both formats + a live call → verify per-call log contains `heard:` lines.

**Definition of done:** per-call logs are self-contained; don't have to grep uvicorn.log for context.

**Estimate:** 15 min.

### T-SP-RELIABILITY-2 — ANI `{{From}}` template expansion (ChatGPT P1 #11)

**Exists?** TwiML `<Parameter>` sends literal `{{From}}` instead of expanded caller number.  R3 phase 3 ANI resolver has zero real caller-ID data.

**What to build:** in `/twilio/voice` webhook, parse the incoming Twilio POST form fields (`From`, `To`, `CallerName`) and inject actual values into the TwiML `<Parameter>` XML before returning it.

**Test plan:** unit test the TwiML builder with `From=+15551112222` → assert output XML has that value not `{{From}}`.  Live call → verify per-call log shows `caller='+92...'` not `caller='{{From}}'`.

**Definition of done:** real caller-ID reaches the actor, ANI resolver can accept/use it.

**Estimate:** 30 min.

### T-SP-RELIABILITY-3 — Same-gen TTS multi-fire verify

**Exists?** T4a shipped; earlier gen=11/17 had 5-8 TTS_STREAM_START on the same gen. Post-T4a, expected to be gone but not fully verified.

**What to build:** if verified GONE on next call → mark done, no code. If STILL present → ship T-SP-SPEED-3 (full utterance_id) as targeted fix.

**Test plan:** dial in, do 5+ LLM turns, count `TTS_STREAM_START` per gen. Should be 1 per gen. If not, investigate.

**Definition of done:** ≤1 TTS_STREAM_START per gen on any recent call log.

**Estimate:** 15 min verify + up to 1-2 days if fix needed.

### T-SP-RELIABILITY-4 — Response cache hit measurement

**Exists?** T3.5 shipped (cache warmed with 60 FAQ variants at boot). Effectiveness unmeasured on a real call.

**What to build:** dial with FAQ-heavy script ("insurance?", "hours?", "location?", "parking?") — verify `RESPONSE_CACHE HIT` in log for each, measure E2E latency.

**Test plan:** just make the call and grep the log.

**Definition of done:** at least 4 of 6 FAQ questions in a call hit cache; those turns have E2E <500ms.

**Estimate:** 5 min after next call.

---

## Scale track (T-SP-SCALE-1..3)

Multi-call verified working at n=10 (2026-08-19 spike). Real limits at higher concurrency need work.

### T-SP-SCALE-1 — ElevenLabs connection pool raise

**Exists?** `_shared_clients` has `max_connections=20, max_keepalive_connections=10`. Saturates around n=20-30 concurrent calls.

**What to build:** raise limits to `max_connections=100, max_keepalive_connections=50`.  Add per-format metrics (open-connections gauge, queue depth).

**Depends on:** none.

**Test plan:** re-run `scripts/multi_call_probe.py --n 30` — first-media should stay <500ms.

**Definition of done:** n=30 concurrent probe passes with p50 first-media <400ms.

**Estimate:** 30 min.

### T-SP-SCALE-2 — Process-level scaling via uvicorn workers

**Exists?** Server runs as single uvicorn process. CPU-bound at ~100+ concurrent calls (ONNX + TTS decoding + log I/O).

**What to build:** `run_server.sh` uses `--workers 4` (or gunicorn+uvicorn workers) + shared response cache DB continues to work (SQLite WAL is process-safe).  Boot-time warmup happens per worker.

**Depends on:** verify all singletons are worker-safe (they're process-local so each worker warms its own).

**Test plan:** `multi_call_probe.py --n 50` on multi-worker config — should scale roughly linearly.

**Definition of done:** n=50 concurrent probe passes.

**Estimate:** 2-4 hours (config + verification).

### T-SP-SCALE-3 — OpenAI/Groq rate limit backoff

**Exists?** Router has cooldown, but no coordinated backoff across concurrent calls hitting the same provider.

**What to build:** add a token-bucket rate limiter per provider; when rate limit hit, cooldown fires globally not per-call. Cross-call fallback stays intact.

**Depends on:** none.

**Test plan:** synthetic n=50 with only OpenAI available → verify no cascade failures, just per-call fallback.

**Definition of done:** n=50 calls at 100% OpenAI don't cascade into 429 storms.

**Estimate:** 4 hours.

---

### T-SP12 — Killer dental demo

**Exists?** Working single-vertical demo (Smile Dental Clinic).

**What to build:** End-to-end scripted demo showing the closed loop:
1. Inbound call: caller "Hi, want to book a cleaning for tomorrow 2pm, name Abbas, phone 03303172789"
2. Agent: fast reply, uses SemanticPlan (no wrong-time), fires check_availability + book_appointment
3. On call end: OutcomeEngine emits `BOOKED`, NextActionPolicy schedules `SEND_SMS_CONFIRMATION` (immediately) + `SEND_SMS_REMINDER` (24h before) + `SYNC_TO_GHL` (immediately) + `SEND_WHATSAPP_FIRST_VISIT_INFO` (immediately)
4. Outbox delivers all 4
5. Customer texts back "can I move to 3pm?" → BusinessTask.RESCHEDULE opens → agent (via SMS) resolves availability, reschedules, updates calendar + GHL
6. Reconciliation confirms all systems agree

**Files touched:** demo script doc + rehearsal recording.

**Depends on:** T-SP1 through T-SP11.

**Test plan:** rehearse the demo 3 times without a manual assist. Record final video.

**Definition of done:** 5-minute recorded demo suitable for an Upwork proposal + client pitch. Uploaded somewhere accessible.

**Estimate:** 1 day.

---

## Parallel work threads

**T-SP items don't all need to be done in one line.** They form 4 parallel threads that can advance simultaneously:

### Thread A — Intelligence (speed + correctness of what the agent says)
Order: T-SP1 → T-SP3
Blocks: nothing else waits on this thread, but T-SP1 is the biggest lever
Ship velocity: 1-2 days for T-SP1, 0.5 day for T-SP3

### Thread B — Business layer (Customer, Task, NextAction — the loop)
Order: T-SP4 → T-SP5 → T-SP6 → T-SP7
Blocks: T-SP11, T-SP12, all commercial features
Ship velocity: 5-8 days total, each item testable independently

### Thread C — Deployability (multi-tenant, routing, adapters)
Order: T-SP8 → T-SP9 → T-SP10
Blocks: agency sale, white-label
Ship velocity: 2-4 days total

### Thread D — Integrations (the last mile to a real client)
Order: T-SP2 → T-SP11 → T-SP12
Blocks: nothing after
Ship velocity: 3-5 days total, but each item is client-visible

### Reliability tail (parallel, small, do in gaps)
- Per-call log regex fix (ChatGPT P1 #8) — 15 min
- ANI `{{From}}` expansion (ChatGPT P1 #11) — 30 min
- Same-gen TTS multi-fire — verify killed by T4a on next call; if not, targeted fix
- Fallback/retry — largely covered by T-SP7 Outbox

### Speed-only work (parallel, longer)
- Groq/Cerebras routing for simple turns (Lane B in ChatGPT roadmap) — 1-2 days, only after T-SP1 (so plan can guide routing decisions)
- OpenAI persistent WS tool continuation — 4-6 hours
- Full utterance_id refactor (T4c) — only if T-SP1 doesn't fully fix intelligence bugs

**Dependency graph:**
```
                T-SP1 (SemanticPlan wire) ──┬── T-SP3 (RAG evidence)
                                            ├── SPEED-4 (Groq routing)
                                            ├── SPEED-5 (speculative reads, also needs T-SP2)
                                            └── SPEED-6 (persistent WS tool continuation)

T-SP4 (Customer) ─── T-SP5 (BusinessTask) ─── T-SP6 (Outcome/NextAction/Sched)
                                                     ─── T-SP7 (Outbox)
                                                             │
                                                             ├─── T-SP10 (CRMAdapter)
                                                             │
                                                             └─── T-SP11 (SMS/WhatsApp Channels)
                                                                          │
T-SP8 (Tenant config) ─── T-SP9 (DNIS)                                    │
                                                                          │
T-SP2 (CalendarAdapter) ──────────────────────────────────────────────────┘
                                                                          │
                                                                          ▼
                                                             T-SP12 (Killer demo)

INDEPENDENT (do in gaps):
  SPEED-1 ✅ shipped   SPEED-2 (Flux bench+flip)
  RELIABILITY-1..4     SCALE-1..3 (only when we outgrow n=10)
```

---

## Explicit "do NOT rebuild" list (from both audits)

The following ALREADY EXIST and are strong. Do not create parallel implementations.

- `packages/runtime/call_actor.py` — `CallActor`, temporal ownership, per-generation cancellation
- `packages/runtime/turn_manager.py` — turn state machine, EAGER/CONFIRM/RESUMED events
- `packages/dialogue/state.py` — `DialogueState`, conversation state
- `packages/dialogue/reducer.py` — event → state reducer
- `packages/dialogue/commit.py` — `CommitCoordinator`, action_id, idempotency
- `packages/core_agent/speech_commit_gate.py` — SpeechCommitGate
- `packages/dialogue/plan.py` — SemanticPlan schema (T-SP1 WIRES this, doesn't rebuild)
- `packages/rag/evidence.py` — EvidenceBundle schema (T-SP3 WIRES this)
- `packages/slot_parsers/` — StructuredInputSession, PhoneParser (EXTEND via `register_slot_type`, don't rebuild)
- `packages/integrations/ghl_client.py` — GHL operations (T-SP10 WRAPS this, doesn't replace)
- `packages/integrations/google_calendar.py` — partial (T-SP2 UPGRADES this, doesn't replace)
- `packages/channels/whatsapp.py` — transport (T-SP11 CONNECTS this to Customer)
- `apps/api/app/providers/` — all provider abstractions (LLM router, STT, TTS)
- `packages/observability/` — telemetry + failure intelligence
- `packages/response_cache/` — SQLite response cache (WARMED via `common_turns.py` in current work)
- `packages/voice/filler.py` — filler pool (recency-avoiding pick)

---

## Estimated effort table

| Thread | Total days |
|---|---|
| Thread A (Intelligence: T-SP1 + T-SP3) | 1-2 days |
| Thread B (Business layer: T-SP4-7) | 5-8 days |
| Thread C (Deployability: T-SP8-10) | 3-5 days |
| Thread D (Integrations: T-SP2 + T-SP11 + T-SP12) | 3-5 days |
| Speed track (SPEED-2 Flux, SPEED-4 Groq routing, SPEED-5 speculative reads, SPEED-6 persistent WS, SPEED-3 T4c if needed) | 3-5 days |
| Reliability tail (RELIABILITY-1..4) | ~0.5 day scattered |
| Scale track (SCALE-1..3, only for beyond-demo prod) | 1-2 days |
| **TOTAL** | **~17-29 focused days** |

Not calendar time — that's actual dev days. Calendar time depends on how many hours per day. At 4 focused hours/day = 4-7 weeks. At 8 hours/day = 3-4 weeks.

**SPEED-1 (async smart-turn) already SHIPPED. SPEED-3 (T4c) may not be needed if T-SP1 kills the same-gen multi-fire.**

---

## First-N-days task queue (concrete)

Assumes: focused sessions, one item at a time, always verify on a real call before moving on.

**Day 1 (today, 2026-08-19):**
- ✅ Write this unified plan doc
- Start T-SP1 SemanticPlan wire-up (4-8 hours)
- Ship it, verify on a real call, measure whether wrong-time / dropped-follow-up bugs disappear

**Day 2:**
- T-SP1 verification + fixes based on Day 1 real-call
- T-SP3 EvidenceBundle wire (half day)
- Reliability tail: per-call log regex + ANI expansion (~1 hour)

**Day 3:**
- T-SP2 CalendarAdapter — Google Calendar with idempotency + timezone (1 day)

**Day 4-5:**
- T-SP4 Customer + CustomerIdentity (1 day)
- T-SP5 BusinessTask (1-2 days)

**Day 6-8:**
- T-SP6 OutcomeEngine + NextActionPolicy + ActionScheduler (2-3 days)

**Day 9-10:**
- T-SP7 Outbox + Reconciliation (1-2 days)

**Day 11-12:**
- T-SP8 Tenant runtime config (1-2 days)

**Day 13:**
- T-SP9 DNIS (0.5 day)
- T-SP10 CRMAdapter start (0.5 day)

**Day 14-15:**
- T-SP10 finish (1-2 days)

**Day 16-17:**
- T-SP11 SMS/WhatsApp Channels (1-2 days)

**Day 18:**
- T-SP12 Killer demo (1 day)

**After that** — pursue individual client integrations, HubSpot adapter, missed-call recovery flow, outbound speed-to-lead. Everything the audits list past #22.

---

## What to update after each task

At the end of every T-SP item:
1. Mark `[x]` in WORKING-NOTES.md TODO list.
2. Add session log entry.
3. Update "Current state" paragraph.
4. Record the shipped CallSid + measurements if applicable.
5. **Update this file** — mark the T-SP item's "Definition of done" as achieved, add any lessons learned, note any deferred sub-tasks.

If any T-SP item's real work turns out different from what's specified here — UPDATE this file before ending the session. Future Claudes read this doc first.
