# VoiceOps — Systems Architecture Blueprint for Claude Code
**Date:** 2026-08-19  
**Purpose:** Convert the current receptionist-agent repository into the commercial VoiceOps architecture implied by the accumulated market research.

---

# 0. Read This First

Do **not** respond to this document by creating dozens of empty manager/service classes.

The repository already contains a strong call-level kernel. Reuse and converge existing code.

The new work should focus on the missing **business operating layer around the call runtime**.

The target loop is:

```text
Customer
  ↓
BusinessTask
  ↓
NextActionPolicy
  ↓
Channel / Contact Action
  ↓
Existing Conversation Runtime
  ↓
Authoritative Business Action
  ↓
BusinessOutcome
  ↓
NextActionPolicy again
```

This document defines the actual systems, classes, data models, responsibilities, interactions, and implementation sequence.

---

# 1. Existing Systems to KEEP

Do not duplicate these.

## Call Runtime

Existing:

```text
packages/runtime/call_actor.py
packages/runtime/turn_manager.py
packages/runtime/playback_ledger.py
packages/runtime/heard_text_reconciler.py
packages/runtime/streaming_stt_bridge.py
```

Keep:

```python
CallActor
TurnManager
PlaybackLedger
HeardTextReconciler
```

These remain the call-level runtime.

---

## Dialogue State

Existing:

```text
packages/dialogue/state.py
```

Keep:

```python
DialogueState
ConversationAgenda
TaskState
TaskKind
TaskStatus
SlotEvidence
SlotStatus
```

Important distinction:

`TaskState` is a **conversation-local task**.

We will add a separate persistent object called:

```python
BusinessTask
```

for cross-call/customer workflows.

---

## Commit / Safety

Existing:

```text
packages/dialogue/commit.py
packages/core_agent/speech_commit_gate.py
```

Keep and extend:

```python
CommitCoordinator
CommitResult
ActionProposal
CallerConfirmation
SpeechCommitGate
```

Do not create another generic commit engine.

---

## Structured Input

Existing:

```text
packages/slot_parsers/
```

Keep:

```python
StructuredInputSession
SlotResult
SlotStatus
SlotSource
SlotFragment
```

Extend the parser registry instead of inventing a second framework.

---

## RAG Evidence

Existing:

```text
packages/rag/evidence.py
```

Keep:

```python
EvidenceBundle
EvidenceClaim
Answerability
```

Turn this into the canonical retrieval output for business facts.

---

## Channel Layer

Existing:

```text
packages/channels/base.py
packages/channels/whatsapp.py
```

Keep:

```python
Channel
IncomingMessage
```

Add SMS and later email through this abstraction.

---

# 2. System A — CUSTOMER SYSTEM

## Why it exists

The current repo persists sessions/transcripts/bookings, but not a durable customer entity that can be recognized across voice, WhatsApp, SMS, CRM, and future email.

Without this there is no true customer memory, callback continuity, or omnichannel continuity.

## New package

```text
packages/customer/
    models.py
    identity.py
    memory.py
    service.py
```

## Core classes

```python
class Customer:
    id
    tenant_id
    display_name
    created_at
    updated_at

class CustomerIdentity:
    id
    customer_id
    identity_type
    normalized_value
    provider
    verified
    confidence

class CustomerFact:
    id
    customer_id
    key
    value
    source_type
    source_id
    confidence
    created_at
    expires_at
```

Services:

```python
CustomerService
CustomerIdentityResolver
CustomerMemoryService
```

## Identity types

```text
PHONE
WHATSAPP
SMS_PHONE
EMAIL
GHL_CONTACT
HUBSPOT_CONTACT
CRM_EXTERNAL_ID
```

## Example

```text
voice ANI +923001234567
WhatsApp +923001234567
GHL contact 93723
```

all resolve to:

```text
Customer #182
```

---

# 3. System B — BUSINESS TASK SYSTEM

## Why it exists

The most important new domain object is not another conversation task.

