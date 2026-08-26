# Receptionist Agent — Full Codebase Audit

**Audit date:** 2026-08-26  
**Bundle:** `receptionist-codebase-2026-08-26_1912-audit-2026-08-26.zip`  
**Audit lanes:**
1. Backend / CRM / Security / Compliance
2. Humanness / Receptionist Capability / Runtime Intelligence

This is a progress audit against the two 2026-08-25 audits **plus a fresh architectural sweep for gaps those audits did not catch**. It is deliberately not a voice-latency/STT/TTS tuning audit.

---

# 0. Executive verdict

## Ship verdict

**Do not put this build in front of unrelated paying multi-tenant customers yet.** The codebase does **not** need a rewrite. The core architecture has many good pieces, but there are several high-blast-radius integration seams where the intended safety/intelligence component exists without actually controlling the live path.

The most important newly identified blocker is larger than yesterday's individual tenant bugs:

> **The HTTP/DB layer is becoming multi-tenant, but the live receptionist runtime is still process-global single-business.**

`apps/api/app/core/session_manager.py:26-54` caches one global `BusinessProfile`, one global calendar adapter, and one global CRM sink. `start_session_with_id()` accepts a `tenant_id`, but `session_manager.py:126-132` still loads the same process-wide business and builds the brain against that same global calendar/sink configuration. Admin provisioning stores tenant business data in tenant metadata, but the runtime does not resolve that tenant-specific business configuration before creating a call.

That means fixing the existing `"default"` tenant bypass alone is insufficient. Two legitimate tenants can still be isolated at the SQL row level while being served the wrong **business persona, calendar, and CRM credentials** at the application-runtime level.

## Highest-priority architecture delta

Build one explicit per-tenant runtime boundary:

```text
Inbound identity
  -> IntegrationIdentityResolver
  -> tenant_id + business_id
  -> TenantRuntimeContextResolver
  -> TenantRuntimeContext
       business profile
       calendar adapter + credentials
       CRM/sinks + credentials
       telephony ownership
       compliance policy
       feature flags
       limits/budgets
  -> CallSession / ReceptionistBrain
```

Cache **by tenant_id/business_id**, not process-wide. Do not let any live call or API path obtain a business/calendar/sink without a tenant identity.

## What materially improved since 2026-08-25

- `/debug/*` was removed from the ordinary public HTTP allowlist and production mounting is feature-gated.
- `PhoneNumberMapping` + `resolve_tenant_from_phone()` now exist.
- `short_ticket.py` now provides a good fail-closed HMAC ticket primitive.
- Twilio `/status` signature verification is present.
- HubSpot has a real transient retry policy including 429/Retry-After.
- Google Calendar now has `find_by_phone`, `cancel`, and `reschedule` lifecycle support.
- `AcknowledgmentKind` exists in `NextActionPolicy`.
- Twilio actor shutdown now attempts a REST-side call termination after local teardown.
- Real-estate tooling now exposes `take_message`.
- Focused regression tests around these newer components are strong: **157/157 passed** in the audit environment.

## What has *not* changed enough

The recurring failure pattern remains:

> **A correct component has been built, but the legacy/live ingress or conversation path bypasses it.**

Examples: phone tenant resolver exists but Twilio stream still hardcodes `default`; short ticket exists but public chat/voice APIs still fail open; NextActionPolicy exists but only governs a narrow post-booking synthesizer; ReactiveBrain exists but its commit lane has no tools; `take_message` exists but does not persist a message; `END_CALL` exists as a policy enum but the live call lifecycle is still mostly farewell heuristics and timers.

---

# 1. Backend / CRM / Security / Compliance audit

## P0 — launch blockers

### [B-P0.0] NEW — runtime tenant isolation is incomplete above the database

**Files:**
- `apps/api/app/core/session_manager.py:23-54`
- `apps/api/app/core/session_manager.py:96-137`
- `apps/api/app/routes/admin.py` tenant business-profile provisioning
- `packages/integrations/calendar_factory.py`
- `packages/integrations/sinks.py:684+`

**Status:** PENDING — **new highest backend priority**

**What's on disk:** SQL/API tenant IDs, tenant DB guard machinery, phone mapping, and tenant metadata provisioning.

**What's missing:** the actual runtime dependencies are not resolved from tenant identity. `load_business()`, `get_calendar()`, and `get_sink()` are process-wide singletons. `start_session_with_id()` accepts `tenant_id` but still uses the one globally loaded business.

**Impact:** wrong business persona, wrong calendar, wrong CRM write destination/credentials, and wrong business policy for otherwise correctly authenticated tenants. This can create cross-customer data/action leakage without any SQL cross-tenant query.

**Concrete fix:**
- Create `TenantRuntimeContextResolver.resolve(tenant_id, business_id=None)`.
- Load tenant business profile from DB/tenant configuration, not one JSON file.
- Build/cache calendar and integration adapters by `(tenant_id, business_id, config_version)`.
- Store per-tenant secrets in a secret store/encrypted configuration layer; never global env for SaaS integrations.
- Pass the resulting context into session creation and every tool factory.
- Add a two-tenant integration test proving tenant A can never invoke tenant B's calendar or CRM mock.

---

### [B-P0.1] `"default"` is still a supertenant for live in-memory sessions

