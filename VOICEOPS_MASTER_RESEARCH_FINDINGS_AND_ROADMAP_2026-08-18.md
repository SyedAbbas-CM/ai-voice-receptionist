# VoiceOps AI --- Master Market Research, Product Strategy & Engineering Roadmap

**Research consolidation:** approximately August 13--18, 2026\
**Purpose:** Canonical handoff for Claude Code. This consolidates the
findings from the recurring VoiceOps/AI-automation market-watch reports
discussed over the preceding days, removes repetition, and converts the
market signals into an implementation and Upwork strategy.

> **Important:** This is not a fresh market-search report. It is
> primarily a consolidation of the scheduled-watch findings already
> surfaced in our conversations. The final sections translate those
> accumulated findings into product decisions.

------------------------------------------------------------------------

# 0. ADHD MASTER SUMMARY

## The market in one sentence

Businesses are no longer merely asking for "an AI that answers the
phone." The valuable work is increasingly:

**voice + reliable business actions + CRM + calendar + follow-up +
outbound + messaging + observability + reusable deployments.**

## The biggest discoveries across the reports

### 1. Basic AI receptionists are commoditizing

The common package is already:

``` text
Retell/Vapi
+ Twilio
+ ElevenLabs
+ GoHighLevel
+ n8n/Make/Zapier
+ FAQ
+ booking
+ SMS
```

This is not enough to differentiate us.

### 2. Fixing broken existing voice agents is a real market

Repeated buyers already have Retell/Vapi/Twilio/ElevenLabs systems and
want someone to fix:

``` text
latency
dead air
interruptions
call drops
duplicate speech
bad naturalness
timezone bugs
tool failures
invalid input
false confirmations
off-script behavior
```

This should become a sellable **Voice Agent Reliability Audit / Rescue**
service.

### 3. Buyers increasingly care about business outcomes

They want:

``` text
BOOKED
INTERESTED
NOT_INTERESTED
CALLBACK
NO_ANSWER
VOICEMAIL
DNC
TRANSFERRED
QUALIFIED
FAILED
```

These outcomes must drive CRM state, retries, alerts and reporting.

### 4. Outbound is becoming a first-class product

Not merely "make outbound calls."

The demanded flow is increasingly:

``` text
lead arrives
→ call within seconds
→ qualify
→ handle objections
→ book/transfer
→ disposition
→ CRM
→ retry/SMS
```

### 5. Shared state across phone + SMS + WhatsApp is moving toward baseline

We should not build separate bots.

Build:

``` text
Customer
→ shared memory/task state
→ Phone / SMS / WhatsApp / Web
```

### 6. Agencies want reusable deployments

White-label agencies want someone who can repeatedly deploy signed
clients.

We therefore need:

``` text
Core Runtime
+ Vertical Template
+ Tenant Config
+ Integration Pack
+ Knowledge Pack
= Client Deployment
```

### 7. Production reliability is itself marketable

Buyers explicitly care about:

-   double-booking prevention
-   CRM/calendar consistency
-   API failure behavior
-   latency
-   interruptions
-   retries
-   logging
-   staging/testing
-   regression behavior

Our CommitGate/ActionReceipt architecture is commercially useful.

### 8. Cost is becoming a differentiator

There is demand for custom infrastructure partly to escape
managed-platform per-minute markup.

Therefore benchmark:

``` text
latency
quality
reliability
$/minute
$/call
$/successful outcome
```

### 9. Healthcare/dental remains the best first polished vertical

It exercises:

-   RAG
-   appointments
-   structured intake
-   repeat callers
-   urgent escalation
-   messaging
-   transactional correctness
-   eventually compliance

### 10. Home services is the strongest second vertical

HVAC/plumbing/electrical naturally need:

``` text
address
service area
job type
urgency
quote
booking
CRM/job system
SMS
```

------------------------------------------------------------------------

# 1. What Changed Across the Daily Research

The research did not point to one sudden revolutionary feature. Instead,
each day strengthened a different layer of the same thesis.

The progression was approximately:

``` text
Voice bot
↓
Integrated receptionist
↓
Revenue/operations agent
↓
Reliable transactional runtime
↓
Reusable multi-tenant Conversation OS
```

The following sections preserve the major day-by-day findings.

------------------------------------------------------------------------

# 2. Early Signal --- Buyers Are Paying to Fix Existing Voice Agents

One of the strongest early findings was a current Upwork buyer looking
specifically for Vapi/Retell/GHL expertise to diagnose an existing
receptionist's:

-   latency
-   naturalness

This matters because the client already had the voice system.

The product category is therefore not merely:

> Build me an AI receptionist.

It is also:

> My AI receptionist exists but sucks. Diagnose and fix it.

That directly supports a second commercial offer:

# Voice Agent Performance & Reliability Audit

Potential targets:

``` text
Retell
Vapi
Twilio
ElevenLabs
GoHighLevel
n8n
custom stacks
```

Audit:

``` text
STT timing
endpointing
LLM timing
tool timing
TTS timing
playout
interruptions
duplicate speech
dead air
tool failures
RAG failures
state errors
```

------------------------------------------------------------------------

# 3. Baseline Commercial Feature Set

Across multiple reports, the normal buyer expectation repeatedly
included:

-   24/7 inbound reception
-   outbound follow-up
-   lead qualification
-   appointment booking
-   rescheduling
-   cancellation
-   live transfer
-   missed-call recovery
-   CRM synchronization
-   SMS follow-up
-   post-call automation
-   call transcripts
-   recordings
-   structured outcomes

Frequently named surrounding systems included:

``` text
GoHighLevel
HubSpot
Salesforce
Google Calendar
n8n
Make
Zapier
SMS
WhatsApp
```

Therefore these should be viewed as ecosystem requirements, not exotic
add-ons.

------------------------------------------------------------------------

# 4. Speed-to-Lead Became a Repeated Signal

Several jobs emphasized immediate lead response.

Desired architecture:

``` text
web form / CRM lead
       ↓
webhook
       ↓
policy/consent validation
       ↓
outbound call within seconds
       ↓
qualification
       ↓
booking / transfer
       ↓
CRM outcome
       ↓
SMS / follow-up
```

This should eventually be a first-class runtime mode.

Metrics:

``` text
lead_created_to_call_start_ms
lead_created_to_contact_ms
lead_created_to_booking_ms
```

------------------------------------------------------------------------

# 5. Missed-Call Recovery

Another repeated commercial capability:

``` text
customer calls
→ nobody answers / agent unavailable
→ immediate SMS or WhatsApp
→ customer continues conversation
→ optional AI callback
```

This is valuable because the pitch is directly tied to recovered
revenue.

It should use the same CustomerState as the phone call.

------------------------------------------------------------------------

# 6. Vertical Concentration

Recurring demand clustered around:

## Healthcare / Dental

-   appointments
-   intake
-   FAQs
-   insurance
-   patient outreach
-   escalation

## Home Services

-   HVAC
-   plumbers
-   roofers
-   electricians
-   locksmiths
-   general service businesses

## Real Estate

-   lead qualification
-   outbound warming
-   booking
-   callbacks

## Sales / Marketing Agencies

-   speed-to-lead
-   appointment setting
-   white-label client deployments

## Legal / Professional Services

-   reception
-   qualification
-   routing
-   scheduling

Healthcare/dental remained the strongest first demo because it forces us
to solve reusable hard problems.

------------------------------------------------------------------------

# 7. Production Engineering Became a Hiring Criterion

The reports increasingly surfaced buyers evaluating:

``` text
latency
interruptions
background noise
failure handling
privacy
retention
pronunciation
fallback behavior
backend skills
testing
analytics
deployment experience
```

This is critical.

A portfolio should not merely say:

> I build AI voice agents.

It should visibly prove:

``` text
real call
→ natural interruption
→ RAG
→ structured input
→ tool call
→ authoritative receipt
→ deliberate provider failure
→ graceful recovery
→ analytics
```

------------------------------------------------------------------------

# 8. Pricing Bifurcation

The research repeatedly showed a split.

## Commodity

Simple Retell/Vapi/GHL packages are heavily price compressed.

These often sell: - setup - calendar - FAQ - CRM - basic automation

## Higher-value

More serious production builds pay for: - custom backend - reliability -
integrations - deployment - analytics - optimization - ongoing support

Therefore:

**Do not position VoiceOps as "I configure Retell."**

Position it as:

> Production voice engineering and business automation.

------------------------------------------------------------------------

# 9. Outbound Revenue Engine Signal

A particularly useful job pattern involved:

``` text
Apollo/list
→ Vapi/Twilio calls
→ sales conversation
→ objection handling
→ classify interest
→ callback handling
→ wrong-contact handling
→ discovery booking
→ CRM
→ n8n
→ Slack
```

This led to the typed-disposition concept:

``` text
INTERESTED
NOT_INTERESTED
WRONG_CONTACT
CALLBACK_REQUESTED
VOICEMAIL
BOOKED
DO_NOT_CALL
NO_ANSWER
QUALIFIED_NOT_BOOKED
```

These must be code-level state, not merely an LLM-generated summary.

------------------------------------------------------------------------

# 10. Productization Became Paid Work

One particularly important signal came from an existing voice-AI agency
seeking help with:

-   LLM improvements
-   voice improvements
-   latency
-   realtime behavior
-   n8n/Make/API layer
-   logging
-   technical documentation
-   standardized packages
-   reusable client deployments

This means agencies are reaching the stage where their problem is no
longer:

> Can we build one agent?

It is:

> How do we turn this into a maintainable product?

That directly validates our template architecture.

------------------------------------------------------------------------

# 11. Human Receptionist QA Opportunity

Another market signal was AI being used to **audit human
receptionists**, rather than replace them.

Example pattern:

``` text
call transcript
→ appointment/CRM verification
→ rubric evaluation
→ exception detection
→ Teams/Slack
```

Potential scoring:

``` text
greeting
discovery
insurance
emergency handling
cancellation handling
scheduling accuracy
required questions
```

This is valuable because businesses can buy it before trusting AI to
answer calls.

Commercial progression:

``` text
AI audits humans
→ AI handles overflow
→ AI handles missed calls
→ AI handles routine calls
```

------------------------------------------------------------------------

# 12. Home Services Strengthened

Home-services demand repeatedly appeared.

Typical workflow:

``` text
missed call
→ qualify job
→ address
→ service-area check
→ quote/estimate
→ schedule
→ confirmation
→ CRM/job system
```

Additional recurring requirements:

-   property qualification
-   pricing tables
-   payment links
-   quote follow-up
-   no-show recovery
-   voicemail → SMS
-   database reactivation

