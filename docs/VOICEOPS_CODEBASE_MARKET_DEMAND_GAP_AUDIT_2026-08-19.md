# VoiceOps Codebase × Market Demand — Gap Audit

**Author:** ChatGPT (external audit of this repo against accumulated voice-agent Upwork demand research)
**Delivered:** 2026-08-19
**Verified against actual code:** _pending — see `WORKING-NOTES.md` "Audit verification" section for what's confirmed vs overstated_

**Why this doc exists:** ChatGPT did a repo-specific audit (not a generic wishlist) and identified that (a) the call-runtime is much more built-out than either of us thought, and (b) the actual missing layer is business-side (Customer, BusinessTask, NextActionPolicy, ActionScheduler, Outbox). This reorders the roadmap.

**Working-notes cross-reference:** the "Session log" entry for 2026-08-19 records what was verified and what remains to check. Do NOT execute items from this doc without first cross-checking against the verified gap map.

---

## 60-second conclusion

Repo is much further along than expected.  You do **not** need to build the giant architecture from scratch. You already have a surprisingly large portion of the difficult voice-runtime layer:

| System                           | Repo status                  |
| -------------------------------- | ---------------------------- |
| `CallActor` / temporal ownership | Strong                       |
| Turn management                  | Strong                       |
| Barge-in / cancellation          | Strong, active bugs remain   |
| `DialogueState`                  | Strong                       |
| Conversation-level `TaskState`   | Strong                       |
| `CommitCoordinator`              | Strong                       |
| `SpeechCommitGate`               | Strong                       |
| Structured-input architecture    | Strong base, phone only      |
| RAG                              | Good                         |
| `EvidenceBundle`                 | Already exists               |
| Provider abstraction             | Strong                       |
| Latency telemetry                | Strong                       |
| Cost telemetry                   | Already exists               |
| Failure intelligence             | Good                         |
| Adversarial testing              | Good                         |
| Google Calendar                  | Partial                      |
| GHL                              | Partial                      |
| WhatsApp                         | Transport exists             |
| SMS                              | Outbound confirmation exists |
| n8n                              | Workflow examples exist      |
| TCPA / consent primitives        | Exists                       |
| DB tenant isolation              | Strong                       |

The thing missing is the layer **above the call.**

```text
                  WHAT YOU HAVE

                  ┌──────────┐
                  │CallActor │
                  └────┬─────┘
                       │
                DialogueState
                       │
                   LLM/tools
                       │
              CommitCoordinator
                       │
               SpeechCommitGate
                       │
                  caller


                  WHAT WE ADD

                    Customer
                       │
                  BusinessTask
                       │
                NextActionPolicy
             ┌─────────┼──────────┐
             │         │          │
           Voice      SMS      WhatsApp
             │         │          │
             └─────────┼──────────┘
                       │
                  existing
                 CallActor
                       │
                DialogueState
                       │
                 SemanticPlan
                       │
              CommitCoordinator
                       │
               authoritative
                    tools
                       │
                 CommitResult
                       │
                  OutcomeEngine
                       │
                NextActionPolicy
                       │
             ┌─────────┼────────────┐
             │         │            │
           CRM      Scheduler      Human
```

That is the commercial transformation.

---

## The biggest finding — SemanticPlan is already built but unused

Repo has:

```text
packages/dialogue/plan.py
```

with:

```python
SemanticPlan
PlanOperation
PlannedFact
PlannedQuestion
DeliveryIntent
```

Comments describe exactly the architecture we've been discussing:

```text
understand
→ semantic plan
→ validate
→ tool/commit
→ realize speech
```

including sourced facts, forbidden claims, critical facts that must not be paraphrased.

**But** current live path in `packages/core_agent/planners/semantic.py` does something much weaker:

```text
ReceptionistBrain
       ↓
already generates response
       ↓
SemanticPlanner
       ↓
classifies response AFTERWARD
```

So it's effectively:

```text
LLM creates answer
→ then we determine what kind of answer it was
```

instead of:

```text
LLM/code determines WHAT must happen
→ authority validates it
→ only then determines HOW to say it
```

**This is probably the most important intelligence refactor after T4.**

Directly explains bugs recently patched:

- Caller: "1:30", Calendar: "1:30 is valid", Agent: "2:30". Patched with prompt rule.
  With proper SemanticPlan: `PlannedFact(claim="1:30 PM", source="caller:selected_time", critical=True)` — realizer can't substitute.
- "I want tooth implants after" — dropped. Repo already has `SemanticPlan.pending_tasks` for deferred multi-intent.

**Recommendation:** finish that architecture instead of continuing to patch every intelligence error into the prompt.

---

## Calendar architecture mismatch

Safe commit adapter is `packages/integrations/calendar_commit_adapter.py`, implementation is literally `FakeCalendarBookingAdapter`. Calls:

```python
calendar.book(
    ...,
    idempotency_key=proposal.idempotency_key
)
```

But `GoogleCalendar.book()` **doesn't accept `idempotency_key`.** And Google Calendar currently only implements ~ `is_available()`, `list_slots()`, `book()`, whereas clinic tools expose `find_existing_appointment`, `cancel_appointment`, `reschedule_appointment`. Those lifecycle capabilities are properly implemented/tested against `FakeCalendar`, not the real Google integration.

Need:

```python
CalendarAdapter

GoogleCalendarAdapter
FakeCalendarAdapter
MicrosoftCalendarAdapter     # later
```

with:

```python
get_availability()
find_booking()

create_booking(idempotency_key=...)
reschedule_booking(idempotency_key=...)
cancel_booking(idempotency_key=...)
```

**Every mutation goes through `CommitCoordinator`.** Directly addresses "how do you prevent double bookings and CRM/calendar inconsistency?" (repeated Upwork demand signal).

---

## Timezone problem worth fixing

Google adapter has logic like:

```python
timeMin=start.isoformat() + "Z"
```

Dangerous. If `start` has timezone → `2026-08-19T13:30:00+05:00Z` is wrong.  If it doesn't → appending `Z` declares 13:30 UTC even if clinic meant 13:30 America/New_York.

`GoogleCalendar.list_slots()` defaults to 09:00–17:00 instead of deriving from tenant business hours.

Especially worth fixing because timezone failures were repeatedly appearing in actual paid "repair my voice agent" jobs from research.

---

## Structured input is much easier than expected

Repo already has `packages/slot_parsers/` with:

```python
StructuredInputSession
SlotStatus
SlotSource
SlotFragment
SlotResult
```

and the exact states we wanted:

```text
INCOMPLETE
POSSIBLE
VALID
AMBIGUOUS
INVALID
```

Registry currently only has phone. Extend it:

```text
PhoneParser       ← already
EmailParser
DateParser
TimeParser
DateTimeParser
PostalCodeParser
AddressParser
DOBParser
IdentifierParser
CurrencyParser
```

Later: InsuranceMemberIdParser, PropertyAddressParser, ConfirmationCodeParser.

Much smaller project than creating a new Structured Input subsystem.

---

## RAG further ahead than expected

Repo has `EvidenceBundle`, `EvidenceClaim`, `Answerability` in `packages/rag/evidence.py`. That's the evidence architecture we wanted.

Problem: strongest pathway isn't the normal active pathway. RAG handler has `emit_evidence_bundle=False` by default. Runtime tends toward:

```text
retrieval
→ shaped prose
→ LLM
```

Want:

```text
retrieval
      ↓
EvidenceBundle
      ↓
SemanticPlan.facts[]
      ↓
grounded realization
```

Very important convergence.

---

## Main missing layer — BusinessTask

Do NOT confuse with existing `TaskState` inside `DialogueState` (conversation-local).

Create:

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

    context
    authoritative_refs