**File:** `apps/api/app/core/session_manager.py:140-203`

**Status:** PENDING

`get_session()` still checks:

```python
if state.tenant_id != tenant_id and tenant_id != "default":
    return None
```

The same privileged bypass exists in `end_session()` and `end_session_async()`.

`middleware/auth.py` also maps the single `API_KEY` compatibility path to tenant `"default"`.

**Fix:** exact ownership equality only. A development default tenant must be a real tenant row with no privileged semantics. In production, strongly consider rejecting bootstrap configuration that maps a generic API key to the literal tenant ID `default`.

**Acceptance test:** create A/B/default sessions; `default` must receive the same 404/denial as B when trying to read/end A.

---

### [B-P0.2] `/debug/*` HTTP is better, but debug WebSocket and cross-tenant telemetry remain unsafe

**Files:**
- `apps/api/app/middleware/auth.py:37-94`
- `apps/api/app/routes/debug.py:232+`, `348+`, `366-405`
- `apps/api/app/main.py` observability router gating

**Status:** PARTIAL

**Done:** `/debug/` is no longer in the public HTTP prefix list; production router mounting is gated.

**Still wrong:**
- Authenticated debug HTTP handlers operate over global traces/call event logs and accept `tenant_id` filters as caller input. A normal tenant key is not equivalent to an admin observability credential.
- `/debug/live` calls `await ws.accept()` with no explicit ticket/admin authentication. `BaseHTTPMiddleware` does not secure WebSocket scopes.

**Fix:** admin-only observability surface or tenant-scoped queries bound to authenticated identity; use a short-lived signed WSS ticket bound to `tenant_id + call_id` and verify it **before** `accept()`.

---

### [B-P0.3] Public AI/STT/TTS endpoints still fail open

**Files:**
- `apps/api/app/middleware/auth.py:40-94`
- `apps/api/app/routes/chat.py`
- `apps/api/app/routes/voice.py`
- `apps/api/app/routes/elevenlabs_compat.py`
- `apps/api/app/core/config.py:335`
- `packages/auth/short_ticket.py`

**Status:** PENDING, despite the ticket primitive being DONE

`/chat/`, `/voice/`, `/v1/`, `/call-stream/` remain public. `/v1/*` only enforces the compatibility key if `compat_api_key` is configured, and its default is `None`.

This is a provider-balance and denial-of-service boundary, not merely login UX.

**Fix:**
- Widget bootstrap endpoint authenticates tenant/site and mints a short ticket.
- Chat/STT/TTS endpoints require the ticket and derive tenant from it.
- `/v1/*` is fail-closed: when no compat key/service credential is configured, return 503, never anonymous access.
- Add per-IP + per-tenant request/audio/token quotas and payload size limits.

---

### [B-P0.4] Signed/tenant-resolved telephony ingress is only scaffolded; Twilio live WSS still bypasses it

**Files:**
- `apps/api/app/routes/twilio.py:659-674`
- `apps/api/app/telephony/tenant_from_phone.py:1-38, 92+`
- `packages/auth/short_ticket.py:1-66, 157+`
- `apps/api/alembic/versions/20260825_0003_phone_number_mappings.py`

**Status:** PARTIAL

The resolver and ticket primitives are well designed. The migration even states that inbound WSS should resolve the dialled number before brain dispatch.

But the live handler still does:

```python
await ws.accept()
...
await handle_twilio_stream_via_actor(ws, tenant_id="default")
```

The resolver therefore exists as an unused security component.

**Recommended trust chain:** signed `/twilio/voice` webhook -> resolve destination number -> mint short ticket bound to `CallSid + tenant_id` -> embed in `<Stream>` URL -> WSS verifies ticket before accept -> create actor under ticket tenant. Reject unknown number/ticket mismatch.

---

### [B-P0.4b] NEW — SignalWire, Telnyx, Plivo, Vapi and channel ingress repeat the same default-tenant design

**Files:**
- `apps/api/app/routes/signalwire.py:99,121`
- `apps/api/app/routes/telnyx.py:126,149`
- `apps/api/app/routes/plivo.py:150,169`
- `apps/api/app/routes/vapi.py:141,216`
- `apps/api/app/routes/channels.py:45`

**Status:** PENDING

Fixing Twilio alone will reproduce this audit when the second provider is enabled.

**Fix:** one `IntegrationIdentityResolver`, with adapters for:
- phone-number ownership
- provider account/subaccount/location ID
- Vapi assistant/phone identity
- WhatsApp phone-number ID
- Telegram bot/config identity

Then one provider-specific signature verifier/ticket bridge, all producing the same internal `InboundIdentity(tenant_id, business_id, provider, external_call_id)`.

---

### [B-P0.5] Direct outbound dial still bypasses policy and allows caller-ID selection

**File:** `apps/api/app/routes/outbound.py:340-416`

**Status:** PENDING

The direct `/outbound/dial` route does not call the existing kill-switch/policy/consent decision path. It accepts arbitrary `to` and a client-supplied `from_number`, then directly invokes Twilio.

The same architectural bypass should be assumed dangerous for each provider-specific `/dial` implementation unless routed through one policy service.