It is a **durable business goal** that survives individual calls.

Examples:

```text
BOOK_APPOINTMENT
RESCHEDULE_APPOINTMENT
QUALIFY_LEAD
OUTBOUND_CONTACT
CALLBACK_REQUEST
MISSED_CALL_RECOVERY
QUOTE_FOLLOWUP
REACTIVATE_CUSTOMER
```

## New package

```text
packages/business_tasks/
    models.py
    service.py
    transitions.py
```

## Classes

```python
class BusinessTask:
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

class TaskAttempt:
    id
    business_task_id
    channel
    session_id
    started_at
    ended_at
    outcome_id

class TaskTransition:
    id
    business_task_id
    from_status
    to_status
    reason
    created_at
```

Services:

```python
BusinessTaskService
TaskTransitionService
```

## Statuses

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

## Relationship to DialogueState

```text
BusinessTask
    ↓
one or more calls/messages
    ↓
each interaction has DialogueState
```

Do not merge these two layers.

---

# 4. System C — OUTCOME ENGINE

## Why it exists

Current market demand repeatedly asks for typed dispositions that drive CRM, retries, follow-up, and analytics.

Do not rely on transcript summaries.

## New package

```text
packages/outcomes/
    models.py
    engine.py
    rules.py
```

## Classes

```python
class BusinessOutcome:
    id
    tenant_id
    customer_id
    business_task_id
    session_id

    code
    confidence
    source
    evidence
    created_at

class OutcomeEngine:
    def derive(...)
    def validate(...)
    def persist(...)
```

## Canonical outcome codes

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
DO_NOT_CALL

FAILED_TECHNICAL
FAILED_WORKFLOW
ABANDONED
UNRESOLVED
```

## Inputs

OutcomeEngine should derive outcomes from:

```text
CommitResult
DialogueState
call transport result
tool receipts
customer statements
policy state
```

The LLM may assist on fuzzy classification, but authoritative receipts override model guesses.

---

# 5. System D — NEXT ACTION POLICY

## Why it exists

This is the main new product system.

It answers:

> Given everything we know right now, what should the business do next?

It sits above channels and below business policy.

## New package

```text
packages/orchestration/
    next_action.py
    priority.py
    scheduler.py
    executor.py
```

## Classes

```python
class NextActionContext:
    customer
    business_task
    latest_outcome
    prior_attempts
    consent_state
    channel_state
    business_hours
    tenant_policy

class NextActionCandidate:
    action_type
    channel
    execute_at
    priority
    reason_codes

class NextActionDecision:
    action_type
    channel
    execute_at
    priority
    reason_codes
    business_task_id

class NextActionPolicy:
    def decide(context) -> NextActionDecision
```

Supporting policies:

```python
PriorityPolicy
ChannelSelectionPolicy
ContactTimingPolicy
CallbackPriorityPolicy
EscalationPolicy
ConsentPolicy
CostPolicy
```

## Actions

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

## Deterministic priority rule

At minimum:

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

Promised callbacks must beat generic campaigns.

DNC must veto all marketing/prospecting actions.

---

# 6. System E — ACTION SCHEDULER

## Why it exists

The repo can detect callback intent, but there is no durable generalized engine that guarantees a callback actually happens later.

## Classes

```python
class ScheduledAction:
    id
    tenant_id
    business_task_id
    customer_id

    action_type
    channel
    execute_at
    priority

    status
    attempt_count
    idempotency_key

class ActionScheduler:
    schedule()
    cancel()
    reschedule()
    claim_due_actions()

class ActionExecutor:
    execute()
```

## Requirements

Must support:

```text
safe worker claims
multi-worker concurrency
leases
retry
cancellation
DNC re-check
consent re-check
calling-window re-check
business-hours re-check
```

Use it for:

```text
promised callbacks
speed-to-lead
missed-call recovery
appointment reminders
quote follow-up
nurture
reactivation
```

---

# 7. System F — OUTBOX / DELIVERY SYSTEM

## Why it exists

Current fire-and-forget calls such as SMS/email confirmation are not durable.

Booking can succeed while confirmation/CRM update disappears.

## New package

```text
packages/platform/outbox.py
```

## Classes

```python
OutboxEvent
OutboxService
OutboxRepository
OutboxWorker