This should be the second vertical template.

------------------------------------------------------------------------

# 13. Production Stack Baseline

Repeated named technologies:

``` text
Retell
Vapi
ElevenLabs
Twilio
GoHighLevel
n8n
Make
Zapier
```

These technologies themselves are becoming commodity knowledge.

Differentiation should come from:

``` text
transaction correctness
RAG quality
latency
failure recovery
structured state
provider independence
evaluation
observability
```

------------------------------------------------------------------------

# 14. Multilingual Signal

Several reports surfaced multilingual requirements.

Examples included:

``` text
English + German
English + Spanish
Arabic + English
```

The important architecture decision is not merely "LLM responds in
another language."

Language profile should switch:

``` text
STT
endpointing
LLM language
RAG language
TTS
structured-input normalization
```

------------------------------------------------------------------------

# 15. Healthcare Compliance Signal

Healthcare buyers increasingly expect actual production compliance
knowledge:

-   BAA
-   encryption
-   least privilege
-   audit logging
-   retention
-   PHI handling
-   real deployment experience

Long term:

``` text
ComplianceMode
```

must be architectural rather than a prompt instruction.

------------------------------------------------------------------------

# 16. Outcome / Revenue Event Model Became Central

A later report strengthened the idea that the call itself is not the
final product.

The call produces a business event.

``` text
Call
 ↓
Conversation Outcome
 ↓
Business Event
```

Canonical outcomes:

``` text
INTERESTED
BOOKED
CALLBACK
NO_ANSWER
VOICEMAIL
DNC
TRANSFERRED
QUALIFIED
DISQUALIFIED
ABANDONED
FAILED_TECHNICAL
FAILED_WORKFLOW
```

Each can drive: - CRM - retries - messaging - owner assignment -
analytics - alerts

------------------------------------------------------------------------

# 17. Business KPI Ownership

Buyers increasingly evaluate systems using:

``` text
cost per booked appointment
show rate
sales conversion
ROAS
revenue
acquisition cost
lifetime value
```

Therefore our dashboard should eventually contain both:

## Engineering metrics

``` text
latency
tool failures
provider errors
```

and:

## Business metrics

``` text
booking conversion
qualified leads
show rate
recovered calls
cost per outcome
```

------------------------------------------------------------------------

# 18. Reusable / White-Label Deployments

Several reports showed agencies looking for technical partners who can
repeatedly implement signed clients.

Desired deployment flow:

``` text
clone template
→ load KB
→ connect CRM
→ connect calendar
→ choose voice
→ configure business
→ regression test
→ deploy
```

No customer-specific source-code branch should be necessary.

------------------------------------------------------------------------

# 19. Compliance Expanded Beyond Healthcare

Outbound work surfaced requirements such as:

``` text
TCPA consent
DNC
calling windows
recording disclosure
AI disclosure
A2P 10DLC
SMS opt-outs
retry limits
```

These should become structured policy state.

Do not rely on prompts to remember legal/operational restrictions.

------------------------------------------------------------------------

# 20. Native CRM Voice AI Competition

Another signal was GoHighLevel's own voice/Conversation AI becoming
capable enough for simple SMB deployments.

This means we cannot win solely by being easier to configure.

Our custom runtime should win where native systems struggle:

``` text
complex tools
custom backend
strong RAG
latency engineering
transaction safety
provider routing
multilingual
structured input
observability
```

------------------------------------------------------------------------

# 21. Persistent Customer Memory

Later research strengthened persistent customer state.

Customer memory should include:

``` text
identity
verified numbers
language
bookings
previous intents
unresolved issues
prior outcomes
preferred channel
durable facts + provenance
```

A returning caller should not be treated as a completely new human.

------------------------------------------------------------------------

# 22. Omnichannel Continuity

Phone, SMS and WhatsApp are converging into one customer conversation.

Architecture:

``` text
Tenant
  ↓
Customer
  ↓
Shared Task State
  ↓
Phone | SMS | WhatsApp | Web
```

Do not build:

``` text
voice bot
sms bot
whatsapp bot
```

as three unrelated systems.

------------------------------------------------------------------------

# 23. Enterprise Procurement Signal

Enterprise readiness eventually requires evidence such as:

``` text
data-flow map
subprocessor inventory
retention configuration
recording policy
encryption config
audit logs
access controls
consent policy
backup/failover
```

Do not build all this immediately, but structure
observability/configuration so it can eventually be exported.

------------------------------------------------------------------------

# 24. Provider Flexibility / Latency Tournament

Voice models and provider quality move rapidly.

Avoid:

``` text
Deepgram + OpenAI + ElevenLabs forever
```

Use:

``` text
PerceptionAdapter
ReasoningAdapter
TTSAdapter
TelephonyAdapter
```

Benchmark:

``` text
latency
accuracy
interruptions
tool success
cost
failure rate
language
```

------------------------------------------------------------------------

# 25. Healthcare Operations Beyond Reception

A major higher-value signal involved healthcare voice agents embedded in
workflows such as:

``` text
referrals
prior authorization
scheduling
care-gap closure
patient outreach
```

This is significantly more defensible than a generic receptionist.

The important engineering principle surfaced in these requirements:

> Confirmation before an action commits to the medical/business record.

That strongly validates:

``` text
SemanticPlan
→ validation
→ CommitGate
→ authoritative tool
→ receipt
→ speech
```