```

Types: `BOOK_APPOINTMENT`, `RESCHEDULE_APPOINTMENT`, `QUALIFY_LEAD`, `OUTBOUND_CONTACT`, `MISSED_CALL_RECOVERY`, `CALLBACK_REQUEST`, `QUOTE_FOLLOWUP`, `REACTIVATE_CUSTOMER`.

States: `OPEN`, `IN_PROGRESS`, `WAITING_CUSTOMER`, `WAITING_PROVIDER`, `WAITING_CALLBACK`, `WAITING_HUMAN`, `COMPLETED`, `FAILED`, `CANCELLED`.

Now a business process can survive the phone call.

---

## Then Customer

Genuinely missing. DB currently has: `Tenant`, `ApiKey`, `IdempotencyRow`, `SessionRow`, `TranscriptRow`, `BookingRow`. But NOT: `Customer`, `CustomerIdentity`, `CustomerFact`.

Add:

```python
Customer

CustomerIdentity
CustomerFact

CustomerService
CustomerIdentityResolver
CustomerMemoryService
```

Maps:

```text
phone:+923001234567
whatsapp:+923001234567
ghl:contact_7291
email:abbas@example.com
```

→ `Customer #182`.

Unlocks persistent memory + channel continuity.

---

## Then NextActionPolicy

Probably the single highest-value genuinely new system.

```python
NextActionPolicy
NextActionContext
NextActionCandidate
NextActionDecision
```

Supporting deterministic policies:

```python
PriorityPolicy
ChannelSelectionPolicy
ContactTimingPolicy
CallbackPriorityPolicy
ConsentPolicy
EscalationPolicy
```

Example decisions:

```json
{
  "action": "PLACE_CALL",
  "channel": "voice",
  "execute_at": "2026-08-19T10:13:00",
  "priority": 90,
  "reason": ["NEW_LEAD", "VOICE_CONSENT", "NO_SMS_RESPONSE"]
}
```

```json
{
  "action": "SEND_WHATSAPP",
  "execute_at": "now",
  "priority": 50,
  "reason": ["MISSED_INBOUND_CALL"]
}
```

```json
{
  "action": "STOP_CONTACT",
  "priority": 100,
  "reason": ["DO_NOT_CALL"]
}
```

Closes the loop:

```text
Outcome → NextActionPolicy → Action → Outcome → NextActionPolicy
```

---

## ActionScheduler

Callback extraction exists in outbound flows but no generalized durable system for "call me tomorrow at 3."  Create:

```python
ScheduledAction
ActionScheduler
ActionExecutor
```

Use for: promised callbacks, lead retries, appointment reminders, missed-call recovery, quote follow-ups, nurture, reactivation.

---

## Outbox + reconciliation — high priority

Currently: `asyncio.create_task(send_sms(...))` can disappear if process dies after booking succeeds.

Instead:

```text
booking success
      ↓
DB transaction
      ├─ booking outcome
      └─ OutboxEvent
           ├─ SMS
           ├─ GHL
           └─ Slack
      ↓
COMMIT
```

Worker delivers safely.

Classes: `OutboxEvent`, `OutboxService`, `OutboxWorker`, `DeliveryReceipt`, `RetryPolicy`, `ReconciliationService`.

Solves biggest client concern:

```text
Calendar says booked
GHL says nothing

or

GHL says booked
Calendar doesn't
```

---

## GHL useful but not yet what buyers mean

Currently supports:

```text
upsert contact
add note
create opportunity
get free slots
book appointment
```

Upwork demand wants more:

```text
contact lookup/upsert
custom fields
tags

opportunity lookup
opportunity update
pipeline transitions

tasks
owner assignment

appointments
DNC
consent

workflow triggers
webhooks

two-way reconciliation
```

Existing `GoHighLevelClient` should become the implementation behind `CRMAdapter`.  Don't throw it away.

---

## Operational multi-tenancy — major gap

DB tenancy is good.  Explicit tenant guards + tenant-scoped persistence.

Runtime still has globals:

```python
_business_cache
_calendar_cache
_sink_cache
_retriever_cache
```

`load_business()` loads a single `BUSINESS_PROFILE_PATH` for the process.  WhatsApp literally does `tenant_id = "default"`.