DeliveryReceipt
```

## Flow

```text
authoritative booking succeeds
        ↓
DB transaction
   ├── persist outcome
   ├── update BusinessTask
   └── create OutboxEvents
        ↓
COMMIT
        ↓
workers send:
   SMS
   WhatsApp
   CRM
   Slack
```

Every delivery is idempotent.

---

# 8. System G — RETRY / INTEGRATION ERROR SYSTEM

## Why it exists

Different external failures must be handled differently.

## Classes

```python
IntegrationError
TransientIntegrationError
RateLimitError
AuthenticationError
ValidationError
ConflictError
PermanentIntegrationError

RetryPolicy
RetryDecision
```

## Rules

Reads can often retry automatically.

Writes may retry only when idempotent or when provider semantics guarantee safety.

Never blindly retry business mutations.

---

# 9. System H — RECONCILIATION SYSTEM

## Why it exists

The market explicitly cares about calendar/CRM consistency.

A successful call can still leave business systems divergent.

## New package

```text
packages/platform/reconciliation.py
```

## Classes

```python
ReconciliationService
ConsistencyRule
ReconciliationIssue
ReconciliationResult
```

## Examples

```text
calendar booking exists but CRM appointment missing

CRM says DNC but scheduled call still exists

GHL contact exists twice

SMS outbox says sent but provider delivery failed

VoiceOps says booked but provider object no longer exists
```

Run both event-driven reconciliation and periodic background checks.

---

# 10. System I — TENANT RUNTIME CONFIG

## Why it exists

Database tenant isolation exists.

Runtime configuration is still largely process-global.

Commercial white-label deployments require per-tenant business/profile/provider/integration configuration.

## New package

```text
packages/tenancy/
    config.py
    resolver.py
    secrets.py
    routes.py
```

## Classes

```python
TenantRuntimeConfig
TenantBusinessConfig
TenantIntegrationConfig
TenantVoiceConfig
TenantPolicyConfig

TenantConfigRepository
TenantSecretResolver
```

Tenant config controls:

```text
business profile
timezone
business hours

RAG namespace

calendar
CRM
SMS
WhatsApp

voice
STT
LLM
TTS

transfer targets
compliance rules
```

---

# 11. System J — DNIS / INBOUND ROUTING

## Why it exists

The current Twilio path already knows the number dialed.

Use that to route multi-brand/multi-location calls.

## Classes

```python
InboundRoute
InboundRouteResolver
DialedNumberRegistry
```

## Mapping

```text
dialed number
→ tenant
→ business brand
→ location
→ campaign
→ calendar
→ CRM
→ KB
```

## Example

```text
+1-555-1001
→ Downtown Dental
→ calendar A
→ GHL A
→ KB A

+1-555-1002
→ North Dental
→ calendar B
→ GHL B
→ KB B
```

---

# 12. System K — SEMANTIC PLAN V2

## Why it exists

The repo already has a strong `SemanticPlan` schema, but the current live semantic planner is still post-hoc.

This should become the canonical intelligence architecture.

## Reuse

```text
packages/dialogue/plan.py
```

Keep:

```python
SemanticPlan
PlanOperation
PlannedFact
PlannedQuestion
DeliveryIntent
```

## Add

```text
packages/core_agent/planners/semantic_v2.py
packages/core_agent/realizer.py
```

## Classes

```python
SemanticPlanGenerator
SemanticPlanValidator
SemanticRealizer
DeterministicCriticalRealizer
```

## New live flow

```text
caller input
→ DialogueState
→ evidence/tools
→ SemanticPlan
→ policy/commit
→ authoritative results
→ Realizer
→ SpeechCommitGate
→ TTS
```

## Critical facts

Exact transactional values must be carried structurally.

Example:

```python
PlannedFact(
    key="appointment_time",
    value="2026-08-21T13:30:00-04:00",
    critical=True,
    source="caller_selected_slot"
)
```

The realizer must not change 1:30 into 2:30.

---

# 13. System L — CALENDAR AUTHORITY LAYER

## Why it exists

Commit safety currently works best with FakeCalendar, not the real Google Calendar implementation.

## Add protocol

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
MicrosoftCalendarAdapter later
```