------------------------------------------------------------------------

# 26. Buyers Explicitly Care About Double Booking

Another job specifically raised:

-   double booking
-   inconsistent CRM/calendar state
-   API failures
-   unavailable services

These are direct commercial requirements.

Our demo should intentionally break a calendar API and prove that the
agent does **not** hallucinate success.

------------------------------------------------------------------------

# 27. Difficult-Call Engineering

Senior voice work increasingly emphasizes:

``` text
memory
interruptions
latency
tool calling
CRM integration
reliability
natural turn-taking
```

This strengthens the Reliability Audit product.

We should build diagnostic tooling capable of answering:

> Why did this exact call fail?

------------------------------------------------------------------------

# 28. Cheap Infrastructure Is Valuable

One outbound buyer specifically preferred money spent on:

``` text
voice quality
latency
recognition
reasoning
```

rather than an expensive custom dashboard.

This is useful guidance.

Prioritize:

``` text
voice
intelligence
tools
reliability
RAG
```

Keep early admin/reporting functional and simple.

------------------------------------------------------------------------

# 29. SIP Signal

More mature businesses may want AI connected to existing phone
infrastructure.

Future:

``` text
existing PBX
→ SIP
→ VoiceOps
→ employee extension / queue
```

TelephonyAdapter should eventually support:

``` text
Twilio Media Streams
Twilio SIP
generic SIP
```

Do not interrupt current reliability work to build this.

------------------------------------------------------------------------

# 30. Repair-or-Rebuild Jobs Strengthened the Rescue Offer

Later jobs explicitly offered developers the choice to:

``` text
repair existing stack
OR
rebuild it
```

Reported production bugs included:

``` text
timezone conversion
mid-call hangs
bad validation
```

Another optimization job requested:

``` text
interruptions
off-script callers
repetitive-question reduction
latency
edge cases
production hardening
```

with changes first tested on a duplicate/staging version.

That is almost exactly a productized reliability engagement.

------------------------------------------------------------------------

# 31. Multi-Number / DNIS Attribution

A particularly useful requirement involved multiple websites with
different phone numbers.

VoiceOps should store:

``` text
ANI = caller number
DNIS = number called
```

DNIS can map to:

``` text
tenant
brand
location
campaign
greeting
RAG namespace
calendar
credentials
```

This is extremely useful for agencies, franchises and multi-location
businesses.

------------------------------------------------------------------------

# 32. Generic Input Validation

Another production complaint involved agents accepting nonsense
addresses.

StructuredInput should therefore support:

``` text
phone
email
date/time
postal code
address
DOB
membership ID
insurance ID
confirmation code
```

Status:

``` text
INCOMPLETE
POSSIBLE
VALID
AMBIGUOUS
INVALID
```

------------------------------------------------------------------------

# 33. Shared Intake Across Channels

Another buyer wanted the same structured intake logic across web and
voice.

Therefore:

``` text
Structured Intake Schema
          ↓
 Voice | Web | SMS
          ↓
    DialogueState
```

The schema belongs in the business layer, not the channel.

------------------------------------------------------------------------

# 34. Waitlists and Containment

A high-volume healthcare requirement introduced two useful concepts.

## Waitlist orchestration

``` text
appointment cancelled
→ eligible waitlist patients
→ outreach
→ first acceptance
→ atomic booking
```

## Containment rate

``` text
calls fully resolved by AI
--------------------------
all inbound calls
```

Track:

``` text
containment rate
first-call resolution
abandonment
transfer rate
recovery rate
```

------------------------------------------------------------------------

# 35. White-Label Demand Strengthened Again

Agency jobs repeatedly sought ongoing implementation partners rather
than one-off freelancers.

This makes fast provisioning a product requirement.

A future deployment should resemble:

``` text
new tenant
→ template
→ credentials
→ KB
→ regression suite
→ deploy
```

------------------------------------------------------------------------

# 36. Usage and Concurrency Pricing

Another useful market signal was commercial plans packaged around:

``` text
included minutes
concurrent calls
monthly plan
```

rather than token counts.

Therefore meter:

``` text
voice minutes
peak concurrency
STT
LLM
TTS
telephony
storage
tools
```

but sell around:

``` text
capacity
locations
outcomes
support
```

------------------------------------------------------------------------

# 37. Concurrency Is a Production Requirement

Soak testing cannot remain sequential.

Test:

``` text
1
2
3
5
10
25 concurrent calls
```

Measure:

``` text
p95 latency
queue depth
CPU
RAM
socket pressure
DB contention
provider throttling
dropped streams
```

------------------------------------------------------------------------

# 38. Shared Intelligence Across Channels

A later automotive SaaS requirement made this explicit.

Inbound voice, outbound voice and SMS should share:

``` text
customer
dealership/business knowledge
objections
appointment
lead state
next action
```

The architectural rule strongly aligns with VoiceOps:

> AI controls language; application code controls authority.

------------------------------------------------------------------------

# 39. Hybrid AI + Human Answering

Another platform requirement introduced:

``` text
AI_ONLY
AI_FIRST
HUMAN_FIRST
HUMAN_ONLY
```

A future operator interface should show:

``` text
business
caller
transcript
intent
fields
RAG evidence
tool results
recommended action
transfer instructions
```

This opens a future AI-enhanced answering-service product.

------------------------------------------------------------------------

# 40. Cost Pressure Became a Product Requirement

