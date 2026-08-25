# VoiceOps — Codebase vs Market-Demand Gap Audit
**Codebase:** `receptionist-agent-code-2026-08-19.zip`  
**Audit date:** 2026-08-19  
**Basis:** Static architecture/code audit + repository working notes + comparison against the accumulated scheduled voice-agent market research.

---

# 0. 60-Second Executive Summary

## The most important conclusion

This repository is **not a basic voice bot** and should not be rebuilt around a giant new class hierarchy.

It already contains many of the hard production primitives we wanted:

- `CallActor` and generation-aware cancellation
- turn management / barge-in / playback control
- `DialogueState` and per-call task state
- `CommitCoordinator`
- `SpeechCommitGate`
- structured phone input framework
- RAG with structured `EvidenceBundle`
- provider-swappable STT / LLM / TTS
- detailed latency / call observability
- cost estimation
- multi-tenant DB guards
- Google Calendar
- GoHighLevel client/sink
- SMS/email confirmation
- WhatsApp/Telegram channels
- outbound dialing prototypes
- TCPA/PII components
- adversarial tests and call-transcript tooling
- n8n workflow examples

The repo's biggest missing layer is **above the individual call**:

```text
Customer
   ↓
Durable BusinessTask
   ↓
NextActionPolicy
   ↓
Voice / SMS / WhatsApp / Email
   ↓
Conversation runtime
   ↓
Business Outcome
   ↓
NextActionPolicy again
```

That is what turns the existing voice runtime into the commercial **Conversation / Revenue Operations OS** the market research is pointing toward.

## The five biggest gaps

1. **The real `SemanticPlan` architecture exists as schema but is not live.**  
   The current semantic planner is still post-hoc: the brain generates reply text first, then a wrapper classifies the speech act.

2. **No durable Customer / Identity / Customer Memory / BusinessTask model exists.**  
   `DialogueState.TaskState` is good, but it is a *within-conversation task*, not a business task that survives calls/channels.

3. **No general Outcome Engine → NextActionPolicy → Scheduler loop exists.**  
   Outbound dispositions are vertical-specific and spreadsheet-oriented.

4. **Commercial integrations are present but not production-complete.**  
   In particular, the safe commit path is FakeCalendar-shaped; Google Calendar lacks lifecycle/idempotency integration, and GHL is mostly a best-effort sink rather than authoritative two-way state.

5. **Database multi-tenancy exists, but operational multi-tenancy does not.**  
   Business profile, provider settings, calendar, CRM sink and messaging routes are effectively process-global/default-tenant.

## P0: what to do before adding major new features

- Finish the current T4 voice execution/ownership work and eliminate same-generation multi-fire.
- Make `SemanticPlan` plan-first instead of post-hoc.
- Make Google Calendar work through the same authoritative commit protocol as the fake calendar.
- Put booking, cancellation and rescheduling under one commit/idempotency/receipt protocol.
- Turn the structured RAG evidence path on and make it feed semantic plans.
- Stop solving exact-time / follow-up-intent errors only through prompt patches; preserve exact structured facts in code.

## P1: the most valuable new systems

Add:

```text
CustomerService
CustomerIdentityService
CustomerMemoryService

BusinessTaskService
OutcomeEngine

NextActionPolicy
ActionScheduler

OutboxService
DeliveryReceipt
RetryPolicy
ReconciliationService

InboundRouteResolver / DNIS routing
TenantRuntimeConfig / TenantIntegrationConfig
```

## P2: integrations to finish/build for Upwork

First:

```text
Google Calendar — COMPLETE lifecycle
GoHighLevel — normalized two-way adapter
Twilio SMS — inbound + outbound
WhatsApp — durable shared customer/task state
n8n — normalized event/webhook adapter
Slack — alerts / hot leads / transfer briefing
```

Then:

```text
HubSpot
Microsoft 365 Calendar
Microsoft Graph Email
Teams
Make
```

## What NOT to add

Do not create duplicate versions of:

```text
ConversationOrchestrator
DialogueState
CommitCoordinator
SpeechCommitGate
TurnManager
generic provider abstraction
generic phone parser
```

Those capabilities already exist. Extend/converge the existing runtime.

---

# 1. Audit Scope

The audit asks two different questions:

1. **What does the repository actually have today?**
2. **What is missing relative to current commercial voice-agent demand?**

The market baseline from the accumulated research is no longer simply:

```text
phone
+ LLM
+ TTS
```

The commercially important system increasingly looks like:

```text
Customer / Lead
       ↓
Shared business state
       ↓
NextActionPolicy
       ↓
Voice | SMS | WhatsApp | Email | Human
       ↓
Conversation runtime
       ↓
Authoritative tools
       ↓
Typed outcome
       ↓
CRM + scheduler + next action
```

This audit therefore scores the repository on both **call runtime engineering** and **customer/business orchestration**.

---

# 2. Audit Verification Notes

The repository was unpacked and inspected directly.

Static Python compilation succeeded:

```text
python -m compileall packages apps/api/app
→ OK
```

There are **91 `test_*.py` files** under `apps/api/tests`.

The repository's `WORKING-NOTES.md` reports a development baseline of:

```text
1179 passed
19 pre-existing failed
```

I did **not** independently reproduce the complete pytest baseline in the audit container because the container does not currently have `phonenumbers` installed. The dependency is declared correctly in `apps/api/requirements.txt`, so that missing import is an audit-environment dependency issue rather than evidence of a repository packaging defect.

The working notes also show that the team is actively testing real calls, measuring call-specific p50 latency, and converting failures into fixes. That is materially more advanced than most portfolio voice-agent repositories.

---

# 3. Current Architecture — What Already Exists

## 3.1 Temporal call runtime — STRONG

The repository already has:

```text
packages/runtime/call_actor.py
packages/runtime/call_event.py
packages/runtime/playback_ledger.py
packages/runtime/turn_manager.py
packages/runtime/heard_text_reconciler.py
packages/runtime/streaming_stt_bridge.py
```

`CallActor` is already the closest thing this repository needs to a low-level conversation orchestrator.

It owns:

- call lifecycle
- actor mailbox
- turn generations
- speech generations
- cancellation
- supervised tasks
- stale-result rejection
- temporal state transitions

The design explicitly protects against late results from cancelled turns.

### Recommendation

**Keep `CallActor`. Do not replace it with a new generic `ConversationOrchestrator`.**

The higher-level customer/task system should sit **above** it.

---

# 4. Turn Taking / Barge-In — STRONG BUT CURRENTLY ACTIVE P0

Existing:

```text
packages/runtime/turn_manager.py
packages/voice/barge_in.py
packages/voice/conversation_control.py
packages/runtime/heard_text_reconciler.py
```

The repo already tackles:

- false interruption
- resumed speech
- eager confirmations
- speculative handling
- fragmentation
- cancellation of stale work

This is exactly the kind of difficult-call engineering that current buyers increasingly pay for.

## Current problem

`WORKING-NOTES.md` shows a remaining high-priority issue:

```text
same-generation TTS multi-fire
```

Recent live calls exposed multiple `TTS_STREAM_START` events in the same generation and a speculative/K1 continuation interaction that could stack multiple answers.

There is also evidence of large latency variance:

- ~1.5–1.7s on some normal turns before recent changes
- ~2–3s and higher on recent real calls
- an extreme Turn 1 around 10s in one call

### Market implication

This work is not technical polishing. It directly maps to the **Voice Agent Rescue / Optimization** demand discovered in the scheduled research.

### P0

- [ ] Verify T4a on a larger live-call sample.
- [ ] Eliminate remaining same-generation multi-fire.
- [ ] Introduce `utterance_id` / `response_attempt_id` if ownership cannot be guaranteed through the current generation key.
- [ ] Record p50 / p90 / p95 latency after each runtime change.
- [ ] Preserve these diagnostics as portable audit tooling.

---

# 5. Dialogue State — ALREADY EXISTS

Existing:

```text
packages/dialogue/state.py
```

Important classes:

```text
DialogueState
ConversationAgenda
TaskState
TaskKind
TaskStatus
SlotEvidence
SlotStatus
```

This is a good design.

It already represents:

- multiple intents/tasks during a conversation
- slot evidence
- task status
- active/deferred agenda