## Rules

Every mutation:

```text
SemanticPlan
→ confirmation
→ CommitCoordinator
→ CalendarAdapter
→ CommitResult
→ SpeechCommitGate
```

All internal timestamps must be timezone-aware.

Business hours come from tenant config.

---

# 14. System M — CRM ADAPTER

## Why it exists

Current GHL code is useful but too narrow and sink-oriented.

## Protocol

```python
class CRMAdapter(Protocol):
    lookup_contact()
    upsert_contact()
    update_contact()

    create_opportunity()
    update_opportunity()
    update_pipeline_stage()

    create_task()
    create_note()
    assign_owner()

    set_tags()
    set_custom_fields()

    get_dnc_state()
    set_dnc_state()

    handle_webhook()
```

## Implement first

```python
GoHighLevelCRMAdapter
```

Reuse the existing `GoHighLevelClient` internally.

Then:

```python
HubSpotCRMAdapter
```

---

# 15. System N — MESSAGING CHANNEL SYSTEM

## Reuse

Existing:

```python
Channel
IncomingMessage
WhatsAppChannel
```

## Add

```python
TwilioSMSChannel
```

Later:

```python
EmailChannel
MicrosoftGraphEmailChannel
```

## Message processing

```text
incoming message
→ tenant resolver
→ CustomerIdentityResolver
→ Customer
→ active BusinessTask
→ conversation/business logic
→ OutcomeEngine
→ NextActionPolicy
```

SMS must implement:

```text
STOP
UNSUBSCRIBE
CANCEL
START
```

as persistent compliance state.

---

# 16. System O — WORKFLOW ADAPTER

## Why it exists

n8n workflows exist in the repo, but VoiceOps needs a normalized event interface.

## Protocol

```python
class WorkflowAdapter(Protocol):
    emit_event()
    handle_action()
```

## Implement

```python
N8nAdapter
```

Payload includes:

```text
event_id
event_type
tenant_id
customer_id
business_task_id
outcome_id
timestamp
schema_version
```

Requirements:

```text
signed webhook
idempotency
retry
versioned schema
```

Later:

```python
MakeAdapter
ZapierAdapter
```

---

# 17. System P — NOTIFICATION SYSTEM

## Add

```python
NotificationAdapter
SlackNotificationAdapter
TeamsNotificationAdapter
```

Events:

```text
HOT_LEAD
CALLBACK_REQUESTED
TRANSFER_REQUEST
UNRESOLVED_CALL
TOOL_FAILURE
RAG_LOW_CONFIDENCE
TECHNICAL_FAILURE
```

Deliver via Outbox rather than synchronous call paths.

---

# 18. System Q — CAMPAIGN ENGINE

## Why it exists

Current outbound routes are prototypes and safety-disabled.

Rebuild outbound around the new durable business-state layer.

## Classes

```python
Campaign
CampaignLead
CampaignPolicy
CampaignManager

ContactAttempt
ContactAttemptService
```

## Flow

```text
lead
→ Customer
→ BusinessTask
→ compliance
→ NextActionPolicy
→ ActionScheduler
→ Voice/SMS/etc.
→ OutcomeEngine
→ next action
```

Do not re-enable the current outbound API as-is.

---

# 19. System R — COMPLIANCE POLICY ENGINE

## Reuse

Existing:

```text
packages/compliance/tcpa.py
packages/compliance/pii.py
```