A later buyer wanted to migrate away from managed voice infrastructure
specifically because per-minute cost became too high at expected volume.

This means custom infrastructure can eventually sell on:

``` text
quality
+
reliability
+
lower unit economics
```

Add cost to every benchmark:

``` text
$/minute
$/call
$/successful booking
$/qualified lead
```

------------------------------------------------------------------------

# 41. Buyers Want Proof

Current voice-engineering applications increasingly request:

``` text
live demo
Loom
working phone number
portfolio
production examples
```

Therefore one polished demo is worth more than many unfinished examples.

------------------------------------------------------------------------

# 42. MASTER MARKET CONCLUSION

The opportunity is not:

> Build another AI receptionist.

It is:

> Build the operating system around AI conversations.

VoiceOps should eventually own:

``` text
customer identity
conversation state
business state
RAG
tool authority
transaction safety
CRM
calendar
messaging
outcomes
retries
memory
observability
cost
evaluation
deployment
```

------------------------------------------------------------------------

# 43. Architecture

``` text
Telephony / Messaging
        ↓
Perception
        ↓
DialogueState
        ↓
SemanticPlan
        ↓
Policy + Validation
        ↓
CommitCoordinator
        ↓
Authoritative Tool
        ↓
ActionReceipt
        ↓
SpeechCommitGate
        ↓
TTS / Channel Response
```

Shared:

``` text
Customer Memory
RAG
Outcome Engine
Structured Input
Observability
Cost Metering
Compliance
Tenant Config
```

------------------------------------------------------------------------

# 44. Core Rule

**LLM controls language. Code controls authority.**

The model may propose:

``` text
BOOK_APPOINTMENT
```

Only the authoritative calendar system can prove that it happened.

------------------------------------------------------------------------

# 45. ActionReceipt

Every mutating tool returns a normalized receipt.

``` json
{
  "action_id": "uuid",
  "idempotency_key": "...",
  "operation": "create_booking",
  "status": "SUCCESS",
  "authoritative_id": "provider_object_123",
  "committed_at": "...",
  "provider": "google_calendar"
}
```

No committed language before receipt.

------------------------------------------------------------------------

# 46. Idempotency

Every mutation needs an idempotency key.

Example:

``` text
hash(
 tenant_id,
 customer_id,
 action_type,
 normalized_payload
)
```

Persist:

``` text
key
payload hash
status
provider object
response
timestamps
```

Retries return the previous success rather than creating duplicates.

------------------------------------------------------------------------

# 47. Outbox Pattern

Use for post-call side effects.

``` text
transaction:
  save outcome
  save state
  enqueue outbox
COMMIT

worker:
  CRM
  SMS
  WhatsApp
  Slack
```

Every delivery gets its own idempotency key.

------------------------------------------------------------------------

# 48. Integration Architecture

Interfaces:

``` text
CalendarAdapter
CRMAdapter
MessagingAdapter
WorkflowAdapter
NotificationAdapter
TelephonyAdapter
KnowledgeAdapter
```

Each must have:

``` text
typed schemas
timeouts
retry policy
idempotency
normalized errors
health check
observability
test mode
```

------------------------------------------------------------------------

# 49. INTEGRATIONS TO BUILD --- PRIORITY ORDER

## Tier 1

### Google Calendar

Implement: - availability/free-busy - create - reschedule - cancel -
retrieve - timezone-safe operations - multiple providers/resources

### GoHighLevel

Implement: - contacts - lookup by phone/email - opportunities -
pipelines - tags - custom fields - notes - tasks - appointments - owner
assignment - workflow triggers - DNC/consent state where applicable

### Twilio SMS

Implement: - booking confirmation - missed-call recovery - speed-to-lead
continuation - callback - reminders - opt-out

### WhatsApp

Support direct Meta Cloud API and/or Twilio abstraction.

Implement: - approved templates - booking confirmation - reminders -
missed-call continuation - structured intake - callback - human
handoff - opt-in/out

### n8n

Implement: - inbound webhook - outbound webhook - reusable workflow
templates - signed requests - retry-safe event IDs

### Slack

Implement: - hot lead - transfer briefing - technical failure -
unresolved call - daily summary

------------------------------------------------------------------------

# 50. Tier 2 Integrations

## HubSpot

-   contacts
-   companies
-   deals
-   activities
-   tasks
-   notes
-   owners
-   associations
-   custom properties
-   webhooks

## Microsoft Calendar

Mirror Google Calendar functionality.

## Microsoft Teams

-   escalation
-   QA exception
-   human assist
-   operational alerts

## Make

Generic workflow integration.

## Zapier

Useful commercial compatibility but lower engineering priority than n8n.

------------------------------------------------------------------------

# 51. Tier 3 Vertical Integrations

## Healthcare / Dental

Potential:

``` text
Dentrix
Open Dental
NextGen
athenahealth
FHIR integrations
RingCentral/SIP
```

## Home Services

``` text
ServiceTitan
ServiceM8
Jobber
Housecall Pro
```

## Real Estate

``` text
GoHighLevel
HubSpot
Follow Up Boss
```

Build based on actual contracts after generic adapters are stable.

------------------------------------------------------------------------

# 52. CustomerState

``` text
Customer
├── identity
├── verified phones
├── language
├── preferred channel
├── bookings
├── prior intents
├── unresolved tasks
├── prior outcomes
└── durable facts + provenance
```