## Important distinction

Do **not** confuse this with the new durable customer task system recommended by the market research.

`DialogueState.TaskState` answers:

> What tasks are active **inside this conversation**?

The missing future `BusinessTask` answers:

> What customer/business goal is still active **across calls and channels**?

Example:

```text
BusinessTask:
    BOOK_APPOINTMENT

Call 1:
    DialogueState task asks date
    caller hangs up

WhatsApp:
    customer sends date

Call 2:
    customer finishes booking
```

The three channel interactions belong to one durable `BusinessTask`.

### Recommendation

Keep `DialogueState.TaskState`.

Add a separate, clearly named object such as:

```python
BusinessTask
```

rather than adding another generic `TaskState`.

---

# 6. Semantic Planning — MAJOR ARCHITECTURAL GAP

Two separate semantic concepts exist.

## Current live planner

```text
packages/core_agent/planners/semantic.py
```

This wraps `ReceptionistBrain`.

The sequence is effectively:

```text
ReceptionistBrain generates reply
        ↓
tool loop already happened
        ↓
SemanticPlanner infers speech act afterward
```

Its own docstring explicitly describes this as a post-hoc wrapper.

## Stronger schema already exists

```text
packages/dialogue/plan.py
```

Classes:

```text
SemanticPlan
PlanOperation
PlannedFact
PlannedQuestion
DeliveryIntent
```

This is much closer to the correct target architecture:

```text
understand caller
→ produce semantic plan
→ validate facts/policy
→ perform tools/commit if needed
→ realize wording
```

The schema supports:

- sourced facts
- critical facts
- forbidden claims
- one question
- active task
- pending tasks
- deterministic handling of critical commitments

## Problem

Static reference search shows `SemanticPlan` is currently only exported/defined; it is not the live turn-planning protocol.

The file says a future:

```text
packages/core_agent/planners/semantic_v2.py
```

will implement it, but that implementation is not present.

### This should become P0/P1

The current recent live-call failure proves why.

A caller asked for **1:30**, but the model answered **2:30** even though 1:30 was valid.

The repository patched the prompt with:

> USE THE EXACT TIME THE CALLER SAID.

That is a sensible emergency patch, but the durable fix is architectural:

```text
caller time
→ parsed structured value
→ availability result
→ critical PlannedFact
→ deterministic realization
```

The LLM should not be free to substitute a different valid slot.

The same applies to the forgotten “tooth implants after” secondary intent.

That belongs in:

```text
DialogueState
+ SemanticPlan.pending_tasks
```

not only in prompt memory.

### Build

```text
packages/core_agent/planners/semantic_v2.py
packages/core_agent/realizer.py
```

Suggested classes:

```python
SemanticPlanGenerator
SemanticPlanValidator
SemanticRealizer
DeterministicCriticalRealizer
```

But reuse the existing `SemanticPlan` schema.

---

# 7. Commit / Transaction Safety — STRONG PRIMITIVE, INCOMPLETE PRODUCTION WIRING

Existing:

```text
packages/dialogue/commit.py
```

Classes:

```text
ActionProposal
CallerConfirmation
CommitCoordinator
CommitResult
CommitOutcome
CommitAdapter
```

This is very good.

The coordinator already has:

- proposal validation
- caller confirmation
- per-action locking
- deterministic idempotency concept
- commit result handling

This directly matches premium market demand around:

- avoiding double booking
- confirmation before committing
- tool failure correctness
- avoiding false claims

---

# 8. SpeechCommitGate — STRONG

Existing:

```text
packages/core_agent/speech_commit_gate.py
```

This is one of the strongest commercial differentiators in the repo.

It protects speech before TTS and explicitly covers unsupported commitments such as:

- bookings
- cancellations
- payments
- prices
- transfers
- RAG assertions

### Recommendation

Do not replace it.

Extend it so committed speech consumes normalized authoritative receipts across **all** business mutations.

---

# 9. Critical Calendar Gap — SAFE COMMIT PATH IS FAKE-CALENDAR SHAPED

This is one of the most important findings in the audit.

Existing commit adapter:

```text
packages/integrations/calendar_commit_adapter.py
```

Class:

```python
FakeCalendarBookingAdapter
```

Its own comment says a Google adapter is a future follow-up.

The adapter calls:

```python
calendar.book(
    ...,
    idempotency_key=proposal.idempotency_key,
)
```

But:

```text
packages/integrations/google_calendar.py
```

has:

```python
GoogleCalendar.book(...)
```

without an `idempotency_key` argument.

It also currently only implements:

```text
is_available()
list_slots()
book()
```

while clinic tools expose:

```text
find_existing_appointment
cancel_appointment
reschedule_appointment
```

Those lifecycle methods are implemented/tested against `FakeCalendar`, not Google Calendar.

## Consequence

The repo has the **correct safety architecture**, but it is not yet consistently applied to the real calendar adapter that a paying client is likely to use.

### P0/P1 fix

Create a true protocol:

```python
class CalendarAdapter(Protocol):
    async def get_availability(...)
    async def find_booking(...)
    async def create_booking(..., idempotency_key: str)
    async def reschedule_booking(..., idempotency_key: str)
    async def cancel_booking(..., idempotency_key: str)
```

Implement:

```text
FakeCalendarAdapter
GoogleCalendarAdapter
later MicrosoftCalendarAdapter
```

Then place **BOOK / RESCHEDULE / CANCEL** all behind `CommitCoordinator`.

---

# 10. Google Calendar Timezone Correctness — NEEDS HARDENING

Current Google adapter constructs:

```python
timeMin=start.isoformat() + "Z"
timeMax=end.isoformat() + "Z"
```

This is fragile.

If `start` is timezone-aware, `isoformat()` already contains an offset.

If it is naive, appending `Z` declares it UTC regardless of business timezone.

`list_slots()` also constructs times directly from the supplied `day` and defaults to hardcoded:

```text
09:00–17:00
```

while `FakeCalendar` is explicitly wired to business hours.

### This matters because the market research repeatedly surfaced:

- timezone bugs
- wrong booking times
- existing-agent repair jobs
- CRM/calendar divergence

### Build

A single canonical scheduling layer:

```python
SchedulingService
BusinessTimezone
AvailabilityQuery
AvailabilityResult
```

Rules:

- internal times are timezone-aware
- business timezone is explicit
- provider adapters convert only at boundaries
- returned slots carry full ISO timestamps, not only `"13:30"` strings
- business hours come from authoritative tenant config
- DST tests exist
- ambiguous/nonexistent local-time tests exist

---

# 11. Structured Input — FRAMEWORK EXISTS, ONLY PHONE IMPLEMENTED

Existing:

```text
packages/slot_parsers/session.py
packages/slot_parsers/registry.py
packages/slot_parsers/phone.py
packages/slot_parsers/phone_validator.py
```

Strong existing concepts:

```text
StructuredInputSession
SlotStatus
SlotSource
SlotFragment
SlotResult
```

Statuses already match the market-driven architecture:

```text
INCOMPLETE
POSSIBLE
VALID
AMBIGUOUS
INVALID
```

## Gap

The parser registry currently registers phone input only.

### Do not build a second StructuredInputManager

Extend the existing registry.

Add:

```text
EmailParser
DateParser
TimeParser
DateTimeParser
DOBParser
PostalCodeParser
AddressParser
CurrencyParser
IdentifierParser
```

Later vertical parsers:

```text
InsuranceMemberIdParser
ConfirmationCodeParser
PropertyAddressParser
```

### Priority

```text
P1:
email
date/time
postal/address

P2/P3:
DOB
insurance/member IDs
currency/custom identifiers
```

---

# 12. RAG — STRONG FOUNDATION, STRONGER PATH NOT YET LIVE

Existing:

```text
packages/rag/
```

includes:

- chunking
- embedding
- hybrid retrieval
- SQLite vector retrieval
- FTS
- evidence schema
- answerability
- voice shaping

Important:

```text
packages/rag/evidence.py
```

already defines:

```text
EvidenceBundle
EvidenceClaim
Answerability
```

This is good.

## Problem 1 — structured evidence path is optional/off

`LookupAnswerHandler` has:

```python
emit_evidence_bundle: bool = False
```

The normal vertical-tool construction does not turn it on.

