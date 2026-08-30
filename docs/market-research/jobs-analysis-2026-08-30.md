# Freelance market analysis — Aug 2026

**Source:** `Jobs.txt` (Upwork scrape, ~124 posts, rolling 4-week window ending 2026-08-30)
**Purpose:** Validate current receptionist product direction + identify adjacent agent products for post-demo pivot.

## TL;DR

**Current receptionist direction is market-validated.** Don't pivot mid-build.

- Voice receptionist requests = 22 direct posts, our stack fits near-perfectly
- Buyers name our exact features: per-tenant knowledge, staff-editable prompts, HIPAA/BAA, calendar sync without race conditions, "answer in 2 rings + book in ≤10s"
- Median serious build budget: **$400-$2K fixed** or **$25-60/hr**
- Highest budgets in healthcare vertical ($2K-$10K)

**Top 3 next products after receptionist demo:**
1. WhatsApp Booking Agent (1 week, 85% stack reuse)
2. White-label multi-tenant agency SaaS (2-3 weeks, uses Terraform module)
3. Clinic voice receptionist with EHR write-through (1 week per EHR)

**Avoid:** $5-$50 "expert" builds with 100+ requirement bullets (arbitrage bait), commission-only SDR postings, "fix my GHL AI agent for $20" (inherit platform blame).

---

## Full taxonomy (124 posts)

| Bucket | Count | Median budget | Our fit |
|---|---:|---|---|
| Voice-agent / IVR / receptionist (inbound) | 22 | $400-$2K fixed | ★★★★★ |
| Workflow automation (n8n/Make/Zapier) | 19 | $5-$800 | ★ |
| RAG / knowledge-base agents | 17 | $200-$1.7K | ★★★ |
| Customer support chat (text) | 10 | $100-$500 | ★★★★ |
| Sales / outbound / SDR | 9 | $150-$10K | ★★★★ |
| WhatsApp booking / scheduling | 8 | $100-$700 | ★★★★★ |
| Data extraction / scraping / OCR | 7 | $80-$35/hr | ★ |
| Content / SEO / social-media | 7 | $50-$850 | ★★ |
| CRM/GHL setup & fix | 6 | $95-$500 | ★★ (config work, not new build) |
| Personal assistant / email triage | 5 | $15-30/hr | ★★★ |
| Coding-agent / dev-tooling | 5 | mixed | ★ |
| Off-topic (Unity XR, Godot, ML) | 9 | — | ✗ |

**Real-agent-buildable share:** ~84% (104/124).

## Stack keyword density (whole file)

| Tool | Mentions |
|---|---:|
| n8n | 110 |
| GoHighLevel (GHL total) | 139 |
| Retell | 56 |
| Zapier | 49 |
| WhatsApp | 36 |
| Vapi | 24 |
| Twilio | 24 |
| AWS Bedrock | 24 |
| LangChain | 19 |
| Airtable | 18 |
| ElevenLabs | 18 |
| HubSpot | 14 |
| LiveKit | 13 |
| pgvector | 13 |
| LangGraph | 13 |
| RingCentral | 12 |
| Calendly | 11 |
| Deepgram | 6 |
| Nextech EHR | 5 |

**Reading:** n8n dominates orchestration, Retell dominates voice-agent competition, GHL dominates CRM in the buyer base. Deepgram appearing only 6x (vs Retell 56x) suggests most buyers don't know or care about STT provider — they buy the wrapper.

## 12 highest-signal individual opportunities