**Fix:** create a single `OutboundCallService.place_call()` that enforces, in order:
1. global + tenant kill switches
2. tenant-owned caller ID
3. destination normalization
4. DNC/revocation
5. consent policy where applicable
6. quiet hours/jurisdiction
7. cooldown/frequency caps
8. tenant spend/concurrency cap
9. idempotency/audit record
10. transport adapter dial

No route should call a telephony client's raw dial method directly.

---

### [B-P0.6] HIPAA-CONDITIONAL — Lightsail remains inappropriate for ePHI processing

**Status:** PENDING / conditional on healthcare production

The repository still carries a Lightsail deployment path and SQLite-first storage. AWS's current HIPAA Eligible Services list (July 2026) includes EC2/RDS/ECS/Fargate and does not list Lightsail. AWS states that PHI should only be processed/stored/transmitted in HIPAA-eligible services under the applicable BAA.

**Fix for healthcare mode:** EC2/ECS/Fargate or another eligible compute path, RDS PostgreSQL, AWS BAA, required downstream BAAs, encryption/key policy, audit/retention controls, and an explicit `compliance_mode=hipaa` that rejects non-approved sinks/providers.

Do not block ordinary non-healthcare US pilots on this if healthcare data is out of scope.

---

## P1 — must harden before scaling pilots

### [B-P1.1] UPDATE/DELETE tenant guard is still syntactic, not semantic

**Files:** `apps/api/app/db/tenant_guard.py:62-81,103-136`; `apps/api/app/db/session.py:105-140`

**Status:** PENDING

The guard considers a statement tenant-filtered if compiled SQL contains `tenant_id` plus `WHERE`/`ON`; it does not prove the predicate binds to the current tenant. Compile failure is permissive. ORM auto-injection is SELECT-only.

**Fix:** inject current-tenant criteria into ORM SELECT/UPDATE/DELETE and make privileged cross-tenant execution an explicit capability/context. Do not use SQL-text substring inspection as the authorization decision.

---

### [B-P1.2] ORM/Alembic nullability drift remains

**File:** `apps/api/app/db/models.py:114,132,152`

**Status:** PENDING

Core tenant-scoped rows still model `tenant_id` as optional/nullable while migration policy has moved it to NOT NULL. Align ORM and migration invariants.

---

### [B-P1.3] Idempotency race and uniqueness mismatch remain

**Files:** `apps/api/app/db/models.py:85,92`; `apps/api/app/db/idempotency.py:56-99,133-169,176-198`

**Status:** PENDING

Lookup key is `(tenant_id, key, scope)`, but the unique constraint is `(tenant_id, key)`. The decorator executes the side effect before inserting the idempotency row. The webhook helper explicitly documents its check-then-set race.

**Fix:** unique `(tenant_id, scope, key)`; reserve an idempotency row atomically before mutation (`INSERT ... ON CONFLICT DO NOTHING`); represent IN_PROGRESS / COMPLETED / FAILED and return/recover deterministically.

---

### [B-P1.4] CRM durable outbox is still absent

**Files:**
- `packages/integrations/sinks.py`
- `packages/integrations/hubspot_client.py:90-91,171-175`
- `apps/api/alembic/versions/20260825_0003_phone_number_mappings.py:12-14`

**Status:** PENDING

The code itself references an outbox as future work. The migration comment explicitly says `integration_outbox` was deferred. Composite sink isolation prevents one sink from crashing the call, but it also means final CRM delivery can disappear after logs.

**Fix:** transactional `integration_outbox` + delivery worker + retry schedule + dead-letter state + idempotent destination keys. Calls should enqueue business events; connectors deliver them asynchronously.

---

### [B-P1.5] HubSpot transient retry policy is DONE

**File:** `packages/integrations/hubspot_client.py:78-176`

**Status:** DONE

408/429/500/502/503/504 plus network errors retry; `Retry-After` is honored with fallback backoff/jitter; non-retryable auth/validation errors fail directly.

**Minor improvement:** pool/reuse an `httpx.AsyncClient` rather than constructing a client for each attempt.

---

### [B-P1.6] SMS consent and messaging governance are still missing

**Files:**
- `packages/integrations/sinks.py:577-606`
- `packages/integrations/sms_sender.py:63+`
- `packages/compliance/tcpa.py`
- `apps/api/app/core/config.py:125-130`

**Status:** PENDING

The outbound voice compliance provider is not the same thing as booking-confirmation SMS consent. `FollowupSink` directly sends a confirmation when a phone/time is present. There is no durable tenant-scoped `SmsConsent`, no global/per-tenant `messaging_enabled` send-boundary gate, and the generated SMS advertises `Y/N/STOP` without a complete inbound state machine.

**Fix:** durable consent/suppression ledger keyed by tenant + normalized/hash phone; STOP/START/HELP/Y/N processing; send-boundary kill switch; consent source/timestamp/revocation; idempotent message dispatch.

---

### [B-P1.7] Postgres configuration accepts an async dialect with a sync engine

**File:** `apps/api/app/db/session.py:72-80`

**Status:** PENDING

The comment says `postgresql+asyncpg` is accepted, but the code uses synchronous `create_engine`/`SessionLocal`. Reject asyncpg URLs in this architecture or complete an `AsyncEngine/AsyncSession` migration. For the current design, standardize `postgresql+psycopg://`.

---