So the stronger evidence representation exists, but the current main path still favors legacy shaped prose.

## Problem 2 — vector tenant filter is post-filtered

The SQLite vector path documents that sqlite-vec cannot push the business filter into kNN.

The code compensates by over-fetching globally and then fetching/filtering by:

```text
business_id
```

This reduces cross-tenant starvation and prevents returned cross-tenant rows, but it is not ideal tenant-scoped retrieval.

For serious white-label deployments, use either:

- physically separate tenant/vector namespaces, or
- a vector backend that supports metadata pre-filtering.

## Problem 3 — no dedicated reranker

The repository has hybrid/RRF retrieval but no strong semantic reranker layer.

## Problem 4 — no contradiction/conflict layer

`EvidenceBundle` acknowledges answerability states, but source contradiction resolution is not a mature subsystem yet.

### P0/P1

- [ ] Make EvidenceBundle the primary RAG contract.
- [ ] Feed evidence into `SemanticPlan.facts`.
- [ ] Never let the answer-shaping model invent unsupported business facts.
- [ ] Add a reranker interface.
- [ ] Add explicit `INSUFFICIENT_EVIDENCE` / `CONFLICTING_EVIDENCE` response behavior.
- [ ] Improve tenant-scoped vector storage.

---

# 13. Business Truth — PRESENT AS CONCEPT, NOT CENTRALIZED

The repository has authoritative sources:

```text
BusinessProfile
Calendar
RAG
CRM sink
```

but no explicit shared notion of authoritative ownership.

The current market-driven rule should be:

```text
business hours → tenant business profile
appointment existence → scheduling provider
CRM lead stage → CRM
price/policy → business profile or evidence
payment status → payment provider
```

### Recommendation

Do not necessarily create a giant `BusinessTruthService`.

Instead make authority explicit in schemas:

```python
AuthoritativeValue[T]
SourceRef
EvidenceRef
```

and make `SemanticPlan` consume these typed values.

---

# 14. Customer Model — MISSING

Database models currently include:

```text
Tenant
ApiKey
IdempotencyRow
SessionRow
TranscriptRow
BookingRow
```

There is no durable:

```text
Customer
CustomerIdentity
CustomerFact
```

This blocks:

- returning-caller memory
- callback continuity
- omnichannel continuity
- customer-level task state
- lead history
- preference memory
- duplicate-contact prevention

### Add

Suggested package:

```text
packages/customer/
    models.py
    identity.py
    memory.py
    service.py
```

Core models:

```python
Customer
CustomerIdentity
CustomerFact
CustomerPreference
```

Database tables:

```text
customers
customer_identities
customer_facts
```

### Identity examples

```text
phone:+923001234567
whatsapp:+923001234567
email:a@example.com
ghl_contact:abc123
hubspot_contact:987
```

Multiple identities can resolve to one customer.

---

# 15. Identity Resolution — MISSING

Current messaging session key:

```python
f"{channel}_{external_user_id}"
```

So:

```text
WhatsApp:+923001234567
```

and:

```text
Voice:+923001234567
```

are effectively different conversational identities.

### Add

```python
CustomerIdentityResolver
IdentityClaim
IdentityResolution
```

Resolver order might be:

```text
verified provider identity
→ normalized phone
→ CRM external ID
→ verified email
→ ambiguous / create new customer
```

This is required for true omnichannel continuity.

---

# 16. Durable BusinessTask — MAJOR MISSING SYSTEM

This should be added **above** `DialogueState`.

Use a name such as:

```python
BusinessTask
```

to avoid confusion with `packages.dialogue.state.TaskState`.

Examples:

```text
BOOK_APPOINTMENT
RESCHEDULE_APPOINTMENT
QUALIFY_LEAD
MISSED_CALL_RECOVERY
CALLBACK_REQUEST
QUOTE_FOLLOWUP
OUTBOUND_CONTACT
REACTIVATE_CUSTOMER
REFERRAL_FOLLOWUP
```

Fields:

```python
BusinessTask:
    id
    tenant_id
    customer_id
    task_type
    status
    priority
    created_at
    due_at
    owner_id
    context
    authoritative_refs
```

Statuses:

```text
OPEN
IN_PROGRESS
WAITING_CUSTOMER
WAITING_PROVIDER
WAITING_CALLBACK
WAITING_HUMAN
COMPLETED
FAILED
CANCELLED
```

Add:

```python
BusinessTaskService
TaskAttempt
TaskTransition
```

Persistence:

```text
business_tasks
task_attempts
task_transitions
```

---

# 17. Outcome Engine — MISSING GENERIC SYSTEM

There is current outbound disposition logic in:

```text
apps/api/app/core/disposition_handler.py
```

but it is:

- Vapi-specific
- real-estate-specific
- Google-Sheet-specific
- end-of-call specific
- not the central state model for all channels

Current lead statuses include a narrow set such as:

```text
HOT_LEAD
COLD_LEAD
PROPERTY_UNAVAILABLE
NO_ANSWER
CALLBACK_REQUESTED
```

## Needed

A canonical typed `BusinessOutcome`.

Suggested package:

```text
packages/outcomes/
    models.py
    engine.py
    rules.py
```

Core:

```python
OutcomeEngine
BusinessOutcome
OutcomeCode
OutcomeEvidence
```

Canonical codes:

```text
BOOKED
RESCHEDULED
CANCELLED

INTERESTED
NOT_INTERESTED
QUALIFIED
DISQUALIFIED

CALLBACK_REQUESTED
NO_ANSWER
VOICEMAIL
WRONG_CONTACT

TRANSFERRED
DNC

FAILED_TECHNICAL
FAILED_WORKFLOW
ABANDONED
UNRESOLVED
```

Persist:

```text
outcomes
```

Outcome should be created from:

- authoritative tool receipts
- conversation state
- call transport result
- policy state

The LLM may help classify fuzzy outcomes, but code should validate them.

---

# 18. NextActionPolicy — HIGHEST-VALUE NEW PRODUCT SYSTEM

There is no general NextAction system in the repository.

This is the clearest new system demanded by the latest market research.

## Suggested package

```text
packages/orchestration/
    next_action.py
    scheduler.py
    priority.py
```

## Classes

```python
NextActionPolicy
NextActionContext
NextActionCandidate
NextActionDecision

LeadPriorityPolicy
CallbackPriorityPolicy
ChannelSelectionPolicy
ContactTimingPolicy
EscalationPolicy
CostPolicy
```

Decision:

```python
NextActionDecision:
    action_type
    channel
    execute_at
    priority
    reason_codes
    business_task_id
```

Actions:

```text
PLACE_CALL
SEND_SMS
SEND_WHATSAPP
SEND_EMAIL
WAIT
SCHEDULE_CALLBACK
CREATE_HUMAN_TASK
TRANSFER
STOP_CONTACT
COMPLETE_TASK
```

## Critical deterministic invariant

Promised callbacks must beat generic campaign activity.

Example ranking:

```text
EMERGENCY
>
PROMISED_CALLBACK
>
ACTIVE_HIGH_VALUE_LEAD
>
NEW_SPEED_TO_LEAD
>
MISSED_CALL_RECOVERY
>
RESCHEDULE
>
NORMAL_FOLLOWUP
>
NURTURE
```

This should be code, not an LLM suggestion.

---

# 19. Action Scheduler — MISSING

A callback time is currently extracted in outbound work, but there is no durable generalized scheduler that actually guarantees the follow-up.

### Add

```python
ActionScheduler
ScheduledAction
ActionClaim
ActionExecutionResult
```

Database:

```text
scheduled_actions
```

Requirements:

- due-time indexes
- idempotent claims
- multi-worker safe
- lease/lock
- retry
- cancellation
- DNC re-check before execution
- business/calling-hours re-check before execution

This unlocks:

- promised callbacks
- speed-to-lead
- missed-call recovery
- quote follow-up
- no-response escalation
- appointment reminders

---

# 20. Outbox — MISSING DURABLE SIDE-EFFECT LAYER

Current confirmation behavior in:

```text
calendar_commit_adapter.py
```

uses fire-and-forget:

```python
asyncio.create_task(send_sms(...))
asyncio.create_task(send_confirmation_email(...))
```

This is fine for a demo but not for production truth.