Never use raw transcript dumps as authoritative memory.

------------------------------------------------------------------------

# 53. InboundRouteContext

``` text
CallContext
├── ANI
├── DNIS
├── tenant
├── brand
├── location
├── campaign
├── language
└── acquisition source
```

This supports multi-brand/multi-location agencies.

------------------------------------------------------------------------

# 54. StructuredInput

Generic parser framework:

``` text
phone
email
date
time
DOB
postal code
address
membership ID
insurance ID
confirmation code
currency
```

Return:

``` text
INCOMPLETE
POSSIBLE
VALID
AMBIGUOUS
INVALID
```

------------------------------------------------------------------------

# 55. Outcome Engine

``` text
CallOutcome
├── disposition
├── qualification
├── booking
├── transfer
├── follow_up
├── owner
├── failure_class
└── estimated revenue
```

Canonical dispositions:

``` text
INTERESTED
NOT_INTERESTED
WRONG_CONTACT
CALLBACK_REQUESTED
VOICEMAIL
BOOKED
DO_NOT_CALL
NO_ANSWER
QUALIFIED_NOT_BOOKED
TRANSFERRED
DISQUALIFIED
ABANDONED
FAILED_TECHNICAL
FAILED_WORKFLOW
```

------------------------------------------------------------------------

# 56. Outbound Revenue Engine

``` text
lead
→ policy
→ outbound call
→ qualification
→ objection handling
→ booking / transfer
→ disposition
→ CRM
→ retry
→ SMS/WhatsApp
```

Support: - CRM webhook - CSV - list import - API - n8n

------------------------------------------------------------------------

# 57. Callback Continuity

``` text
outbound call missed
→ customer calls back
→ identify customer + campaign
→ restore pending task
→ continue
```

Do not start an anonymous new conversation.

------------------------------------------------------------------------

# 58. Omnichannel

``` text
Customer
        ↓
Shared Task State
 ┌──────┼────────┐
Phone  SMS   WhatsApp
```

All channels use: - same memory - same tools - same policies - same RAG

------------------------------------------------------------------------

# 59. Human Assist

Future:

``` text
AI handles
→ uncertainty / policy / high value
→ human queue
```

Human receives: - transcript - business - customer - state - evidence -
tool receipts - recommendation

------------------------------------------------------------------------

# 60. RAG Requirements

Required:

``` text
tenant filtering BEFORE retrieval
hybrid retrieval
metadata filters
reranking
evidence/provenance
confidence
abstention
query rewriting
caching
latency metrics
```

Never hallucinate: - hours - price - insurance - policies - availability

------------------------------------------------------------------------

# 61. Voice / Latency Instrumentation

Measure:

``` text
speech end
STT usable partial/final
planner start
LLM first token
tool start/end
TTS request
first audio
playout
```

Track p50/p95/p99.

------------------------------------------------------------------------

# 62. Cost Instrumentation

Per call:

``` text
cost_stt
cost_llm
cost_tts
cost_telephony
cost_storage
cost_tools
total_cost
cost_per_minute
cost_per_successful_outcome
```

Run provider combinations as latency/reliability/cost tournaments.

------------------------------------------------------------------------

# 63. Natural Conversation Requirements

-   barge-in
-   deterministic TTS cancellation
-   no zombie audio
-   adaptive endpointing
-   short acknowledgements
-   correction handling
-   repetition avoidance
-   off-script callers
-   noisy callers
-   spelling/digit capture
-   uncertainty escalation

------------------------------------------------------------------------

# 64. Failure Taxonomy

``` text
STT_ERROR
ENDPOINTING_ERROR
DIGIT_CAPTURE_ERROR
RAG_HALLUCINATION
RAG_NO_EVIDENCE
TOOL_TIMEOUT
TOOL_FALSE_SUCCESS
DUPLICATE_ACTION
STATE_DIVERGENCE
TTS_DUPLICATE
TTS_ZOMBIE_AUDIO
BAD_BARGE_IN
DEAD_AIR
CALL_DROP
TIMEZONE_ERROR
INVALID_INPUT_ACCEPTED
```

Every real failure becomes a regression case.

------------------------------------------------------------------------

# 65. Observability

Every call should be reconstructable.

Events:

``` text
connected
STT
turn
semantic plan
RAG
tool request
tool receipt
TTS
playout
interrupt
outcome
workflow
```

------------------------------------------------------------------------

# 66. KPIs

## Technical

``` text
p50/p95/p99 latency
tool success
interruption success
dead air
provider errors
call drops
```

## Business

``` text
booking conversion
qualified lead rate
containment
first-call resolution
transfer
abandonment
recovery
show rate
cost per outcome
```

------------------------------------------------------------------------

# 67. Deployment Templates

``` text
Core Runtime
+ Vertical Template
+ Tenant Config
+ CRM Adapter
+ Knowledge Pack
+ Provider Profile
+ Policy Pack
```

Templates:

``` text
Dental Receptionist
Medical Front Desk
Home Services
Inbound Lead Qualifier
Outbound SDR
Appointment Recovery
Real Estate
```

------------------------------------------------------------------------

# 68. Compliance Policy

Eventually structured fields for:

``` text
DNC
consent
calling window
recording disclosure
AI disclosure
SMS opt-out
retry limits
retention
PHI mode
```

Not prompt text.

------------------------------------------------------------------------