### [B-P1.8] Backups/RPO/restore discipline remain absent

**Status:** PENDING

SQLite WAL settings are a useful local hardening measure, not a production recovery strategy. Before paid tenants: managed PostgreSQL + automated backups/PITR + tested restore procedure + documented RPO/RTO.

---

### [B-P1.9] Retention and PII log hygiene remain weak

**Files:**
- `packages/observability/call_event_log.py:452-468`
- per-call logger and call/transcript logging sites
- `packages/schemas/business.py:60-61`

**Status:** PENDING

Call-event-log default retention is effectively unlimited when `CALL_EVENT_LOG_RETENTION_DAYS=0`; per-call logs have historically been intentionally persistent. Multiple runtime logs include transcript fragments, phone data, and call content outside the DB PII redactor.

**Fix:** one logging-boundary PII filter/allowlist, per-tenant retention policy, and a GC job covering DB transcripts, call-event DB, per-call files, cached artifacts, and downstream sinks where contractual deletion is supported.

---

### [B-P1.10] API-key lifecycle is incomplete

**File:** `apps/api/app/routes/admin.py:108+`

**Status:** PENDING

Key issuance exists; explicit key revocation/deletion and cache invalidation do not form a complete operator workflow.

**Fix:** revoke endpoint, audit event, immediate `_db_key_cache` invalidation, key status/created/last-used metadata, and eventually scopes/roles rather than one tenant-wide bearer authority.

---

### [B-P1.11] Canonical incident trace still missing

**Status:** PENDING

The project has many useful observability pieces, but no authoritative operator view joining:

`call -> tenant/business -> transcript -> decisions -> tool calls -> booking -> transfer -> CRM deliveries -> SMS/email -> provider call status -> end reason`.

Build this on a normalized event ledger, not by querying arbitrary debug globals.

---

### [B-P1.12] NEW — `/metrics` is public but contains `tenant_id` labels

**Files:** `apps/api/app/middleware/auth.py:46`; `packages/runtime/telemetry.py:115+`, `443-473`

**Status:** PENDING

The auth comment says `/metrics` has “no tenant data,” while Prometheus metrics are explicitly labelled with `tenant_id`. This exposes customer identifiers and internal operational data to unauthenticated scraping when metrics are mounted.

**Fix:** private-network scrape or admin auth, and consider opaque internal tenant labels instead of business identifiers.

---

### [B-P1.13] NEW — no tenant cost/abuse control plane

**Status:** PENDING

There is provider retry/rate-limit handling, but not SaaS-level protections against a tenant or caller consuming unbounded LLM/STT/TTS/telephony spend.

Add:
- per-tenant concurrent-call cap
- max call duration
- daily/monthly spend/usage budget
- per-IP widget quota
- repeated-ANI/redial abuse threshold
- max upload/audio sizes
- circuit breakers per external provider
- bounded tool/action counts
- alert at budget thresholds

This is required before public widget endpoints or multiple tenants become real.

---

### [B-P1.14] GDPR data-subject workflow is absent

**Status:** PENDING for EU pilot

No coherent subject access/export/delete route or worker was found. A European customer needs a way to find a caller across sessions/transcripts/bookings/messages/call events and propagate deletion/rectification through supported sinks.

**Fix:** create a subject index based on normalized/hash phone/email, DSR request record, access/export operation, erasure operation with legal-retention exceptions, destination deletion adapters, and audit proof.

---

### [B-P1.15] AI disclosure policy must be jurisdiction/compliance-mode driven

**Files:** `packages/schemas/business.py:55-63`; `packages/core_agent/prompt.py:18-35`

**Status:** PARTIAL

The Utah correction is good: when directly asked, the prompt says the system is the virtual receptionist rather than denying AI use.

However `ai_disclosure_enabled=False` remains the business-profile default. For an EU pilot in August 2026, this should not be left to a tenant's casual toggle. EU Article 50 transparency obligations now apply and the Commission says people interacting directly with AI should be informed from the start of the first interaction unless it is obvious.

**Fix:** `compliance_mode` / jurisdiction policy selects the required disclosure behavior. For EU mode, startup/config validation should reject a greeting policy that omits required initial disclosure.

---

### [B-P1.16] Recording notice configuration conflates “recording exists” with “notice enabled”

**Files:** `packages/schemas/business.py:60-61`; `packages/compliance/jurisdiction.py:159+`

**Status:** PENDING

There is no authoritative `recording_enabled` + `recording_notice_policy`. Compliance audit logic infers recording state from `recording_notice_enabled`, which can create false assurance.

**Fix:** model actual recording/transcription retention separately from notice policy and fail configuration validation when the two are inconsistent for a jurisdiction.

---

### [B-P1.17] Twilio call-status verification is DONE, but post-call dispatch is not

**File:** `apps/api/app/routes/twilio.py:610-656`

**Status:** PARTIAL

Signature verification is present. Completed status still emits `TWILIO_STATUS_COMPLETED_TODO`; reliable follow-up/outbox dispatch is not wired.

---

# 2. Humanness / Receptionist Capability / Runtime Intelligence audit

The previous audit's central diagnosis remains correct: the codebase has unusually good conversation primitives, but production behavior is still too often an LLM improvising the next sentence rather than a stateful receptionist policy selecting the next action.