If the process dies after booking but before messaging, the confirmation is lost.

### Add

```text
packages/platform/outbox.py
```

Classes:

```python
OutboxEvent
OutboxService
OutboxRepository
OutboxWorker
DeliveryReceipt
```

Database:

```text
outbox_events
delivery_receipts
```

Flow:

```text
booking committed
      ↓
save authoritative booking outcome
      ↓
save outbox events in same DB transaction
      ↓
commit
      ↓
worker sends:
    SMS
    WhatsApp
    CRM update
    Slack alert
```

---

# 21. Retry Engine — PARTIAL / NEEDS NORMALIZATION

Provider-specific retry/fallback behavior exists in places, especially LLM routing.

But business integrations do not share a common error model.

### Add common error classes

```python
IntegrationError
TransientIntegrationError
RateLimitError
AuthenticationError
ValidationError
ConflictError
PermanentIntegrationError
```

And:

```python
RetryPolicy
RetryDecision
```

Rules differ between:

```text
safe reads
idempotent writes
non-idempotent writes
```

Never blindly retry a mutation.

---

# 22. Reconciliation Engine — MISSING AND COMMERCIALLY IMPORTANT

Current sinks are mostly best-effort.

Example risk:

```text
Google Calendar booking succeeds
GHL mirror write fails
```

Now calendar and CRM disagree.

This was explicitly identified by current buyers in the research.

### Add

```text
packages/platform/reconciliation.py
```

Classes:

```python
ReconciliationService
ConsistencyRule
ReconciliationIssue
ReconciliationResult
```

Examples:

```text
booking exists in calendar but not CRM
CRM says DNC but campaign is scheduled
customer phone differs across sources
outbox says delivered but provider says failed
```

---

# 23. GoHighLevel — PRESENT BUT TOO NARROW FOR CURRENT MARKET

Existing:

```text
packages/integrations/ghl_client.py
packages/integrations/sinks.py
```

Current client supports:

```text
upsert_contact
add_note
create_opportunity
list_free_slots
book_appointment
```

This is a useful foundation.

## Missing for the jobs repeatedly appearing in research

```text
lookup contact
update contact fields
custom fields
tags lifecycle
find/update opportunity
pipeline stage transition
create task
assign owner
appointment lookup
appointment update/cancel
DNC state
consent state
workflow trigger
webhook ingestion
two-way reconciliation
```

## Architectural problem

GHL currently behaves mainly as a sink after business activity.

The market increasingly requires GHL to be:

```text
source
+
destination
+
campaign state
+
task state
```

### Build

A normalized:

```python
CRMAdapter
GoHighLevelCRMAdapter
```

with typed methods.

Reuse `GoHighLevelClient` internally.

---

# 24. GHL HTTP Performance / Reliability

`GoHighLevelClient._request()` creates a new:

```python
httpx.AsyncClient(...)
```

for every request.

That sacrifices connection pooling.

The timeout default is also too large for a live voice turn if the request occurs synchronously in the caller's critical path.

### Improve

- reuse a long-lived async client
- separate connect/read/write/pool timeouts
- assign a voice-interaction latency budget
- use circuit breaker
- prefer asynchronous post-call mutations where immediate truth is not required
- persist authoritative receipt

---

# 25. CRM Sink — NEEDS TO BECOME EVENT/OUTBOX DRIVEN

Existing:

```text
CRMSink
MultiSink
GHLSink
SheetsSink
```

These are useful, but the model is essentially:

```text
on_booking()
on_call_end()
```

The market now needs richer event semantics:

```text
LeadQualified
AppointmentBooked
CallbackRequested
DoNotCall
MissedCall
TransferCompleted
TechnicalFailure
```

### Recommendation

Keep current sink compatibility, but introduce normalized domain events.

Then sinks subscribe to:

```text
BusinessOutcome / OutboxEvent
```

rather than only whole-call lifecycle hooks.

---

# 26. WhatsApp — TRANSPORT EXISTS, OMNICHANNEL STATE DOES NOT

Existing:

```text
packages/channels/whatsapp.py
packages/channels/base.py
apps/api/app/routes/channels.py
```

Good:

- normalized `IncomingMessage`
- voice/text support
- signature verification
- common brain pipeline

But:

```python
tenant_id = "default"
```

is hardcoded in the channel bridge.

And session identity is:

```text
channel + external_user_id
```

So WhatsApp and voice do not converge on one persistent customer/task identity.

### Required

```text
provider identity
→ tenant resolver
→ customer identity resolver
→ active BusinessTask
→ channel session
```

This is the difference between:

> We support WhatsApp.

and:

> The customer can continue the same booking on WhatsApp after the phone call.

The second one is commercially meaningful.

---

# 27. SMS — OUTBOUND CONFIRMATION EXISTS, INBOUND CHANNEL DOES NOT

Existing:

```text
packages/integrations/sms_sender.py
```

SMS is currently used for booking confirmation.

Missing:

- inbound Twilio SMS webhook
- shared `IncomingMessage` adaptation
- customer resolution
- task continuation
- STOP/START compliance state
- delivery receipts/status callbacks
- missed-call recovery
- speed-to-lead conversational SMS

### Build

```python
TwilioSMSChannel(Channel)
```

using the existing channel abstraction.

This is a particularly high-leverage addition because much of the channel pipeline already exists.

---

# 28. Email — CONFIRMATION EXISTS, CUSTOMER ORCHESTRATION DOES NOT

Existing:

```text
packages/integrations/email_sender.py
```

This is a confirmation sender.

It is not an email conversation/lead channel.

Later build:

```python
EmailChannel
MicrosoftGraphEmailAdapter
GmailEmailAdapter
```

after SMS/WhatsApp.

Research suggests email is useful as part of the broader lead orchestration layer, but it does not need to displace current priorities.

---

# 29. n8n — WORKFLOW EXAMPLES EXIST, PRODUCT ADAPTER DOES NOT

The repository contains:

```text
workflows/n8n/
```

including:

- post-call router
- outbound workflow
- assistant prompt

These are good portfolio/reference assets.

But there is no normalized application-level:

```python
N8nAdapter
WorkflowEvent
WebhookDelivery
```

### Add

Generic outbound event endpoint:

```text
VoiceOps
→ signed webhook
→ n8n
```

Generic inbound action endpoint:

```text
n8n
→ authenticated event/action
→ VoiceOps BusinessTask/NextAction
```

Include:

- event ID
- idempotency
- signature
- timestamp
- retry behavior
- versioned schema

---

# 30. Slack — MISSING, EASY COMMERCIAL WIN

Not found in current runtime.

Add after OutcomeEngine/outbox.

Use for:

```text
HOT_LEAD
CALLBACK_REQUESTED
TRANSFER_REQUEST
UNRESOLVED_CALL
TOOL_FAILURE
RAG_LOW_CONFIDENCE
SYSTEM_FAILURE
```

Suggested:

```python
NotificationAdapter
SlackNotificationAdapter
```

This is small engineering work and repeatedly useful in SMB/agency jobs.

---

# 31. HubSpot — MISSING

No native HubSpot adapter was found.

Do after GHL is normalized.

Suggested capabilities:

```text
lookup/create/update contact
companies
deals
deal stages
notes
tasks
owners
associations
custom properties
webhooks
```

Do not implement until the generic `CRMAdapter` contract is stable.

---

# 32. Microsoft 365 — MISSING

No native:

```text
Microsoft Calendar
Microsoft Graph Email
Teams
```

integration was found.

This should be Tier 2 because it opens:

- professional services
- healthcare/admin
- enterprise-ish workflows
- email lead orchestration

---

# 33. Multi-Tenancy — DATABASE STRONG, RUNTIME WEAK

The repo has meaningful tenant protection in persistence:

```text
Tenant
tenant_id
contextvar tenant guard
cross-tenant session protection
```

That is excellent.

However, runtime business state is still process-global:

```python
_business_cache
_calendar_cache
_sink_cache
_retriever_cache
```

`load_business()` reads one:

```text
settings.business_profile_path
```

for the process.

Provider credentials and integration configuration are also primarily environment-global.

### Therefore

Current state is:

```text
database tenancy: strong
operational deployment tenancy: incomplete
```

This must change before white-label/multi-client deployment.