| # | Title | Budget | Fit |
|---|---|---|---|
| 1 | AI Voice Agent Build (Vapi/Retell + Twilio), EHR Scheduling and CRM Integration | $2K fixed | ★★★★★ near-perfect |
| 2 | AI Voice Receptionist - No Latency | hourly | ★★★★★ matches our roadmap |
| 3 | Voice AI Specialist - Overflow Phone for San Diego Spa (Vagaro) | $700 fixed | ★★★★ needs live-listen UI |
| 4 | Retell AI Expert - Optimize Production Voice Agent Latency | hourly | ★★★ consulting only |
| 5 | WhatsApp AI Agent Developer — Clinic Booking (Plato Medical API) | $500 range | ★★★★★ needs WhatsApp adapter |
| 6 | AI Voice Agent Expert for European Real Estate | $500 | ★★★★★ (Syed's active freelance) |
| 7 | AI Voice Receptionist + Lead Gen Builds | $500/build repeat | ★★★★★ repeat contract |
| 8 | AI Engineer for Recruitment Automation / HubSpot | $100 baseline | ★★★★ HubSpot + summary |
| 9 | WhatsApp AI Agent for Veterinary Clinic | $500 | ★★★★★ multi-location vertical |
| 10 | Senior AI Engineer - RAG pipeline with citations, 4K PDFs | $45-65/hr, 3-6mo | ★★★ adjacent |
| 11 | Senior Backend Engineer, Predictive Dialer Platform | $10K + 12mo | ★★ months of new telephony |
| 12 | AI-Powered BPO Outbound Calling Platform | hourly, 10-40 concurrent | ★★★ dialer additions |

## Top 8 new products ranked by (market demand × stack reuse)

### 1. WhatsApp Booking Agent — vertical starter kits
- **Signal:** 8 booking posts, WhatsApp in 36 lines, verticals: physio, vet, dental, tourism, real-estate, rehab clinic
- **Budget:** $100-$700 per build, repeatable
- **Reuse:** ~85% — brain, tools, calendar, CRM sinks, session manager, annotation dashboard
- **New:** WhatsApp Cloud API adapter (~3 days), voice-note transcription in message pipeline (STT already there), phone-number-identity-resolution
- **Effort to demo:** 1 week

### 2. Voice Overflow Receptionist (rings humans first, catches misses)
- **Signal:** Spa post + ~5 more (dental, HVAC "missed call = $300-$5K job")
- **Reuse:** Full voice pipeline, escalation rules already in tools framework
- **New:** Overflow-forwarding docs per PBX (Dialpad/RingCentral/GHL/Snet), supervised-launch live-listen UI, per-state compliance disclosure library (CA AB 2905/SB 1001/AB 489 already documented)
- **Effort to demo:** 1 week + 3 days for live-listen viewer

### 3. Multi-Tenant Receptionist SaaS for Agencies (white-label)
- **Signal:** Multiple explicit "give me source + white-label" asks
- **Reuse:** Voice pipeline + Terraform module + annotation dashboard as client-facing view
- **New:** Sub-account model, per-tenant number provisioning, agency billing, per-tenant prompt-editor UI ("staff must edit without a ticket" — repeated 6x)
- **Effort to demo:** 2-3 weeks

### 4. Clinic Voice Receptionist with EHR Write-Through (HIPAA-safe)
- **Signal:** $2K Nextech aesthetics, physio Plato, vet, dental SaaS — healthcare is highest-budget subvertical
- **Reuse:** Voice + tools + escalation
- **New:** BAA-signable stack review (HIPAA compliance_mode = task #75), EHR adapter framework (Cliniko simplest, then Nextech, Jane, Vagaro, SimplePractice, Halaxy), medical-guardrail prompt library, configurable recording-consent per US state
- **Effort to demo:** 1 week for one EHR; each additional adapter ~2-3 days

### 5. Outbound Cold-Call Qualification Agent (Vapi/Retell alternative)
- **Signal:** 9 direct posts + $10K predictive-dialer + BPO platform post
- **Reuse:** Voice pipeline, tools, brain (script/persona per campaign)
- **New:** Bulk-dialer controller with pacing, AMD (answering-machine detection), DNC list, per-campaign eval/reporting, Slack alert integration
- **Effort to demo:** 2 weeks for batch-dialer with basic AMD; predictive/skills-routed is months

### 6. RAG "Business Brain" bolt-on
- **Signal:** 17 RAG posts, several want "brain for the business"
- **Reuse:** Brain is already per-tenant knowledge
- **New:** Document ingestion UI (drag-drop PDF/DOCX), citation surfacing in annotation dashboard, eval harness for accuracy
- **Effort to demo:** 1 week to expose what we already have as standalone Q&A

### 7. Lead-Reactivation Voice+WhatsApp Loop (real-estate / home-services)
- **Signal:** UK estate agency $500 repeatable, "Automation Specialist" real-estate posts, HubSpot recruitment
- **Reuse:** Voice + brain + session manager + tools (CRM, calendar)
- **New:** Dormant-lead selection query per CRM, multi-touch cadence engine, WhatsApp adapter (shared with #1)
- **Effort to demo:** 1.5-2 weeks

### 8. Post-Call Summarizer / Annotation Studio (productized dashboard)
- **Signal:** "AI Transcript Summary MVP for Sales/Support" $850, HubSpot recruitment ("save summary back to HubSpot"), real-estate ("every call transcribed → summarized → written back")
- **Reuse:** Annotation dashboard IS already this
- **New:** Structured extraction schema per vertical, CRM write-back connectors (HubSpot, GHL, Service Fusion, Markate)
- **Effort to demo:** ~1 week to package the dashboard + 2 CRM connectors

## Anti-patterns to avoid

1. **Fixed-price $5-$50 "expert" builds with 100+ requirement bullets** — arbitrage bait. Either deliver junk or anchor at $30 forever.
2. **"Fix my GHL AI voice agent for $20"** — unbounded scope inside someone else's SaaS + you inherit platform blame.
3. **Commission-only appointment setter / SDR postings** ($0 base + 25% recurring) — high-noise, no proof they can close.
4. **"Start proposal with [emoji]" combined with $5-$30 budgets** — filtering theater from clients who don't respect freelancer time.
5. **Off-topic "AI" posts** — Unity/Godot games, PPG signal ML, plant-image annotation at $3/hr. Skip on sight.

## What this means for our current sprint

**Continue current direction unchanged.** Every feature in flight maps to a stated market need:

| Current work | Market signal |
|---|---|
| Voice pipeline + Deepgram + ElevenLabs | 22 receptionist posts |
| Discovery drill orchestrator | Buyers repeatedly ask for "asks the right questions" |
| Slot-capture sub-agents (phone/email/name/date/yes_no) | Buyers name these fields explicitly |
| GHL sink | 139 GHL mentions — dominant CRM in buyer base |
| Annotation dashboard + LK judges | 4 posts explicitly ask for post-call transcript + summary + write-back |
| Terraform per-client module | Multiple "white label / give me source" asks |
| HIPAA compliance_mode (#75) | Healthcare is highest-budget subvertical |
| Google Calendar sink | Every booking post mentions calendar sync |

**Finish these, demo, THEN pick a next product.**

Recommended next products to plan (not build) while finishing receptionist:
- WhatsApp adapter (highest volume, shortest effort)
- White-label agency mode (highest MRR ceiling)
- EHR adapter framework (highest per-deal value)

## Referenced files

- Source: `Jobs.txt` (repo root, gitignored — market intel, not codebase)
- This analysis: `docs/market-research/jobs-analysis-2026-08-30.md`
- Related: `docs/AWS-DEPLOYMENT-BRIEF-2026-08-29.md`, `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`