## P0 — competitive receptionist behavior

### [H-P0.1] `NextActionPolicy` is still not the general turn controller

**Files:**
- `packages/dialogue/next_action_policy.py:1-4,197+,293+`
- `packages/core_agent/next_action_synthesizer.py:244-249`
- `apps/api/app/core/config.py:505`

**Status:** PARTIAL

The file itself still says `NOT WIRED TO RUNTIME`. The one real construction found builds `ConversationDecisionState` in the deterministic post-booking synthesizer. This is valuable, but narrow.

**Smallest correct delta:** make every committed caller turn produce/update `ConversationDecisionState`, invoke `NextActionPolicy`, and treat its selected action/ack/delivery intent as the authority. Let the LLM verbalize the action rather than decide from scratch what the business action is.

Do **not** just set the feature flag to true and assume the integration is complete.

---

### [H-P0.2] Semantic acknowledgments exist as an enum but real signals do not populate the decision state

**File:** `packages/dialogue/next_action_policy.py:114+,197+`

**Status:** PARTIAL

`AcknowledgmentKind` includes NONE/LISTEN/UNDERSTOOD/CORRECTION/EMPATHY/AGREEMENT/TRANSITION/WAIT. `ConversationDecisionState` has the kinds of inputs needed to select them.

But the fields such as caller correction, hardship, dictation/waiting and previous acknowledgment are not fed by a general runtime reducer. The post-booking synthesizer's construction does not populate the broader conversational signals.

**Fix:** a bounded `TurnSignalReducer` should combine transcript intent + acoustic state + existing DialogueState into the decision state. Policy chooses the semantic ACK; realization chooses wording. This is the difference between “Gotcha” roulette and contextually correct listening behavior.

---

### [H-P0.3] ReactiveBrain is wired to a branch but **unsafe to enable globally**

**Files:** `apps/api/app/routes/twilio_actor.py:4661-4681,4929-4958`; `packages/core_agent/reactive_brain.py`

**Status:** PARTIAL / deployment blocker for this feature

The actor does route to `_brain_job_reactive()` behind `reactive_brain_enabled`.

However `_brain_job_reactive()` invokes:

```python
reactive_turn(..., tools=None, ...)
```

and then returns from the normal committed path. A reactive “commit” therefore does not run the ordinary tool-capable receptionist brain in the same way. Turning this on globally risks making calls *sound* more responsive while silently degrading booking/CRM/tool correctness.

**Correct architecture:** ReactiveBrain is a lane controller:
- SILENT -> do nothing / maintain state
- BACKCHANNEL -> lightweight ACK
- COMMIT -> delegate to the normal tool-capable action/policy execution path

Only after COMMIT shares the normal tool/guard semantics should it graduate from controlled cohorts.

---

### [H-P0.4] Human transfer is still simulated, not a telephony primitive

**Files:** `packages/integrations/clinic_tools.py:373-382`; similar restaurant/real-estate handlers

**Status:** PENDING

The tool reports `{"escalated": true, "callback_number": ...}` without an actual dial/conference/bridge outcome. The LLM can therefore say a human was connected when no transfer happened.

**Build `TransferCoordinator`:**
- `TransferDestination`
- `TransferRule`
- `TransferAttempt`
- `TransferOutcome`
- BLIND / WARM / CALLBACK / MESSAGE_IF_FAILED
- timeout/no-answer/busy/failed behavior
- warm handoff summary
- return-to-AI or message fallback

The verbalizer must not say “connected” until a transport-level receipt confirms the bridge.

---

### [H-P0.5] `take_message` moved from absent to PARTIAL, but it is still conversational simulation

**File:** `packages/integrations/real_estate_tools.py:307-325,600-615`

**Status:** PARTIAL

Real estate now exposes `take_message`; that is genuine progress. Its handler only returns a `ToolResult` object containing the message. There is no durable `ReceptionMessage` table/model, delivery workflow, queue/inbox status, or generalized clinic/restaurant primitive.

**Fix:** persist first, then acknowledge. Model recipient/department, subject, body, priority, callback preference, status, delivery attempts and timestamps. Route urgent messages differently from normal ones. Surface them in the receptionist inbox.

---

### [H-P0.6] END_CALL improved operationally, but semantic ownership is still incomplete

**Files:**
- `packages/dialogue/next_action_policy.py:375-377`
- `apps/api/app/routes/twilio_actor.py:1148-1177`
- farewell scheduling in `twilio_actor.py:2777+`
- stream end reasons around `twilio_actor.py:6155-6160`
- `apps/api/app/core/config.py:373`

**Status:** PARTIAL

**Improvement:** actor `stop()` now best-effort calls `_end_twilio_call()` via Twilio REST, fixing the old “our WSS ended but the phone leg stays in progress” behavior. The farewell scheduler waits for playout and aborts if the caller resumes.

**Still missing:** policy-level `END_CALL` does not generally control this lifecycle because NextActionPolicy is not the general controller. Farewell-text pattern recognition still triggers termination. The default config also has `twilio_use_actor=False`, so a fresh/default deployment may not be using the improved actor path at all.

There is also end-reason vocabulary drift: the skip list checks `caller_hangup` / `ws_closed`, while stream termination uses reasons such as `stop-event` / `ws-disconnect`. This can cause redundant REST termination attempts and makes observability less authoritative.