---

# 34. Tenant Runtime Configuration — ADD

Create:

```text
packages/tenancy/
    config.py
    resolver.py
    secrets.py
```

Suggested:

```python
TenantRuntimeConfig
TenantBusinessConfig
TenantVoiceConfig
TenantIntegrationConfig
TenantPolicyConfig

TenantConfigRepository
TenantSecretResolver
```

Tenant config should choose:

```text
business profile
KB namespace
calendar
CRM
phone numbers
messaging accounts
voice
STT
LLM profile
business hours
timezone
language profile
transfer targets
policy profile
```

Do not load one global business JSON for all tenants.

---

# 35. DNIS / Multi-Number Routing — DATA EXISTS, RESOLUTION DOES NOT

Twilio actor state already knows:

```text
caller_number
dialed_number
```

That is the raw material needed for DNIS routing.

Missing:

```python
InboundRouteResolver
DialedNumberRoute
```

Suggested mapping:

```text
dialed number
→ tenant
→ brand
→ location
→ campaign
→ business config
→ KB
→ calendar
→ CRM account
```

Persist:

```text
inbound_routes
```

This is important for:

- franchises
- agencies
- multi-location practices
- marketing attribution
- multiple websites

---

# 36. Outbound Engine — PROTOTYPE EXISTS, NOT YET COMMERCIAL-GRADE

Existing:

```text
apps/api/app/routes/outbound.py
packages/integrations/dialer_policy.py
apps/api/app/core/outbound_registry.py
apps/api/app/core/disposition_handler.py
```

Useful existing features:

- batch leads
- business hours
- cooldown
- max attempts
- DNC concept
- consent provider functions
- Vapi/local transport
- end-of-call disposition
- Google Sheets writeback

## Important safety finding already documented by repo

Outbound endpoints are disabled by default.

The route comments explicitly list blockers including:

- consent not guaranteed
- production route bypasses consent-aware policy
- DNC is client-controlled
- caller-controlled external IDs/sheets
- caller-ID override

The audit confirms the active route calls:

```python
decide_can_call(...)
```

rather than:

```python
decide_can_call_with_consent(...)
```

### Do not simply turn outbound on.

Rebuild it on top of:

```text
Customer
BusinessTask
ConsentState
NextActionPolicy
ActionScheduler
OutcomeEngine
```

---

# 37. Outbound Registry — MUST BECOME DURABLE

Current:

```text
apps/api/app/core/outbound_registry.py
```

is explicitly in-memory.

If process restarts between dial and end-of-call webhook:

```text
context is lost
→ disposition cannot be correlated
```

Horizontal workers cause the same problem.

### Replace with persistence

Suggested:

```text
contact_attempts
provider_call_correlations
```

with:

```python
ContactAttempt
ProviderCallRef
```

---

# 38. Speed-to-Lead — MISSING AS A PRODUCT FLOW

No generalized speed-to-lead engine was found.

Required flow:

```text
CRM/form lead arrives
→ validate consent
→ create BusinessTask
→ NextActionPolicy
→ call/SMS within policy window
→ outcome
→ CRM
→ next action
```

Metric:

```text
lead_created_to_first_attempt_ms
lead_created_to_contact_ms
lead_created_to_booking_ms
```

This is a major Upwork-ready capability.

---

# 39. Missed-Call Recovery — MISSING

No generalized missed-call recovery flow was found.

Build:

```text
missed inbound call
→ identify tenant/customer
→ create MISSED_CALL_RECOVERY task
→ NextActionPolicy
→ SMS/WhatsApp
→ optional callback
```

This is one of the easiest revenue-oriented features to explain to SMB clients.

---

# 40. Callback Continuity — MISSING

Current outbound disposition can identify callback intent/time, but there is no durable callback scheduler + inbound continuity.

Needed:

```text
outbound lead
→ requests callback tomorrow 3 PM
→ ScheduledAction saved

customer calls back first
→ ANI identifies customer
→ active BusinessTask restored
→ pending outbound task continues
```

This is a high-value differentiation.

---

# 41. Compliance — GOOD PRIMITIVES, INCOMPLETE POLICY WIRING

Existing:

```text
packages/compliance/tcpa.py
packages/compliance/pii.py
```

Good foundation:

- consent provider abstraction
- TCPA-oriented dial policy
- PII redaction

Business profile also has disclosure-related fields.

## Missing central state

Need durable:

```text
ConsentRecord
DNCRecord
ChannelOptOut
DisclosureRecord
```

and a central:

```python
CompliancePolicyEngine
```

The engine should evaluate before every scheduled contact.

Important invariants:

```text
DNC → stop voice + SMS
SMS STOP → persist opt-out
calling window → checked at execution time
consent → channel/purpose scoped
recording disclosure → traceable
```

---

# 42. Critical SMS Compliance Gap

Outgoing SMS can include opt-out language.

But there is no inbound SMS channel that reliably interprets:

```text
STOP
UNSUBSCRIBE
CANCEL
START
```

and persists policy state.

This must exist before marketing automated outbound SMS at scale.

---

# 43. Human Transfer — PARTIAL STATE, NO REAL TRANSFER SYSTEM

`CallActor` has a:

```text
TRANSFERRING
```

state.

Some vertical tools can request escalation/callback.

But no general:

```python
TransferManager
TransferRequest
TransferResult
TransferBrief
HumanQueue
```

was found.

### Build later P2/P3

Support:

```text
cold transfer
warm transfer
transfer failure
callback fallback
```

Warm-transfer brief:

```text
caller
intent
fields collected
urgency
RAG evidence
tool results
recommended next action
```

This repeatedly appears in commercial jobs.

---

# 44. Persistent Customer Memory — MISSING

There is call/session/transcript persistence, but no customer-centric durable memory layer.

Add:

```python
CustomerMemoryService
CustomerFact
CustomerPreference
```

Every fact should carry:

```text
source
confidence
created_at
expires_at
verification status
```

Do not create memory by dumping old transcripts back into the LLM.

---

# 45. Cross-Channel Continuity — MISSING

Transport support exists.

Shared customer/task continuity does not.

Target:

```text
Customer
  ↓
BusinessTask
  ↓
Voice | SMS | WhatsApp | Email
```

This is a major future market baseline and should influence schema decisions now.

---

# 46. Automatic Call Evaluation — PARTIAL FOUNDATION, MISSING GENERAL PIPELINE

The repository has:

- many unit/integration tests
- adversarial scenarios/reports
- observability
- failure intelligence
- call transcript generation
- benchmark manifest

But:

```text
packages/evals/
```

does not contain a general production-call evaluation engine.

### Add

```text
packages/evals/call_evaluator.py
packages/evals/evaluators/
```

Core:

```python
CallEvaluationPipeline
EvaluationResult
EvaluationFinding
```

Evaluators:

```text
GroundingEvaluator
CommitmentConsistencyEvaluator
ToolResultConsistencyEvaluator
TaskCompletionEvaluator
RepetitionEvaluator
DeadAirEvaluator
InterruptionEvaluator
OutcomeConsistencyEvaluator
PolicyComplianceEvaluator
```

This directly supports the **Voice Agent Rescue Audit** product.

---

# 47. Failure → Regression Automation — PARTIAL MANUAL PROCESS

The team already does this manually through working notes and adversarial tests.

Formalize it.

Add:

```python
FailureFixtureBuilder
ReplayEngine
RegressionCase
RegressionSuite
```

Pipeline:

```text
production failure
→ classify
→ capture sanitized call artifacts
→ generate replay fixture
→ expected invariant
→ permanent regression
```

This becomes both internal engineering infrastructure and a consulting deliverable.

---

# 48. Observability — STRONG

Existing:

```text
packages/observability/
packages/runtime/telemetry.py
scripts/build_call_transcript.py
```

This is a strong part of the repository.

It already tracks important latency components.

This should be preserved and made more visible in the demo.

### Do not spend weeks on a giant dashboard.

Expose:

```text
call timeline
p50/p95
STT
LLM
tool
TTS
interruptions
failures
outcome
cost
```

in a simple operator view/report first.

---

# 49. Cost Telemetry — EXISTS, NEEDS BUSINESS-OUTCOME JOIN

Existing:

```text
packages/observability/cost.py
```

