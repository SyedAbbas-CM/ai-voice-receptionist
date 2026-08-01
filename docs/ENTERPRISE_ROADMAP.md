# Enterprise Roadmap — Path from Prototype to First Paying Customer to First Enterprise Deal

**As of:** 2026-08-01
**Source:** Gap analysis against ~15 competitors + real 2026 buyer requirements
**Bottom line:** 4 weeks to first paying SMB. 8 weeks to 10 customers. 6-9 months to first enterprise (SOC 2 elapsed-time is the bottleneck).

---

## TL;DR — The 5 Biggest Gaps

Ranked by revenue impact per week of engineering effort.

| # | Gap | Blocks | Revenue impact | Effort |
|---|---|---|---|---|
| 1 | **No multi-tenant control plane.** One deployment = one business. No self-serve onboarding, no per-tenant billing/isolation. | Every paying customer past #1. Enterprise procurement rejects on first security questionnaire. | Unlocks $0 → $10k MRR | 3-4 wks |
| 2 | **No SOC 2 Type II + BAA.** Enterprise buyers demand these BEFORE the technical demo. 84% of orgs cannot pass an AI-agent compliance audit today. | Any clinic, mid-market restaurant chain, deal >$1k/mo. | Unlocks $2k → $5k+ deals | 6-12 mo elapsed, ~2 wks eng, $15-40k audit spend |
| 3 | **No POS / EHR / calendar write-back.** Tools mutate in-memory only. Real customers need Toast/Square/Clover + athenaOne/Epic/eClinicalWorks. | Every restaurant deal past demo. Loman ($299/mo) and Hostie ($199/mo) built their pricing on this. | Doubles ACV per location | 2-3 wks per integration |
| 4 | **No per-tenant recording / playback / analytics UI.** We log to SQLite. Every $199+/mo competitor ships a tenant portal. | Every SMB renewal. Owner-visibility drives retention. | Cuts churn from ~50%/mo → <10% at $199 tier | 2 wks |
| 5 | **Adversarial harness at 18/34 pass** (47%). A hallucinated policy on a live call = Air Canada scenario. 518+ US AI-hallucination cases since Jan 2025. | Every outbound use case + every enterprise conversation. | Blocks TCPA-safe outbound + torpedoes demos | 2 wks |

Everything else is downstream of these five.

---

## The Market Shape (Honest Read)

Three tiers, each with different economics:

- **Horizontal platforms** (Retell $0.07/min, Vapi $0.05+pass-through, Bland, Synthflow, ElevenLabs Agents): race to the bottom, target devs. Do not chase — companies with $50-200M raised will win the latency+price war.
- **Vertical SaaS** (Slang $399-599/mo/loc, Loman $199-299, Hostie $199+, Assort $1.5-10k, Newo): pricing held from 2024 to 2026. Where we should play.
- **Enterprise contact-center** (PolyAI, Parloa $3B, Decagon $4.5B, Sierra $15.8B): custom $150k+/yr. Not our starting market.

**Only 1 in 10 customer-service interactions is fully automated by voice AI as of 2026.** 90% of restaurant + clinic phone volume is still humans. Real wedge exists.

---

## Enterprise Compliance Requirements

Buyers do compliance-review-first, technical-demo-second in 2026. A vendor who cannot produce SOC 2 Type II + sub-processor list + DPA template under NDA within 48 hours rarely advances past stage one.

| Requirement | We have | We need | Cost/effort |
|---|---|---|---|
| **SOC 2 Type II** | Nothing formal | Report from A-LIGN / Prescient / Sensiba / Schellman, or Vanta / Drata rollout | 6-12 mo elapsed, $15-40k, ~4 wks eng |
| **HIPAA BAA (clinic)** | None | BAAs across all 5 subprocessors: Deepgram (enterprise), Cartesia (enterprise), Twilio (free with request), Vapi ($1k/mo add-on), and — critically — an LLM that offers a BAA (Groq does NOT). Route clinic to Azure OpenAI / Anthropic Enterprise / on-prem Qwen. | 2 wks negotiation, $1.5-3k/mo add-on cost |
| **PCI DSS 4.0 (restaurant, card-on-file)** | We take no cards | Route to POS-hosted checkout link via SMS — avoid touching PANs entirely | 3-5 days |
| **GDPR / CCPA** | Nothing formal | DPA template, deletion API, transcript-purge cron | 1 wk |
| **TCPA (outbound)** | Nothing | Consent capture, state+federal DNC scrubbing, calling-window enforcement (8am-9pm local), AI disclosure preamble, abandonment tracker | 2 wks. **Do NOT ship outbound until this exists.** |
| **99.9% uptime SLA** | Router has 8s timeout + cool-down; single-region | Multi-region deploy, dual-path telephony, provider health-check dashboard, on-call rota | 3-4 wks |
| **On-prem / VPC option** | ✅ Already have: 2× 3090 + Ollama + 186 GB cached models | Package as Docker Compose + Helm chart with signed BAA rider | 2 wks packaging |
| **SSO (SAML/OIDC)** | None | Okta / Azure AD / Google Workspace via WorkOS | 1 wk |
| **RBAC** | None | Admin/manager/viewer/auditor per tenant | 3 days |
| **Immutable audit log** | In-memory OTel spans | Append-only Postgres with row-hash chain, or S3 Object Lock | 1 wk |