**Fix:** one `CallEndReason` enum and state machine. Semantic END_CALL -> farewell -> TTS playout receipt -> provider hangup -> status callback confirmation. Caller hangup/provider stop should produce a different terminal reason and cancel pending farewell work.

---

### [H-P0.7] Google Calendar basic lifecycle parity is DONE

**File:** `packages/integrations/google_calendar.py:249-378` plus `find_by_phone`

**Status:** DONE for basic lifecycle

Cancel/reschedule/find-by-phone are implemented. This closes a real demo-vs-production gap.

It does **not** close the broader scheduling-domain gap below.

---

## P1 — capability parity / productization

### [H-P1.1] Returning callers are still anonymous at conversation start

**Status:** PENDING

No `CallerResolver` / `CallerContext` runtime was found. ANI exists but is not converted into a compact pre-turn customer context containing known caller, upcoming appointment, prior call/open issue, CRM record, preferred language/staff, etc.

**Fix:** resolve before greeting where possible; treat CRM reads as cached/context retrieval, not something the LLM must discover by improvising tool calls after the caller repeats themselves.

---

### [H-P1.2] Acoustic/emotional features are not closing the loop into policy

**Status:** PENDING/PARTIAL

The repository computes useful acoustic signals and NextActionPolicy contains delivery/affect concepts, but no general per-turn path fuses these into the decision state.

Keep this bounded: acoustic signals should affect pacing, acknowledgment and interruption tolerance, not be treated as medical/emotional truth.

---

### [H-P1.3] Business operating model is still too flat for real multi-location SMBs

**File:** `packages/schemas/business.py:36-63`

**Status:** PENDING

One profile has one weekly hours object, one address, one escalation phone, services and FAQs. Missing first-class:
- locations
- departments
- staff/providers
- staff->service eligibility
- resources/rooms
- per-location/per-provider calendars
- split hours
- holidays/time off/temporary closures
- buffers and appointment rules
- staff-first overflow routing
- after-hours routing

This should be structured state, not prompt text.

---

### [H-P1.4] Receptionist inbox is still fragments, not an operating surface

**Status:** PARTIAL

Dashboard/call/transcript/booking pieces exist. Durable messages, transfer outcomes, knowledge gaps, CRM delivery status, follow-up tasks and unresolved caller issues do not form one workflow.

Target: one call outcome ledger and one operator inbox, not multiple debug pages.

---

### [H-P1.5] Knowledge-gap feedback loop is absent

**Status:** PENDING

Low-confidence/unknown business questions should create a durable `KnowledgeGap` after the agent safely declines/asks to take a message. Owner answers once; KB is updated; future calls benefit. This creates compounding receptionist intelligence instead of repeated hallucination risk.

---

### [H-P1.6] Multilingual is infrastructure, not a live receptionist workflow

**Status:** PARTIAL

Language-capable STT/LLM pieces exist, but there is no complete automatic chain:

`detect/confirm language -> STT locale -> conversation locale -> LLM -> matching TTS voice/language -> remember preference`.

For European pilots this rises in priority.

---

### [H-P1.7] SemanticPlan is not dead, but it is model-driven metadata rather than control authority

**Status:** PARTIAL

The normal brain can emit/parse a semantic plan and use facts from it, so this is not dead code. But it remains optional/model-produced and does not replace the deterministic next-action controller.

Keep SemanticPlan as a representation/realization aid; use state/policy for permission to act.

---

### [H-P1.8] Booking truth guard is good on the normal brain path, but alternate lanes can bypass it

**Status:** PARTIAL

The normal brain has anti-confabulation checks for claiming a booking without a tool result. The ReactiveBrain path's `tools=None` commit lane does not share the same tool-result/booking-truth path.

Consolidate every committed reply behind one `ActionExecutionResult` / claim guard before TTS.

---

# 3. Cross-cutting architecture recommendation

The two audits are actually describing the same architectural problem at different layers:

```text
SECURITY problem:
identity/resolver exists -> legacy ingress bypasses it

HUMANNESS problem:
policy/state exists -> legacy LLM-turn path bypasses it

RELIABILITY problem:
outbox/idempotency concept exists -> direct side effect bypasses it
```

The right fix is not more patches; it is a small number of **mandatory choke points**.

## Mandatory choke point 1 — `InboundIdentityGateway`
Every external request/call/channel enters through provider verification and tenant resolution. Nothing gets `tenant_id="default"` by convenience.

## Mandatory choke point 2 — `TenantRuntimeContext`
Every business/calendar/CRM/compliance dependency comes from the tenant context. No global business/calendar/sink singleton in SaaS runtime.

## Mandatory choke point 3 — `ConversationController`
Every committed user turn updates state and gets exactly one policy decision. The LLM is primarily a natural-language realizer and reasoning helper, not the final authority to book/transfer/end/send.

## Mandatory choke point 4 — `ActionExecutor`
Booking, transfer, message, follow-up and CRM effects execute through typed actions with guards, idempotency and receipts. The agent may only verbally claim effects that have receipts.

## Mandatory choke point 5 — `IntegrationOutbox`
External CRM/messaging writes leave the call latency path and become durable/retryable.