# 69. FIRST DEMO --- Dental

Must show:

1.  real phone call
2.  interruption
3.  RAG answer
4.  appointment intent
5.  real calendar availability
6.  structured input
7.  correction/change
8.  booking
9.  authoritative receipt
10. SMS/WhatsApp
11. CRM
12. outcome
13. timeline
14. latency/cost

## Deliberate failure

Break calendar.

Agent must **not** claim success.

This is one of the most important portfolio moments.

------------------------------------------------------------------------

# 70. SECOND DEMO --- Home Services

``` text
call
→ service
→ address
→ service area
→ urgency
→ quote
→ technician schedule
→ confirmation
→ CRM/job
```

Include DNIS/campaign attribution.

------------------------------------------------------------------------

# 71. THIRD DEMO --- Outbound SDR

``` text
lead arrives
→ immediate call
→ qualify
→ objection
→ book
→ disposition
→ CRM
→ follow-up
```

Include callback continuity.

------------------------------------------------------------------------

# 72. FOURTH DEMO --- Human Call QA

``` text
human call
→ transcript
→ rubric
→ CRM verification
→ exceptions
→ Slack/Teams
```

This creates a separate product category.

------------------------------------------------------------------------

# 73. UPWORK SERVICES TO SELL

## 1. Production AI Voice Receptionist

Keywords:

``` text
AI Voice Agent
AI Receptionist
Retell
Vapi
Twilio
ElevenLabs
GHL
n8n
```

## 2. Voice Agent Rescue / Reliability Audit

Sell:

``` text
latency
interruptions
call drops
tool failures
RAG
false confirmations
edge cases
```

## 3. GHL + Voice + n8n Automation

## 4. Dental / Medical AI Front Desk

## 5. Home Services AI Receptionist

## 6. Outbound AI SDR / Speed-to-Lead

## 7. AI Call QA / Receptionist Auditor

## 8. White-Label Voice AI Engineering

------------------------------------------------------------------------

# 74. Upwork Search Terms

``` text
AI voice agent
AI receptionist
Retell AI
Vapi
Twilio voice AI
ElevenLabs agent
GoHighLevel voice AI
GHL AI
n8n voice
voice agent latency
voice agent optimization
voice agent developer
AI phone agent
AI appointment setter
AI SDR
outbound AI calling
dental AI receptionist
medical AI receptionist
HVAC AI receptionist
ServiceTitan AI
ServiceM8 AI
HubSpot voice agent
WhatsApp AI agent
Twilio SIP AI
EHR voice AI
Retell optimization
Vapi optimization
voice agent troubleshooting
```

------------------------------------------------------------------------

# 75. Portfolio Assets

Before aggressive applications:

``` text
live demo number
Loom
architecture diagram
call timeline
latency benchmark
cost benchmark
tool receipt
failure demo
RAG evidence
WhatsApp/SMS result
CRM screenshot
GitHub README
case study
```

------------------------------------------------------------------------

# 76. Product Packages

## Receptionist MVP

-   inbound
-   FAQ
-   lead capture
-   calendar
-   SMS
-   CRM

## Production VoiceOps

Add: - RAG - structured input - receipts - recovery - analytics -
regression

## Multi-Location / Agency

Add: - templates - DNIS - multiple locations - campaign attribution -
concurrency - usage

## Custom / Regulated

Add: - SIP - custom backend - healthcare - compliance - PMS/EHR/ERP
adapters

------------------------------------------------------------------------

# 77. EXACT TODO ORDER

## P0 --- Reliability first

-   [ ] Eliminate duplicate/zombie speech.
-   [ ] Make interruption cancellation deterministic.
-   [ ] Finish SpeechCommitGate.
-   [ ] Finish SemanticPlan/CommitCoordinator.
-   [ ] Authoritative ActionReceipt for tools.
-   [ ] Fix tenant filtering before vector retrieval.
-   [ ] Remove global model/config mutation.
-   [ ] Ensure streaming TTS begins without unnecessarily waiting for
    full model completion.
-   [ ] Fix calendar/business-hours truth mismatch.
-   [ ] Deterministic event timeline.
-   [ ] Failure taxonomy.
-   [ ] Regression tests from every known failure.

## P1 --- Core intelligence

-   [ ] Generic StructuredInput.
-   [ ] Evidence-backed RAG contract.
-   [ ] CustomerState.
-   [ ] Outcome Engine.
-   [ ] InboundRouteContext/DNIS.
-   [ ] Provider adapter interfaces.
-   [ ] Latency tournament.
-   [ ] Cost telemetry.
-   [ ] Sequential soak.
-   [ ] Concurrent soak.

## P2 --- Commercial integration pack

-   [ ] Google Calendar.
-   [ ] GoHighLevel.
-   [ ] Twilio SMS.
-   [ ] WhatsApp.
-   [ ] n8n.
-   [ ] Slack.
-   [ ] Outbox.
-   [ ] Idempotency registry.
-   [ ] retries.
-   [ ] reconciliation.

## P3 --- Dental portfolio

-   [ ] realistic KB.
-   [ ] booking/reschedule/cancel.
-   [ ] SMS/WhatsApp.
-   [ ] CRM.
-   [ ] outcomes.
-   [ ] deliberate failure.
-   [ ] live number.
-   [ ] Loom.
-   [ ] architecture assets.

## P4 --- Revenue features