### The "Five-BAA Problem"

Every layer processes PHI in a clinic call. To sign a covered-entity BAA we need signed BAAs from all subprocessors:

| Layer | Vendor | BAA available? | Action |
|---|---|---|---|
| Telephony | Twilio (via Vapi) | ✅ free with request | Sign it |
| Orchestration | Vapi | ✅ $1k/mo add-on | Upgrade or replace with self-host |
| STT | Deepgram | ✅ enterprise tier | Upgrade |
| **LLM** | **Groq** | ❌ **not offered as of Aug 2026** | **Blocker.** Route clinic traffic to Azure OpenAI, Anthropic Enterprise, or on-prem Qwen/Llama |
| TTS | Cartesia | ✅ enterprise tier | Upgrade |

Recommendation: ship a HIPAA-mode toggle that swaps Groq → Azure OpenAI gpt-4o-mini, or uses our on-prem PC. **The on-prem path is our defensible wedge.**

---

## Missing Features Ranked by Revenue Impact

### Tier A — Ship in next 30 days (unlocks first paying customer)

| Feature | Why | Effort |
|---|---|---|
| Business-profile self-serve UI | Every new customer is an eng-day today. Cannot scale past ~3 tenants. | 1 wk |
| Multi-tenancy (data model + auth + isolation) | Foundation for everything else. | 2 wks |
| Per-tenant call recording + transcript playback UI | Owner-visibility drives renewal. Without it: 40-60% churn at $199/mo. | 1 wk |
| Basic analytics dashboard per tenant | Same as above. | 4 days |
| Adversarial harness to >30/34 pass | 47% pass rate = real Air Canada scenario waiting. | 2 wks |
| Warm-transfer to human with whisper-brief | Standard of care in 2026. Every deal asks "what if it doesn't know?" | 3 days |

### Tier B — Ship in next 60 days (unlocks 10 customers + first chain deal)

| Feature | Why | Effort |
|---|---|---|
| Toast + Google Calendar + athenaOne write-back | Turns a $199 "voicemail replacement" into a $399 "phone concierge" | 2-3 wks per integration |
| Post-call SMS follow-up (booking confirmation, no-show reminder) | Cheapest lift to satisfaction score | 3 days |
| Voicemail / AMD detection for outbound | ML-based AMD 93-97% vs tone-only 85-90%. Unblocks outbound category. | 1 wk |
| DNC scrubbing + calling-window guard + AI disclosure preamble | TCPA table stakes | 1 wk |
| SSO (WorkOS) + RBAC | Any deal >$1k/mo asks | 1 wk |
| Immutable audit log | SOC 2 evidence + procurement | 1 wk |
| Spanish-language support | Deepgram + Cartesia both support. Immediate US market expander. | 1 wk |
| Own-voice cloning per tenant (on-prem Qwen3-TTS) | Hostie's differentiator. We have the model cached. | 2 wks |

### Tier C — Ship in next 90 days (unlocks first $5k+/mo enterprise)

| Feature | Effort |
|---|---|
| SOC 2 Type II scope + Vanta/Drata rollout | 4 wks eng + $15-40k audit |
| Signed BAAs across the stack + Groq-swap for clinics | 2 wks |
| On-prem "sovereign" deployment package (Docker Compose + Helm) | 2 wks |
| Salesforce + HubSpot CRM push (post-call summary + lead + outcome) | 1 wk per CRM |
| Real-time sentiment + escalation trigger | 1 wk |
| Multi-region deploy (US-East, US-West, EU-West) + tenant pinning | 3 wks |
| Per-tenant billing (Stripe metered) | 1 wk |
| Alerting + on-call runbook (PagerDuty + provider-outage playbooks) | 1 wk |
| Voice cloning UI + consent-capture guardrails | 1 wk |
| PCI-scope reduction: SMS-checkout hand-off | 3-5 days |