## Mandatory choke point 6 — `CallLifecycleCoordinator`
One source of truth for ACTIVE / TRANSFERRING / WRAPPING / ENDED and provider-confirmed end reason.

---

# 4. Test and code-health assessment

## Static health

`python -m compileall` succeeds across the inspected application/packages. No broad syntax failure was found.

## Focused regression slice

An expanded focused suite around the new/fixed surfaces produced:

**157 passed, 0 failed**

including debug auth/live tests, short tickets, phone tenant resolver, Twilio status signature, HubSpot retry, Google Calendar lifecycle, NextActionPolicy/synthesizer wiring tests and ReactiveBrain tests.

This is a good sign: several newly built components are individually solid.

## Broad suite

The broad API test run in this audit environment produced:

- **1313 passed**
- **72 failed**
- **54 skipped**
- **24 errors**

Do **not** interpret this as 96 product bugs. A large share is environment/dependency/configuration related:
- `phonenumbers` unavailable in the audit environment although declared in `apps/api/requirements.txt`
- `sqlite-vec` unavailable although declared
- `onnxruntime` unavailable for smart-turn tests
- `num2words` behavior/dependency causes speech sanitizer expectations not to match
- provider route tests expect configured public URLs/signature settings

However, several failures deserve real triage rather than being dismissed as environment noise:
- `ScriptedLLM.complete()` test double no longer accepts the `site=` keyword used by production interfaces
- `test_router_capabilities::test_allam_is_last_in_groq_alternates`
- `test_turn_manager::test_speech_resume_after_final_fires_turn_resumed`
- Python 3.13 event-loop assumptions in streaming pipeline tests
- phone-precondition tests returning unexpected `None` paths

**Release criterion:** install the declared production/test dependency lock in CI and make the suite classify failures by mandatory vs optional feature. A production branch should not rely on an auditor mentally filtering dependency failures.

---

# 5. Legal/compliance update relevant to this build

This section is product-engineering guidance, not legal advice.

## US outbound AI calls

FCC Declaratory Ruling 24-17 treats AI-generated voices as artificial/prerecorded voices under the TCPA and states that callers using them must obtain prior express consent absent an applicable exemption/emergency. This reinforces why `/outbound/dial` cannot bypass policy.

Source: FCC 24-17, https://docs.fcc.gov/public/attachments/FCC-24-17A1.pdf

## Utah AI disclosure

Utah Code §13-77-103 requires disclosure when a consumer clearly asks whether the interaction uses AI; regulated/high-risk interactions can have stronger start-of-interaction duties. The current prompt's “virtual receptionist” response when asked is materially better than the previous deny/never-confirm behavior.

Source: https://le.utah.gov/xcode/Title13/Chapter77/C13-77-S103_2025050720250507.pdf

## EU AI Act — now live

Article 50 transparency obligations apply from **2 August 2026**. The European Commission's current guidance says people should be notified from the start of the first direct interaction with an AI system unless it is obvious they are interacting with AI.

Sources:
- https://digital-strategy.ec.europa.eu/en/library/guidelines-transparency-obligations-providers-and-deployers-ai-systems
- https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act

For an EU real-estate pilot, `ai_disclosure_enabled=False` cannot remain a casual default.

## GDPR

The product currently lacks the operational workflow needed to make access/erasure/retention controls easy. GDPR Article 5 includes storage limitation/security; Articles 15 and 17 establish access and erasure rights.

Source: https://eur-lex.europa.eu/eli/reg/2016/679/art_17/oj/eng

## HIPAA/AWS

AWS's July 2026 HIPAA Eligible Services Reference lists EC2/RDS/ECS/Fargate and other services eligible for ePHI; Lightsail is not in that list. AWS also says PHI should only be processed/stored/transmitted using HIPAA-eligible services subject to the BAA.

Sources:
- https://aws.amazon.com/compliance/hipaa-eligible-services-reference/
- https://aws.amazon.com/compliance/hipaa-compliance/

---

# 6. What I would implement next — ordered by dependency, not by file

## Gate A — make multi-tenancy real end-to-end

1. **TenantRuntimeContextResolver** — tenant-specific business/calendar/CRM/secrets/compliance/limits.
2. Remove privileged `default` semantics.
3. **IntegrationIdentityResolver** for all telephony/channel providers.
4. Wire short-ticket WSS trust chain before `accept()`.
5. Secure chat/STT/TTS/widget APIs with signed tenant tickets.
6. Lock `/metrics` and debug live streams.

**Exit test:** two tenants with two numbers, two business names, two mocked calendars and two CRM accounts. Run calls concurrently. Every prompt, booking, transfer, CRM event and metric is attributable to the correct tenant with no global leakage.

## Gate B — make receptionist actions true, not conversational claims

7. **ConversationController**: reducer -> DecisionState -> NextActionPolicy on every turn.
8. **ActionExecutor** with receipts/claim guard.
9. ReactiveBrain becomes lane selection; COMMIT delegates into normal tool-capable controller.
10. **TransferCoordinator** with warm/blind/failure fallback.
11. Durable **ReceptionMessage** + inbox routing.
12. Semantic **END_CALL** + canonical call lifecycle/end reason.

**Exit test:** agent is unable to say “booked”, “transferred”, “message sent”, or equivalent unless the corresponding receipt exists.