This is good.

The missing step is persistence/aggregation at:

```text
call
customer task
business outcome
tenant
```

Add:

```python
UsageRecord
CallCost
TaskCost
```

Metrics:

```text
$/minute
$/call
$/qualified lead
$/booking
$/completed task
```

The market is starting to care about managed-platform per-minute cost, so this can become a sales differentiator.

Also version provider rate cards instead of assuming hardcoded prices remain current forever.

---

# 50. Provider Abstraction — STRONG, DO NOT REBUILD

The repo already has broad provider abstraction/factories for:

- STT
- LLM
- TTS
- telephony paths

There is also LLM routing/fallback.

### Do not build another ProviderRegistry now.

Later, add a cross-provider scoring/router only when enough benchmark data exists.

P3/P4:

```python
ProviderScore
ProviderHealth
ProviderSelectionPolicy
```

---

# 51. Circuit Breakers — PARTIAL

LLM routing has fallback/cooldown concepts.

External business integrations do not share a generic circuit breaker.

Later normalize:

```python
IntegrationHealth
CircuitBreaker
ProviderHealthMonitor
```

This matters most for:

```text
calendar
CRM
messaging
```

where waiting 10–30 seconds on a dead provider destroys the voice experience.

---

# 52. Multilingual — PROVIDER FOUNDATION EXISTS, PRODUCT PROFILE DOES NOT

The code has multilingual provider capability concepts and can use multilingual STT/LLM/TTS providers.

What is missing is a coordinated tenant/channel language profile.

Add later:

```python
LanguageProfile
LanguageResolver
```

Profile selects:

```text
STT config
endpointing
LLM language
RAG namespace/translation policy
TTS voice
structured-input normalization
```

This is P3, not P0.

---

# 53. SIP — NOT IMPLEMENTED

Telephony architecture leaves room for SIP/LiveKit, but the actual SIP product capability is not present.

This remains useful for higher-end clients with existing phone systems.

P4 unless a real contract pulls it forward.

---

# 54. Home Services Vertical — NOT YET BUILT

No native:

```text
ServiceM8
Jobber
ServiceTitan
Housecall Pro
```

integration was found.

No complete:

```text
AddressParser
Geocoder
ServiceAreaPolicy
```

exists.

This is correct for now.

Finish dental/receptionist commercial infrastructure first.

Then build home services as the second vertical.

---

# 55. Market Capability Scorecard

| Capability | Current State | Audit |
|---|---:|---|
| Temporal call runtime | Strong | Keep |
| Barge-in / interruption | Strong but active bugs | P0 stabilize |
| Streaming latency | Advanced but inconsistent | P0 |
| Dialogue state | Strong | Keep |
| Semantic plan | Schema only / not live | **Major P0/P1** |
| Commit coordination | Strong primitive | Extend |
| Speech commit gate | Strong | Keep |
| Real calendar transaction safety | Partial | **Major gap** |
| Structured input | Phone strong | Extend |
| RAG retrieval | Good | Improve |
| Evidence-backed RAG | Scaffolded/off main path | Enable |
| Customer model | Missing | **P1** |
| Identity resolution | Missing | **P1** |
| Customer memory | Missing | **P1/P2** |
| Durable business tasks | Missing | **P1** |
| Outcome Engine | Missing generic engine | **P1** |
| NextActionPolicy | Missing | **P1** |
| Durable scheduler | Missing | **P1** |
| Outbox | Missing | **P1** |
| Reconciliation | Missing | **P1/P2** |
| Google Calendar | Partial | Complete |
| GHL | Useful partial | Expand |
| SMS outbound | Present | Expand |
| SMS inbound | Missing | Build |
| WhatsApp transport | Present | Add shared state |
| n8n | Workflow examples | Build adapter |
| Slack | Missing | Build |
| HubSpot | Missing | P2 |
| Microsoft 365 | Missing | P2 |
| Outbound campaigns | Prototype/safety-disabled | Rebuild on new state |
| Speed-to-lead | Missing | P2 |
| Missed-call recovery | Missing | P2 |
| Callback continuity | Missing | P2 |
| Compliance primitives | Partial | Centralize |
| DB multi-tenancy | Strong | Keep |
| Runtime multi-tenancy | Weak | **P1** |
| DNIS routing | Missing | **P1** |
| Warm transfer | Missing | P2/P3 |
| Cost telemetry | Good primitive | Join to outcomes |
| Automatic call QA | Partial/manual | Build |
| Failure replay | Partial/manual | Formalize |
| Concurrency / horizontal scale | Partial | P2/P3 |
| Multilingual profiles | Missing | P3 |
| SIP | Missing | P4 |
| Home-services adapters | Missing | P3 |

---

# 56. Exact New Systems / Classes to Add

The goal is the **minimum clean set**, not 100 empty services.

## 56.1 Customer domain

```text
packages/customer/
```

Add:

```python
Customer
CustomerIdentity
CustomerFact

CustomerService
CustomerIdentityResolver
CustomerMemoryService
```

---

## 56.2 Durable business-task domain

```text
packages/business_tasks/
```

Add:

```python
BusinessTask
BusinessTaskType
BusinessTaskStatus
TaskAttempt
TaskTransition

BusinessTaskService
```

Do not reuse the name `TaskState`; dialogue already owns that name.

---

## 56.3 Outcomes

```text
packages/outcomes/
```

Add:

```python
OutcomeCode
BusinessOutcome
OutcomeEvidence

OutcomeEngine
OutcomeValidator
```

---

## 56.4 Orchestration / next action

```text
packages/orchestration/
```

Add:

```python
NextActionType
NextActionChannel
NextActionContext
NextActionCandidate
NextActionDecision

NextActionPolicy
PriorityPolicy
ChannelSelectionPolicy
ContactTimingPolicy

ScheduledAction
ActionScheduler
ActionExecutor
```

---

## 56.5 Platform reliability

```text
packages/platform/
```

Add:

```python
OutboxEvent
OutboxService
OutboxWorker

DeliveryReceipt

RetryPolicy
RetryDecision

ReconciliationIssue
ReconciliationService
```

---

## 56.6 Runtime tenant routing

```text
packages/tenancy/
```

Add:

```python
TenantRuntimeConfig
TenantIntegrationConfig
TenantVoiceConfig
TenantPolicyConfig

TenantConfigRepository
TenantSecretResolver

InboundRoute
InboundRouteResolver
```

---

## 56.7 Integration protocols

The current integrations are mostly concrete classes.

Introduce only the protocols needed for normalization:

```python
CalendarAdapter
CRMAdapter
MessagingAdapter
WorkflowAdapter
NotificationAdapter
```

Do not create generic abstractions for everything.

---

## 56.8 Evaluation

Extend:

```text
packages/evals/
```

Add:

```python
CallEvaluationPipeline
CallEvaluation
EvaluationFinding

GroundingEvaluator
CommitmentEvaluator
ToolConsistencyEvaluator
TaskCompletionEvaluator
RepetitionEvaluator
DeadAirEvaluator
InterruptionEvaluator
OutcomeConsistencyEvaluator

FailureFixtureBuilder
ReplayEngine
```

---

## 56.9 Transfer

Later:

```text
packages/transfers/
```

Add:

```python
TransferManager
TransferRequest
TransferResult
TransferBrief
```

---

# 57. Database Tables to Add

Recommended schema additions:

```text
customers
customer_identities
customer_facts

business_tasks
task_attempts
task_transitions

business_outcomes

contact_attempts
scheduled_actions

outbox_events
delivery_receipts

inbound_routes

tenant_runtime_configs
tenant_integration_configs
```

Potentially:

```text
reconciliation_issues
evaluation_results
```

Do not store raw API secrets directly unless properly encrypted/managed; preferably store secret references.

---

# 58. What Existing Classes Should Be Extended Instead of Duplicated

## Keep / extend

```text
CallActor
TurnManager
SpeechCommitGate
DialogueState
ConversationAgenda
TaskState
CommitCoordinator
CommitResult
StructuredInputSession
EvidenceBundle
RouterLLM / provider factories
telemetry
cost estimator
Channel abstraction
```

## Evolve

### `CommitResult`

Rather than creating an unrelated `ActionReceipt`, evolve the existing type to include normalized receipt fields:

```text
provider
operation
external_id
committed_at
idempotency_key
committed_values
provider_metadata
```

Then it effectively **is** the ActionReceipt concept.

### `Channel`

Extend for:

```text
SMS
email later
```

### `CRMSink`

Keep as compatibility adapter, but migrate important writes onto domain event/outbox handling.

---

# 59. What We Should NOT Add

Avoid:

```text
New ConversationOrchestrator
New DialogueState
New TurnManager
New SpeechManager hierarchy
New generic ProviderRegistry
New generic STT abstraction
New generic TTS abstraction
New phone-input framework
New giant BusinessTruth singleton
```

The repo already has enough machinery in these areas.

The main risk now is **duplicate abstractions**, not lack of abstraction.

---

# 60. Target Architecture for This Specific Repository

The clean target is:

```text
                     Tenant Route / DNIS
                             │
                             ▼
                     Customer Resolver
                             │
                             ▼
                       BusinessTask
                             │
                             ▼
                     NextActionPolicy
                   ┌─────────┼─────────┐
                   │         │         │
                 Voice      SMS     WhatsApp
                   │         │         │
                   └─────────┼─────────┘
                             ▼
                         CallActor
                             │
                             ▼
                       DialogueState
                             │
                             ▼
                        SemanticPlan
                             │
                             ▼
                      Policy / Confirm
                             │
                             ▼
                     CommitCoordinator
                             │
                             ▼
                      Calendar / CRM
                             │
                             ▼
                        CommitResult
                             │
                             ▼
                      SpeechCommitGate
                             │
                             ▼
                       BusinessOutcome
                             │
                             ▼
                     NextActionPolicy
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
              CRM         Follow-up      Human
                           Scheduler
```

The existing runtime remains the center of the **call**.

The new business-state layer becomes the center of the **customer relationship/task**.

---

# 61. P0 — Finish the Voice Runtime Before Product Expansion

## P0.1 Turn execution ownership

- [ ] Verify T4a across a larger sample.
- [ ] Eliminate same-generation multi-fire.
- [ ] Add `utterance_id` / `response_attempt_id` if necessary.
- [ ] Guarantee exactly one authoritative response attempt owns playout.
- [ ] Repair streaming test-harness drift.
- [ ] Run difficult interruption scenarios.

## P0.2 Plan-first semantic architecture

- [ ] Implement `SemanticPlanGenerator`.
- [ ] Stop using post-hoc semantic classification as the primary semantic planner.
- [ ] Implement `SemanticRealizer`.
- [ ] Deterministically realize critical facts.
- [ ] Feed pending multi-intent tasks from `DialogueState`.

## P0.3 Calendar transaction correctness

- [ ] Define `CalendarAdapter`.
- [ ] Wrap Google Calendar under commit protocol.
- [ ] Add persistent idempotency.
- [ ] Add find/cancel/reschedule.
- [ ] Put cancel/reschedule behind CommitCoordinator.
- [ ] Normalize timezone handling.
- [ ] Use business hours from tenant config.
- [ ] Add concurrent double-booking tests.

## P0.4 RAG correctness

- [ ] Enable `EvidenceBundle`.
- [ ] Feed evidence directly into semantic plan facts.
- [ ] Add explicit abstention.
- [ ] Add reranker interface.
- [ ] Improve tenant-scoped vector retrieval.

---

# 62. P1 — Build the Commercial Business-State Layer

## Customer

- [ ] `Customer`
- [ ] `CustomerIdentity`
- [ ] identity resolution
- [ ] durable customer facts
- [ ] CRM external identities

## Business tasks

- [ ] `BusinessTask`
- [ ] `TaskAttempt`
- [ ] task transitions
- [ ] task persistence

## Outcomes

- [ ] canonical `OutcomeCode`
- [ ] `OutcomeEngine`
- [ ] persisted outcomes
- [ ] outcome → task transition

## Next action

- [ ] `NextActionPolicy`
- [ ] priority rules
- [ ] channel selection
- [ ] timing policy
- [ ] callback precedence

## Scheduler

- [ ] durable scheduled actions
- [ ] safe worker claims
- [ ] policy re-check at execution

## Reliability

- [ ] outbox
- [ ] delivery receipts
- [ ] common integration errors
- [ ] retries
- [ ] reconciliation

## Tenancy

- [ ] tenant runtime config
- [ ] per-tenant business profile
- [ ] per-tenant calendar/CRM/messaging config
- [ ] DNIS resolver
- [ ] remove hardcoded messaging `default` tenant

---

# 63. P2 — Commercial Integration Pack

## Google Calendar

- [ ] complete authoritative lifecycle

## GoHighLevel

- [ ] normalized CRM adapter
- [ ] lookup/upsert
- [ ] opportunity update/stages
- [ ] tasks
- [ ] owner assignment
- [ ] custom fields
- [ ] DNC
- [ ] webhooks
- [ ] two-way reconciliation

## SMS

- [ ] inbound Twilio SMS channel
- [ ] status callbacks
- [ ] STOP/START policy
- [ ] shared customer/task state

## WhatsApp

- [ ] tenant resolver
- [ ] customer resolver
- [ ] persistent task continuation
- [ ] delivery receipts / idempotent webhook handling

## n8n

- [ ] normalized event adapter
- [ ] signed webhook
- [ ] idempotent event IDs
- [ ] reusable workflow pack

## Slack

- [ ] hot lead
- [ ] callback
- [ ] technical failure
- [ ] transfer briefing

---

# 64. P2 — Revenue Flows

After P1 foundations:

## Missed-call recovery

- [ ] detect failed/unanswered inbound
- [ ] create recovery task
- [ ] immediate SMS/WhatsApp
- [ ] optional AI callback

## Speed-to-lead

- [ ] inbound lead webhook
- [ ] consent validation
- [ ] task creation
- [ ] immediate contact policy
- [ ] outcome
- [ ] CRM update

## Callback continuity

- [ ] promised callback scheduling
- [ ] callback priority
- [ ] inbound callback resumes task

---

# 65. P2 — Evaluation Product

- [ ] automatic post-call evaluation
- [ ] call-quality score
- [ ] commitment correctness
- [ ] tool/result consistency
- [ ] hallucination/grounding checks
- [ ] repeated-question detection
- [ ] dead-air detection
- [ ] outcome anomaly detection
- [ ] failure → regression fixture

This becomes the infrastructure for the marketable:

# Voice Agent Reliability / Rescue Audit

---

# 66. P3 — Second Integration / Enterprise Layer

Add based on contract demand:

```text
HubSpot
Microsoft Calendar
Microsoft Graph Email
Teams
Make
```

Then:

```text
warm transfer
human assist queue
multilingual LanguageProfile
advanced provider routing
```

---

# 67. P3 — Home Services Template

After dental demo is polished:

Add:

```text
AddressParser
Geocoder
ServiceAreaPolicy
QuotePolicy
```

First native system based on demand:

```text
ServiceM8
or Jobber
```

Then:

```text
ServiceTitan / Housecall Pro
```

as contract pull demands.

---

# 68. P4 — Higher-End Capabilities

- [ ] SIP
- [ ] human answering-service mode
- [ ] healthcare workflow adapters
- [ ] compliance evidence/export
- [ ] adaptive provider router
- [ ] deeper enterprise RBAC
- [ ] multi-region deployment if needed

---

# 69. The First Killer Dental Demo — What the Current Repo Still Needs

The repo is close enough that this should remain the first polished commercial proof.

Demo flow:

```text
real phone call
→ natural interruption
→ clinic FAQ
→ evidence-backed RAG
→ caller asks for appointment
→ exact date/time captured
→ real Google Calendar availability
→ booking committed through CommitCoordinator
→ authoritative receipt
→ SMS/WhatsApp confirmation through outbox
→ GHL contact/opportunity update
→ BusinessOutcome=BOOKED
→ task completed
→ cost/latency/evaluation report
```

## Deliberate failure

Disable/break calendar API.

Agent must:

- not claim the booking succeeded
- explain that it cannot verify the schedule
- record `FAILED_TECHNICAL`
- create follow-up/human task
- optionally send recovery message

That demonstrates the exact reliability buyers are asking for.

---

# 70. Voice Agent Rescue Demo