## Add durable models

```python
ConsentRecord
DNCRecord
ChannelOptOut
DisclosureRecord
```

## Add

```python
CompliancePolicyEngine
```

Checks:

```text
DNC
channel consent
calling windows
SMS opt-out
recording disclosure
AI disclosure
retry limits
```

This engine must be called again **at action execution time**, not only when scheduling.

---

# 20. System S — CUSTOMER MEMORY

## Why it exists

The market increasingly expects repeat callers and cross-channel continuity.

## Classes

```python
CustomerMemoryService
CustomerFact
CustomerPreference
MemoryPolicy
```

Facts carry:

```text
source
confidence
created_at
expires_at
verification state
```

Examples:

```text
preferred language
preferred appointment time
existing booking
unresolved issue
preferred contact channel
```

Do not dump old transcripts wholesale into prompts.

---

# 21. System T — AUTOMATIC CALL EVALUATION

## Why it exists

The repo already has strong observability/adversarial testing.

Turn that into a commercial evaluation product.

## Add

```text
packages/evals/call_evaluator.py
packages/evals/evaluators/
```

## Classes

```python
CallEvaluationPipeline
CallEvaluation
EvaluationFinding
```

Evaluators:

```python
GroundingEvaluator
CommitmentEvaluator
ToolConsistencyEvaluator
TaskCompletionEvaluator
RepetitionEvaluator
DeadAirEvaluator
InterruptionEvaluator
OutcomeConsistencyEvaluator
PolicyComplianceEvaluator
```

## Output

```text
PASS
WARNING
FAIL
```

with failure evidence and event spans.

---

# 22. System U — FAILURE REPLAY / REGRESSION

## Add

```python
FailureFixtureBuilder
ReplayEngine
RegressionCase
RegressionSuite
RegressionResult
```

Pipeline:

```text
production failure
→ classify
→ sanitize/capture
→ create replay fixture
→ expected invariant
→ fix
→ replay
→ permanent regression test
```

This directly supports the Voice Agent Rescue / Reliability Audit service.

---

# 23. System V — HUMAN TRANSFER / ASSIST

## Later

Add only after core commercial stack.

```python
TransferManager
TransferRequest
TransferResult
TransferBrief
HumanQueue
```

Modes:

```text
COLD_TRANSFER
WARM_TRANSFER
HUMAN_ASSIST
CALLBACK_FALLBACK
```

Transfer brief:

```text
caller
intent
BusinessTask
fields collected
urgency
RAG evidence
tool receipts
recommended next step
```

---

# 24. System W — COST / LATENCY BUSINESS METRICS

## Reuse

Existing cost and latency telemetry.

## Extend

Join metrics to:

```text
Customer
BusinessTask
BusinessOutcome
Tenant
```

Add:

```python
UsageRecord
TaskCost
OutcomeCost
```

Metrics:

```text
$/minute
$/call
$/qualified lead
$/booking
$/completed BusinessTask
```

---

# 25. DATA MODEL ADDITIONS

Recommended new tables:

```text
customers
customer_identities
customer_facts

business_tasks
task_attempts
task_transitions

business_outcomes

scheduled_actions
contact_attempts

outbox_events
delivery_receipts

inbound_routes

consent_records
dnc_records
channel_opt_outs

tenant_runtime_configs
tenant_integration_configs

reconciliation_issues
evaluation_results
```

---

# 26. CANONICAL END-TO-END FLOW — INBOUND

```text
Twilio
→ dialed number
→ InboundRouteResolver
→ TenantRuntimeConfig
→ ANI
→ CustomerIdentityResolver
→ Customer
→ existing open BusinessTask or new task
→ CallActor
→ DialogueState
→ SemanticPlan
→ tools/evidence
→ Policy
→ CommitCoordinator
→ CommitResult
→ SpeechCommitGate
→ reply
→ OutcomeEngine
→ BusinessTask transition
→ NextActionPolicy
→ Outbox / ActionScheduler
```