## Gate C — make integrations reliable and governable

13. Transactional integration outbox + worker.
14. Correct idempotency reservation semantics.
15. SMS consent/suppression + send kill switch.
16. Provider-wide outbound call policy service.
17. Postgres + backup/PITR + restore drill.
18. Per-tenant cost/concurrency/abuse controls.

## Gate D — make it a receptionist product rather than a call demo

19. Locations/staff/departments/resources/time-off/holidays.
20. CallerResolver / returning-caller context.
21. KnowledgeGap feedback loop.
22. Unified receptionist inbox + standardized outcomes.
23. Multilingual live routing.
24. DSR/export/delete + retention control plane.

---

# 7. Suggested implementation classes/modules

These names are intentionally concrete so Claude Code can map them into the repo rather than turning this audit into more theory.

```text
packages/runtime/tenant_context.py
  TenantRuntimeContext
  TenantRuntimeContextResolver
  TenantRuntimeCache

packages/integrations/identity.py
  InboundIdentity
  IntegrationIdentityResolver
  ProviderIdentityAdapter

packages/dialogue/controller.py
  ConversationController
  TurnSignalReducer
  ConversationDecisionStateBuilder
  ActionRealizer

packages/actions/executor.py
  ActionExecutor
  ActionReceipt
  ActionClaimGuard

packages/integrations/transfer.py
  TransferCoordinator
  TransferAttempt
  TransferOutcome

packages/integrations/outbox.py
  IntegrationOutboxService
  OutboxDeliveryWorker

packages/runtime/call_lifecycle.py
  CallLifecycleCoordinator
  CallEndReason

packages/reception/messages.py
  ReceptionMessageService
  MessageDeliveryPolicy

packages/compliance/policy.py
  TenantCompliancePolicy
  IdentityDisclosurePolicy
  RecordingNoticePolicy
  MessagingPolicy

packages/runtime/limits.py
  TenantUsageLimits
  UsageBudgetGuard
  AbuseGuard
```

The exact filenames can change; the key requirement is that these become **choke points**, not optional helper utilities that routes can bypass.

---

# 8. Acceptance-test pack to add before first unrelated tenant

1. **Tenant runtime isolation test:** A/B simultaneous calls see distinct business names, tools, calendars, CRM mocks and policy.
2. **Default tenant regression:** literal `default` has zero bypass privileges.
3. **WSS auth test:** invalid/expired/mismatched ticket is rejected before WebSocket accept.
4. **Provider identity matrix:** Twilio/SignalWire/Telnyx/Plivo/Vapi/channel ingress all resolve tenant via a common contract.
5. **Public AI abuse test:** unauthenticated chat/STT/TTS/v1 cannot consume provider resources.
6. **Outbound safety matrix:** kill switch, no consent, DNC, quiet hours, spoofed caller ID and budget exhaustion all deny before provider dial.
7. **Transfer truth test:** no “connected” wording until bridge receipt; no-answer returns to AI/message fallback.
8. **Message durability test:** `take_message` survives process restart and appears in inbox with delivery status.
9. **Policy signal test:** correction/hardship/dictation/waiting produce appropriate ACK semantics without repeated canned acknowledgment.
10. **Reactive commit test:** enabling ReactiveBrain does not disable booking/tool execution or booking claim guard.
11. **END_CALL test:** semantic end -> farewell played -> provider call closed -> status callback -> one canonical end reason.
12. **Outbox retry test:** CRM 429/503 survives process restart and eventually delivers once.
13. **Idempotency race test:** concurrent duplicate webhook/action yields one real side effect.
14. **Retention test:** expired PII disappears from all local stores/logs according to policy.
15. **DSR test:** caller export/delete finds all supported first-party records and queues downstream deletion.
16. **Cost guard test:** tenant budget/concurrency limit cuts off new work safely without affecting another tenant.

---

# 9. Things I would explicitly NOT spend time on yet

- Another giant persona/system prompt rewrite.
- Model swapping as the main “humanness” fix.
- More telephony providers before common ingress identity is fixed.
- More CRM connectors before tenant-specific credentials + outbox exist.
- General ReactiveBrain activation before the commit lane delegates to tool-capable execution.
- DTMF expansion beyond existing fallback until transfer/message/caller context are reliable.
- Fancy A/B testing before the runtime has per-tenant feature/config control and deterministic outcomes.

---

# 10. Final architecture judgment

This is **not a dumb voice bot codebase**. It already contains a stronger-than-average collection of primitives: interruption handling, semantic/dialogue structures, policy scaffolds, provider abstractions, commit/booking safeguards, telephony actor work, observability, RAG hooks, CRM sinks and an increasingly serious tenant/data layer.

The problem is that the project has grown horizontally faster than it has consolidated authority. There are now several ways to enter a call, several ways to make a decision, several ways to produce a side effect and several places to store configuration. The result is that the newest safe/intelligent subsystem can exist while an older route silently bypasses it.

The next milestone should therefore be **convergence**, not feature count:

> One identity path. One tenant runtime context. One conversation controller. One action executor. One external-delivery outbox. One call lifecycle.

Once those are mandatory, many of the current P0/P1 findings collapse at once, and the advanced intelligence you have already built will finally be able to affect every real call instead of only selected feature-gated branches.