Use the existing call timeline / failure tooling.

Show:

```text
BEFORE
3.2s latency
duplicate speech
false tool confirmation

DIAGNOSIS
timeline
ownership bug
provider/tool span

AFTER
1.1s latency
single speech owner
receipt-gated confirmation

REGRESSION
same scenario replayed automatically
```

This is an unusually strong freelance portfolio asset because the repo already contains much of the required diagnostic foundation.

---

# 71. Upwork Capabilities the Repo Can Already Credibly Market

Even before every future system is complete, the code demonstrates real work around:

```text
AI voice agent engineering
Twilio real-time voice
Vapi integration
Deepgram streaming STT
ElevenLabs / multiple TTS
multi-provider LLM routing
barge-in / interruption engineering
voice latency optimization
RAG voice agents
appointment booking
GoHighLevel integration
Google Calendar
WhatsApp voice/text
n8n workflows
TCPA/PII-aware architecture
production call diagnostics
```

But be careful not to market features as production-complete where the audit identified gaps, especially:

```text
full Google Calendar reschedule/cancel safety
true multi-tenant runtime
full omnichannel continuity
commercial outbound campaign engine
warm transfer
HubSpot/Microsoft integrations
```

---

# 72. Upwork Capabilities Unlocked by Each Phase

## After P0

Sell:

```text
Voice Agent Reliability Audit
Retell/Vapi/Twilio optimization
latency/barge-in troubleshooting
safe appointment-agent engineering
RAG correctness
```

## After P1

Sell:

```text
stateful multi-call voice systems
customer memory
callback continuity foundations
production workflow orchestration
white-label runtime architecture
```

## After P2

Sell:

```text
GHL + voice + n8n
missed-call recovery
speed-to-lead
SMS/WhatsApp continuity
dental front desk
outbound lead engine
```

## After P3

Sell:

```text
HubSpot/Microsoft enterprise-ish integrations
home services
human assist
multilingual deployments
```

---

# 73. Highest-Risk Technical Debt Relative to Market Demand

These deserve attention because they could undermine a paid deployment.

## 1. Parallel runtime paths

Feature flags currently allow multiple combinations:

```text
actor on/off
two-planner on/off
dialogue kernel on/off
streaming STT on/off
turn manager on/off
```

This helps migration, but too many parallel behaviors create test complexity.

### Goal

Converge on one canonical production path after current stabilization.

---

## 2. In-memory state

Current examples:

```text
session manager dictionaries
outbound registry
per-call commit result cache
```

These limit multi-worker scale and restart resilience.

Durable business state should not depend on one Python process.

---

## 3. Fire-and-forget integrations

SMS/email and CRM sinks can fail after the authoritative operation.

Move them into outbox/reconciliation.

---

## 4. Synchronous provider SDK work in live paths

Google Calendar's SDK methods are synchronous.

Ensure they do not block the voice event loop.

Use:

- async wrapper / thread offload
- strict timeout
- bounded provider latency

---

## 5. Prompt patches for business truth

Prompts are useful, but exact transactional facts must move into structured plans/realizers.

---

# 74. Suggested Canonical Event Flow

## Inbound

```text
Twilio call
→ DNIS route
→ tenant config
→ ANI customer resolution
→ active BusinessTask lookup
→ CallActor
→ DialogueState
→ SemanticPlan
→ tool/commit
→ CommitResult
→ SpeechCommitGate
→ OutcomeEngine
→ NextActionPolicy
→ scheduler/outbox
```

## Outbound

```text
lead webhook
→ Customer / BusinessTask
→ consent
→ NextActionPolicy
→ ActionScheduler
→ place call
→ CallActor
→ OutcomeEngine
→ CRM/outbox
→ NextActionPolicy
```

## Messaging

```text
SMS/WhatsApp
→ tenant resolver
→ identity resolver
→ Customer
→ BusinessTask
→ same dialogue/business tools
→ OutcomeEngine
→ NextActionPolicy
```

---

# 75. Definition of Done — Calendar Mutation

A calendar mutation is not done until:

- [ ] timezone is explicit
- [ ] request schema is typed
- [ ] caller confirmation is captured when required
- [ ] idempotency key is durable
- [ ] provider mutation returns normalized receipt
- [ ] duplicate request returns same success
- [ ] tool failure cannot produce success speech
- [ ] concurrent conflict test exists
- [ ] reconciliation strategy exists
- [ ] call timeline records provider latency/outcome
- [ ] cancellation/reschedule use the same safety model

---

# 76. Definition of Done — CRM Mutation

- [ ] tenant-scoped credentials
- [ ] customer identity mapping
- [ ] idempotency
- [ ] typed operation
- [ ] timeout
- [ ] retry classification
- [ ] provider receipt
- [ ] outbox/retry where eventual consistency is acceptable
- [ ] reconciliation
- [ ] webhook deduplication
- [ ] DNC/consent changes propagate to contact policy

---

# 77. Definition of Done — Messaging

- [ ] inbound + outbound
- [ ] tenant resolution
- [ ] customer resolution
- [ ] external message id deduplication
- [ ] delivery receipt
- [ ] retry policy
- [ ] STOP/opt-out handling
- [ ] shared BusinessTask
- [ ] no duplicate confirmations
- [ ] channel preference stored

---

# 78. Definition of Done — NextActionPolicy

- [ ] deterministic policy rules
- [ ] promised callback precedence
- [ ] DNC veto
- [ ] consent veto
- [ ] business/calling-window veto
- [ ] retry limits
- [ ] channel availability
- [ ] preferred channel
- [ ] urgency/lead priority
- [ ] cost optionality
- [ ] reason codes logged
- [ ] scheduling durable
- [ ] execution re-validates policy

---

# 79. Recommended 4-Phase Execution Sequence

## Phase 1 — Make the current runtime authoritative

Finish:

```text
T4 ownership
SemanticPlan V2
realizer
Google commit adapter
calendar lifecycle
EvidenceBundle live
```

Do not add large integrations while these are unstable.

## Phase 2 — Add durable customer/business state

Build:

```text
Customer
Identity
BusinessTask
OutcomeEngine
NextActionPolicy
Scheduler
Outbox
Reconciliation
Tenant runtime config
DNIS
```

This is the major product transformation.

## Phase 3 — Complete the commercial SMB stack

Build:

```text
GHL
Google Calendar
SMS
WhatsApp
n8n
Slack
missed-call recovery
speed-to-lead
callback continuity
```

Then record the dental demo.

## Phase 4 — Expand only from sales pull

```text
HubSpot
Microsoft
home services
warm transfer
multilingual
SIP
healthcare workflows
```

---

# 80. Final Architecture Decision

The repository should **not** evolve into 80 disconnected “manager” classes.

The correct hierarchy is:

```text
BUSINESS LAYER
Customer
BusinessTask
Outcome
NextAction
Scheduler

        ↓

CONVERSATION LAYER
CallActor
DialogueState
SemanticPlan

        ↓

AUTHORITY LAYER
Policy
CommitCoordinator
Integration Adapter
CommitResult
SpeechCommitGate

        ↓

PLATFORM LAYER
Tenancy
Outbox
Retry
Reconciliation
Observability
Evaluation
```

That is enough structure.

---

# 81. Final Product Thesis After Seeing the Code

Before the audit, the roadmap could look like we needed to build an entire Conversation OS from scratch.

After inspecting the repository, that is **not** the right conclusion.

The repo already has a credible **production voice runtime kernel**.

What it lacks is the **durable business operating layer around that kernel**.

The shortest path to a differentiated commercial product is therefore:

```text
stabilize current voice runtime
        ↓
activate plan-first semantic architecture
        ↓
make real calendar actions transaction-safe
        ↓
add Customer + BusinessTask + Outcome
        ↓
add NextActionPolicy + Scheduler
        ↓
add Outbox + Reconciliation
        ↓
finish GHL/SMS/WhatsApp/n8n
        ↓
ship exceptional dental demo
        ↓
sell reliability audits + SMB automation
```

The moat becomes:

> **A voice runtime that can prove what happened, preserve business truth, recover from failures, continue a customer's task across channels, and decide the next revenue/operations action automatically.**

That is substantially more defensible than another Vapi/Retell configuration portfolio.