-   [ ] HubSpot.
-   [ ] Microsoft Calendar.
-   [ ] Teams.
-   [ ] Make.
-   [ ] missed-call recovery.
-   [ ] speed-to-lead.
-   [ ] outbound state machine.
-   [ ] callback continuity.

## P5 --- Productization

-   [ ] tenant schema.
-   [ ] vertical templates.
-   [ ] deployment versions.
-   [ ] secret isolation.
-   [ ] per-tenant KB.
-   [ ] provider profiles.
-   [ ] usage metering.
-   [ ] DNIS routing.
-   [ ] white-label provisioning.

## P6 --- Home Services

-   [ ] address parser.
-   [ ] geocoder.
-   [ ] service area.
-   [ ] ServiceM8/Jobber.
-   [ ] quote policy.
-   [ ] technician schedule.

## P7 --- Higher-end

-   [ ] SIP.
-   [ ] multilingual profiles.
-   [ ] healthcare workflows.
-   [ ] human assist.
-   [ ] compliance policy.
-   [ ] evidence manifest.
-   [ ] provider router.

------------------------------------------------------------------------

# 78. Integration Definition of Done

Every mutation must have:

-   [ ] typed schema
-   [ ] validation
-   [ ] normalized IDs
-   [ ] idempotency
-   [ ] timeout
-   [ ] retry classification
-   [ ] safe retry
-   [ ] receipt
-   [ ] normalized error
-   [ ] event log
-   [ ] reconciliation
-   [ ] fixture
-   [ ] provider failure test
-   [ ] duplicate request test
-   [ ] race test where relevant

------------------------------------------------------------------------

# 79. Suggested Repository Shape

``` text
src/
  conversation/
    state/
    planning/
    policy/
    commit/
    outcomes/
    memory/

  voice/
    perception/
    endpointing/
    interruption/
    playback/

  rag/
    retrieval/
    reranking/
    evidence/

  structured_input/
    parsers/
    validators/

  integrations/
    calendar/
    crm/
    messaging/
    workflow/
    notification/
    telephony/

  platform/
    tenancy/
    routing/
    idempotency/
    outbox/
    retries/
    reconciliation/
    metering/
    compliance/

  observability/
    events/
    latency/
    cost/
    failures/

  evaluation/
    replay/
    regression/
    concurrency/
    difficult_calls/

templates/
  dental/
  medical/
  home_services/
  outbound_sdr/
```

------------------------------------------------------------------------

# 80. What NOT to Build Yet

-   [ ] giant Salesforce-like UI
-   [ ] custom ERP
-   [ ] dozens of CRM adapters
-   [ ] full healthcare compliance program before demand
-   [ ] direct SIP before core telephony is stable
-   [ ] every language
-   [ ] five demos simultaneously
-   [ ] autonomous LLM writes without commit control
-   [ ] transcript dumps masquerading as memory
-   [ ] beautiful billing UI before unit economics work

------------------------------------------------------------------------

# 81. Claude Code Immediate Handoff

Claude Code should treat this as the commercial target, but **must not
implement everything simultaneously**.

Sequence:

``` text
1. Audit current reliability work.
2. Finish blockers.
3. Stabilize event schema.
4. Finish CommitGate + ActionReceipt.
5. Generic idempotency.
6. Generic StructuredInput.
7. Fix/benchmark RAG.
8. Add CustomerState.
9. Add Outcome Engine.
10. Add latency + cost telemetry.
11. Google Calendar.
12. Twilio SMS.
13. GoHighLevel.
14. WhatsApp.
15. n8n.
16. Slack.
17. Dental demo.
18. Failure tests.
19. Concurrency tests.
20. Portfolio assets.
```

For every feature:

> Can this work for dental, home services, outbound sales and
> white-label agencies without forking the runtime?

If not, move vertical-specific behavior into configuration, templates or
adapters.

------------------------------------------------------------------------

# 82. The Upwork Strategy

Do not market ourselves merely as:

> AI developer.

Use three parallel wedges.

## Wedge A --- New systems

> Production AI voice receptionist with CRM, scheduling, SMS/WhatsApp
> and reliable tool execution.

## Wedge B --- Broken systems

> I diagnose and fix slow, unnatural or unreliable Retell/Vapi/Twilio
> voice agents.

## Wedge C --- Agencies

> White-label technical implementation partner for repeat voice-AI
> client deployments.

This dramatically increases the set of jobs the same repository can win.

------------------------------------------------------------------------

# 83. Final Thesis

The accumulated research points consistently toward one product
direction.

Do **not** build:

``` text
a better prompt wrapped around a phone number
```

Build:

``` text
Conversation OS
+
provider-independent voice runtime
+
authoritative business state
+
transaction-safe tools
+
evidence-backed RAG
+
persistent customer memory
+
phone/SMS/WhatsApp continuity
+
CRM/calendar/workflow integrations
+
outbound revenue automation
+
observability
+
cost control
+
failure replay
+
vertical templates
```

The first commercial proof should be an exceptionally polished dental
receptionist.

The first parallel service should be voice-agent rescue/reliability
engineering.

The second vertical should be home services.

The next major product mode should be outbound speed-to-lead.

The eventual platform should support agencies deploying many tenants,
many phone numbers, multiple channels, optional human participation and
interchangeable providers.

**The moat is not the voice model.**

**The moat is making AI conversation behave like dependable production
software while still sounding natural.**