> **Database multi-tenancy exists. Operational multi-tenancy does not yet.**

For agency/white-label:

```python
TenantRuntimeConfig
TenantIntegrationConfig
TenantVoiceConfig
TenantPolicyConfig

TenantConfigRepository
TenantSecretResolver
```

---

## DNIS — easy because data already exists

Twilio already knows `caller_number` + `dialed_number`.

Need:

```python
InboundRouteResolver
InboundRoute
```

Route by dialed number to tenant/location.  Marketable for agencies + multi-location businesses.

---

## WhatsApp — half-complete in the right way

Have transport abstraction:

```python
Channel
IncomingMessage
VoiceMessagePipeline
WhatsAppChannel
TelegramChannel
```

Do NOT build new omnichannel framework.  Fix identity/state:

```text
WhatsApp message
      ↓
tenant resolver
      ↓
CustomerIdentityResolver
      ↓
Customer
      ↓
active BusinessTask
      ↓
existing brain/runtime
```

Then phone + WhatsApp become one system.

---

## SMS — same Channel abstraction

Right now SMS is basically a sender.  Add:

```python
TwilioSMSChannel(Channel)
```

Gives: inbound SMS, outbound SMS, delivery receipt, STOP handling, customer resolution, BusinessTask continuation.

Unlocks: missed-call recovery, speed-to-lead, appointment continuation, callbacks.

---

## Outbound needs redesign before enabling

Own repo already knows.  `/outbound` disabled by default, documents issues: consent provider bypass, client-controlled DNC, client-controlled sheet, caller-ID override.

Production-like route currently calls `decide_can_call()` instead of `decide_can_call_with_consent()`.

Don't patch old outbound endpoint.  Build outbound on:

```text
Customer
      ↓
BusinessTask
      ↓
CompliancePolicy
      ↓
NextActionPolicy
      ↓
ActionScheduler
      ↓
CallActor
      ↓
OutcomeEngine
```

---

## Final implementation order

1. **Finish T4 / call-response ownership.** Verify multi-fire is gone, fix streaming test drift, measure latency again.
2. **Make the existing `SemanticPlan` architecture real.** Add plan generator + realizer; exact facts + secondary intents become structured, not prompt-only.
3. **Make real Google Calendar transaction-safe.** Proper `CalendarAdapter`, idempotency, timezone, book/find/reschedule/cancel through `CommitCoordinator`.
4. **Turn `EvidenceBundle` on and feed RAG evidence into SemanticPlan.**
5. **Add `Customer + CustomerIdentity`.**
6. **Add durable `BusinessTask`.**
7. **Add generic `OutcomeEngine`.**
8. **Add `NextActionPolicy`.**
9. **Add durable `ActionScheduler`.**
10. **Add Outbox + DeliveryReceipt + Retry + Reconciliation.**
11. **Replace process-global business/integration config with tenant runtime config.**
12. **Add DNIS route resolution.**
13. **Normalize GHL behind `CRMAdapter`.**
14. **Add inbound `TwilioSMSChannel`.**
15. **Make WhatsApp share Customer + BusinessTask state.**
16. **Add n8n event adapter.**
17. **Add Slack notifications.**
18. **Build missed-call recovery.**
19. **Build speed-to-lead.**
20. **Build callback continuity.**
21. **Add automated post-call evaluators and failure→regression replay.**
22. **Record the killer dental demo.**
23. Then HubSpot/Microsoft/Teams/Make.
24. Then home services.
25. Then warm transfer/multilingual/SIP/advanced healthcare.

> **Strategic shift after actually seeing the code: we are not building the Conversation OS from zero anymore. We already built most of the difficult call-level kernel.**
>
> Now build the **business operating layer around it:**
> Customer + BusinessTask + Outcome + NextAction + Scheduler + Outbox/Reconciliation + tenant-aware integrations.
>
> That turns this from an impressive voice-agent repo into something repeatedly sellable for: dental reception, GHL automation, missed-call recovery, outbound speed-to-lead, white-label agency deployments, voice-agent rescue/optimization.