---

## The 90-Day Roadmap (One Engineer, Linear)

```
Wk 1-2   A2 multi-tenancy + A5 harness closure (parallel)
Wk 3     A1 self-serve profile UI
Wk 4     A3 recording+playback UI, A4 analytics, A6 warm transfer, B2 SMS
         ← v1 launch, sign first customer at $149/mo intro pricing
Wk 5-6   B1a Toast integration + B7 Spanish
Wk 7     B1b Google Calendar + B5 SSO/RBAC
Wk 8     B3+B4 AMD + TCPA guards (unlocks outbound), B6 audit log
         ← 10-customer target
Wk 9-10  B8 own-voice tenant pipeline, C3 on-prem packaging
         (parallel with SOC 2 admin work)
Wk 11    C1 SOC 2 rollout + C7 metered billing + C10 SMS-checkout
Wk 12    C2 BAAs + C4 CRM push + C5 sentiment escalation
Wk 13+   C6 multi-region, C8 alerting, C9 voice-clone UI
         ← enterprise pilots begin
```

Two engineers halves the calendar. Three does not — SOC 2 audit + BAA negotiation are elapsed-time bottlenecks.

---

## Positioning Statement

> **"The receptionist that runs on your own hardware."**
>
> For dental groups, medical practices, and restaurant chains with an IT lead who cares where the audio goes: a HIPAA/PCI-scope-reduced voice receptionist that answers, books, transfers, and files the order into your POS or EHR — deployed on your VPC or on a $2k GPU box in your closet. Owner-clonable brand voice included, so the phone sounds like *you*.

Eliminates 90% of horizontal competitors on positioning alone.

Pricing model: **$399-799/mo/loc as vertical SaaS**, run horizontal cost at ≤$0.03/min via on-prem Ollama when tenant opts in. Capture the margin, reinvest in integrations. Compete with Loman/Hostie, not Retell/Vapi.

---

## Failure-Mode Preemption Checklist

Every item below is a documented 2024-2026 incident type.

### Hallucination liability (Air Canada scenario)

*Moffatt v. Air Canada* (Feb 2024, Canadian tribunal): airline liable for chatbot's fabricated bereavement-refund policy. Court rejected the "chatbot is a separate legal entity" defense. 518+ US AI-hallucination cases since Jan 2025.

- [ ] Every price / policy / clinical detail must come from a retrieved source. Reject LLM-invented facts.
- [ ] "I don't know, let me connect you" is a first-class refusal path — track its usage.
- [x] Adversarial harness exists — need to close gap to >30/34 pass.
- [ ] Recording + transcript retained per tenant 90 days minimum for evidence.
- [ ] MSA disclaimer: "Customer is responsible for confirming any policy relayed by the agent."

### Silent-provider-outage cascade

- [x] Per-provider 8s timeout + 30s cool-down (in router).
- [ ] Health-check dashboard in `/graph` showing p50/p95/p99 per provider live.
- [ ] Silence budget — if TTS first-audio > 800ms and STT last-partial > 1200ms, force fallback provider mid-call.

### PII in transcripts / recordings (BAA breach)

- [x] PII redaction on transcripts.
- [ ] Redaction runs *before* database insert, not after.
- [ ] Recordings segregated per tenant with KMS-per-tenant.
- [ ] 30 / 90 / 365 day auto-purge policy configurable per tenant.

### TCPA outbound violation ($500-1500 per call)

- [ ] Ship B3+B4 before any outbound-capable customer touches production.
- [ ] Default `require_consent_v1 = True` that tenants cannot disable from the UI.

### Voice-cloning IP / publicity rights

- [ ] For own-voice cloning, require signed consent artifact (tenant uploads video of the person consenting). Store indefinitely. Refuse clones without it.

---

## The Bottom Line

**Strong prototype today. 6-8 weeks of focused work from first paying SMB. 6-9 months from first $5k+/mo enterprise deal.**

Enterprise is mostly compliance + multi-tenancy + integrations work — not core AI research. The AI parts (router, sanitizer, guard, adversarial harness, cloned voice on-prem) are already stronger than most competitors.

**Ship in this order:** multi-tenancy → tenant UI → first integration → SOC 2 process kickoff. That's the shortest path to revenue.