---

# 27. CANONICAL END-TO-END FLOW — OUTBOUND

```text
CRM/form lead
→ Customer
→ BusinessTask(QUALIFY_LEAD)
→ CompliancePolicyEngine
→ NextActionPolicy
→ ScheduledAction
→ ActionExecutor
→ place voice call / send SMS
→ CallActor or Channel
→ OutcomeEngine
→ CRM/outbox
→ NextActionPolicy
→ next contact or completion
```

---

# 28. CANONICAL END-TO-END FLOW — MISSED CALL

```text
inbound call missed
→ Customer resolution
→ BusinessTask(MISSED_CALL_RECOVERY)
→ Outcome(MISSED_CALL)
→ NextActionPolicy
→ SEND_SMS now
→ customer replies
→ TwilioSMSChannel
→ same Customer
→ same BusinessTask
→ continue qualification/booking
```

---

# 29. CANONICAL END-TO-END FLOW — CALLBACK CONTINUITY

```text
outbound call
→ caller says "call tomorrow at 3"
→ Outcome(CALLBACK_REQUESTED)
→ NextActionPolicy
→ ScheduledAction tomorrow 15:00

customer calls back at 14:30
→ ANI resolves Customer
→ open BusinessTask found
→ pending callback task resumes
→ scheduled 15:00 callback cancelled
```

---

# 30. CANONICAL END-TO-END FLOW — BOOKING

```text
caller requests 1:30
→ StructuredInput / DialogueState
→ availability result
→ SemanticPlan critical fact = exact slot
→ confirmation
→ CommitCoordinator
→ GoogleCalendarAdapter
→ CommitResult
→ SpeechCommitGate
→ "You're booked for 1:30"
→ Outcome(BOOKED)
→ BusinessTask COMPLETED
→ Outbox:
    SMS
    WhatsApp
    GHL
    Slack if needed
```

---

# 31. WHAT NOT TO BUILD

Do not create:

```text
another ConversationOrchestrator
another DialogueState
another TurnManager
another SpeechCommitGate
another CommitCoordinator
another generic STT abstraction
another generic TTS abstraction
another phone-input framework
another provider factory layer
```

The repository already has these responsibilities.

---

# 32. IMPLEMENTATION ORDER

## P0 — Finish current runtime correctness

```text
1. T4 / one-response ownership
2. SemanticPlan V2
3. deterministic realizer for critical facts
4. Google Calendar commit adapter
5. Google lifecycle: find/book/reschedule/cancel
6. timezone correctness
7. EvidenceBundle becomes live RAG contract
```

## P1 — New business operating layer

```text
8. Customer
9. CustomerIdentityResolver
10. BusinessTask
11. OutcomeEngine
12. NextActionPolicy
13. ActionScheduler
14. Outbox
15. DeliveryReceipt
16. RetryPolicy
17. ReconciliationService
18. TenantRuntimeConfig
19. InboundRouteResolver
```

## P2 — Commercial integrations

```text
20. normalized GHL CRMAdapter
21. TwilioSMSChannel
22. WhatsApp customer/task continuity
23. N8nAdapter
24. SlackNotificationAdapter
25. missed-call recovery
26. speed-to-lead
27. callback continuity
```

## P3 — Evaluation / expansion

```text
28. CallEvaluationPipeline
29. FailureFixtureBuilder / ReplayEngine
30. HubSpot
31. Microsoft Calendar
32. Microsoft Graph Email
33. Teams
34. warm transfer
```

## P4 — Later

```text
35. home services adapters
36. multilingual LanguageProfile
37. SIP
38. human answering-service mode
39. advanced healthcare workflows
```

---

# 33. DEFINITION OF DONE — NEXT ACTION POLICY

NextActionPolicy is not done until:

- [ ] outcome can trigger it
- [ ] Customer context is available
- [ ] BusinessTask context is available
- [ ] promised callbacks outrank campaigns
- [ ] DNC veto exists
- [ ] consent veto exists
- [ ] business/calling windows are checked
- [ ] channel availability is checked
- [ ] retry limits are checked
- [ ] preferred channel can influence decision
- [ ] reason codes are persisted
- [ ] scheduled action is durable
- [ ] policy is re-validated at execution time

---

# 34. DEFINITION OF DONE — BUSINESS TASK

BusinessTask is not done until:

- [ ] persisted
- [ ] tied to Customer
- [ ] survives calls
- [ ] survives server restart
- [ ] can have many TaskAttempts
- [ ] can be continued by voice/SMS/WhatsApp
- [ ] can wait for callback/human/provider
- [ ] OutcomeEngine can transition it
- [ ] NextActionPolicy can act on it
- [ ] completed task prevents obsolete follow-up

---

# 35. DEFINITION OF DONE — CUSTOMER IDENTITY

- [ ] normalized phone identity
- [ ] WhatsApp identity mapping
- [ ] SMS identity mapping
- [ ] CRM external ID mapping
- [ ] duplicate identity prevention
- [ ] ambiguous identity handling
- [ ] tenant scoped
- [ ] callback lookup works by ANI
- [ ] same phone customer works across channels

---

# 36. DEFINITION OF DONE — OUTBOX

- [ ] persisted in same logical transaction as business state
- [ ] idempotent delivery
- [ ] retry
- [ ] delivery receipt
- [ ] poison/dead-letter behavior
- [ ] provider failure observable
- [ ] safe process restart
- [ ] safe multiple workers

---

# 37. FIRST COMMERCIAL SYSTEM TO DEMO

After P0 + P1 + core P2:

```text
Dental receptionist
+ Google Calendar
+ GHL
+ SMS/WhatsApp
+ BusinessTask
+ OutcomeEngine
+ NextActionPolicy
```

Demo:

```text
call
→ RAG FAQ
→ booking
→ authoritative receipt
→ confirmation
→ CRM
→ outcome
→ no unnecessary next action
```

Then intentionally fail calendar:

```text
tool failure
→ no false booking speech
→ FAILED_TECHNICAL outcome
→ NextActionPolicy
→ CREATE_HUMAN_TASK or retry/recovery message
```

---

# 38. SECOND COMMERCIAL SYSTEM TO DEMO

Missed-call recovery:

```text
missed call
→ customer resolved
→ BusinessTask
→ NextActionPolicy
→ SMS
→ SMS reply
→ same task
→ booking
```

This proves VoiceOps is more than a phone bot.

---

# 39. THIRD COMMERCIAL SYSTEM TO DEMO

Speed-to-lead:

```text
web/CRM lead
→ task
→ policy
→ SMS first or voice first
→ no response
→ alternate channel
→ qualification
→ appointment
→ outcome
→ CRM
```

This is where NextActionPolicy becomes visible as a product differentiator.

---

# 40. FINAL ARCHITECTURE

```text
BUSINESS LAYER
Customer
CustomerIdentity
CustomerMemory
BusinessTask
BusinessOutcome
NextActionPolicy
ActionScheduler

        ↓

CHANNEL LAYER
Voice
SMS
WhatsApp
Email later

        ↓

CONVERSATION LAYER
CallActor
DialogueState
SemanticPlan
Realizer

        ↓

AUTHORITY LAYER
Policy
CommitCoordinator
Calendar / CRM / Tools
CommitResult
SpeechCommitGate

        ↓

PLATFORM LAYER
Tenant Config
DNIS
Outbox
Retry
Reconciliation
Compliance
Observability
Evaluation
```

---

# 41. Final Instruction to Claude Code

Do not optimize for the number of new classes.

Optimize for making this loop real:

```text
Customer
→ BusinessTask
→ NextActionPolicy
→ Interaction
→ Authoritative Outcome
→ BusinessTask transition
→ NextActionPolicy
```

The current codebase already owns the interaction runtime.

The next major product milestone is owning the **customer task across interactions**.
