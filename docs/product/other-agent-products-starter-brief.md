# Starter brief — Other agent products beyond the receptionist

**Purpose:** paste this as the first message in a fresh Claude Code chat when you spawn the "other-agent-work" session. Keeps that chat from re-deriving context we've already built.

---

## What we have (the receptionist codebase)

**Repo:** `/Users/az/Desktop/Receptionist Agent`, deployed at `https://agent.eternalconquests.com` (AWS Lightsail, us-east-1). Branch `feat/architectural-networking` == `main` == what's live. Deploy = `./scripts/deploy.sh`.

**Live stack (all working, ~11K LOC):**

| Layer | Component | Reusable for other agents? |
|-------|-----------|----------------------------|
| Telephony | Twilio Media Streams (WSS bidirectional) | ✓ voice/sales/booking |
| STT | Deepgram Flux (native EagerEndOfTurn) + tenant keyterm boost | ✓ any voice agent |
| TTS | ElevenLabs Flash v2.5 + LK sub-agent scoped prompts | ✓ any voice agent |
| LLM router | OpenAI + Groq + Gemini + OpenRouter + Router w/ cooldown | ✓ any agent |
| Core agent brain | `packages/core_agent/` — turn intent, next-action policy, discovery drill orchestrator, slot capture wiring | ✓ any agent |
| Slot capture sub-agents | phone, email, name, date, yes_no (LK task_group pattern) | ✓ any agent asking users for fields |
| Discovery drill | `packages/dialogue/context_discovery.py` — LK task_group adaptation for "ask 3 questions before doing X" | ✓ any complex intent agent |
| Session manager | multi-tenant sessions + transcripts + bookings + call_events | ✓ any agent |
| Backend | FastAPI + SQLAlchemy + Alembic + SQLite (Postgres-ready) | ✓ any agent |
| Multi-tenancy | tenant_guard middleware, per-tenant business profiles, phone-number → tenant mapping | ✓ any agent |
| Auth | ADMIN_TOKEN bearer + password login + HMAC cookies | ✓ any admin surface |
| Annotation dashboard | `/admin/annotate` — human review + verdict + tags + gold flag | ✓ any agent needing QA |
| Trace views | `/trace/{call_id}` humanness + `/admin/calls/{id}/incident` raw | ✓ any agent debugging |
| CRM sinks | GHL + HubSpot + Pipedrive + Google Sheets stubs (need tokens) + Composite chain | ✓ any agent writing to CRM |
| Calendar sinks | Google Calendar + FakeCalendar + OutboxCalendar (deferred sync) | ✓ any booking agent |
| Voice pipeline patterns (LK-stolen) | BargeInPolicy + FillerScheduler + TfidfLoopDetector + backchannel grace | ✓ any voice agent |
| Deploy tooling | `scripts/deploy.sh` (rsync+restart+health), GitHub Actions, `trace_call.sh` | ✓ any deployed agent |
| Terraform | Per-client Fargate stack module (written, not applied) | ✓ any deployed agent |
| Product-lead subagent | `.claude/plugins/product-lead/` — vertical-agnostic product-thinking agent | ✓ any agent product work |

## What we've learned that generalizes

- **"Discovery drill" pattern**: caller says something ambiguous ("follow-up") → orchestrator asks N questions before advancing. Ports to any agent that must gather context before acting.
- **"Sub-agent narrow scope" pattern (LK steal)**: for a specific slot (phone, email, name), swap the wide system prompt for a 200-line narrow-scope one. Small-model reliability jumps dramatically.
- **"Outbox with deferred sync" pattern**: mutations write locally first, then a background worker syncs to external systems when creds are configured. Perfect for CRM writes, calendar events, SMS sends.
- **"Callback in policy decision"**: policy makes a decision → callback fires side effect at the actor layer. Cleanly separates "what to do" from "how to do it."
- **Annotation-driven improvement loop**: human reviewer tags per-turn wins/fails, LK judges auto-label, golden corpus catches regressions on every deploy. Applies to any agent.
- **Tenant keyterm boost for STT**: business names, staff names, service names go into the STT provider's keyterm slot. Immediate accuracy jump on any tenant-specific vocab.

## 8 next-product ideas (ranked by market demand × stack reuse)

*Source: `docs/market-research/jobs-analysis-2026-08-30.md` — analysis of 124 Upwork freelance postings in Aug 2026.*

### 1. WhatsApp Booking Agent — per-vertical starter kits
- **Market signal:** 8 direct posts + 36 mentions of WhatsApp. Verticals asking: physio, vet, dental, tourism, real-estate, Mexican rehab clinic.
- **Budget:** $100-$700 per build, repeatable
- **Reuse:** 85% — brain, tools, calendar, CRM sinks, session manager, annotation dashboard
- **New:** WhatsApp Cloud API adapter (~3 days), voice-note transcription (STT already there), phone-number-identity-resolution
- **Effort to demo:** 1 week
- **Why:** Highest volume of concrete requests, shortest path to shippable second product

### 2. Multi-tenant white-label agency SaaS
- **Market signal:** Multiple "give me source + white-label" asks
- **Reuse:** Voice pipeline + Terraform module + annotation dashboard as client-facing view
- **New:** Sub-account tenant model, per-tenant number provisioning, agency billing, per-tenant prompt-editor UI ("staff must edit without a ticket" — repeated 6x)
- **Effort to demo:** 2-3 weeks
- **Why:** Highest MRR ceiling — agencies resell to their own customer bases

### 3. Clinic voice receptionist with EHR write-through (HIPAA-safe)
- **Market signal:** $2K Nextech aesthetics + physio Plato + vet + dental SaaS. Healthcare = highest-budget subvertical ($2K-$10K)
- **Reuse:** Voice + tools + escalation + our clinic prompt
- **New:** BAA-signable stack review (HIPAA compliance_mode = task #75), EHR adapter framework (Cliniko simplest, then Nextech, Jane, Vagaro, SimplePractice, Halaxy), medical-guardrail prompt library, configurable recording-consent per US state
- **Effort to demo:** 1 week for Cliniko; each additional adapter ~2-3 days

### 4. Voice overflow receptionist (rings humans first, catches misses)
- **Market signal:** Spa post + dental/HVAC ("missed call = $300-$5K job")
- **Reuse:** Full voice pipeline, escalation rules already in tools framework
- **New:** Overflow-forwarding docs per PBX (Dialpad/RingCentral/GHL/Snet), supervised-launch live-listen UI, per-state compliance disclosure library (CA AB 2905/SB 1001/AB 489)
- **Effort to demo:** 1 week + 3 days for live-listen viewer
- **Why:** Autoquill and competitors' entire pitch is "missed call recovery"

### 5. Outbound cold-call qualification agent (Vapi/Retell alternative)
- **Market signal:** 9 direct posts + $10K predictive-dialer + BPO platform post
- **Reuse:** Voice pipeline, tools, brain (script/persona per campaign)
- **New:** Bulk-dialer controller with pacing, AMD (answering-machine detection), DNC list, per-campaign eval/reporting, Slack alert integration
- **Effort to demo:** 2 weeks for batch-dialer with AMD; predictive/skills-routed is months (skip until real deal)

### 6. RAG "business brain" bolt-on
- **Market signal:** 17 RAG posts, several want "brain for the business"
- **Reuse:** Brain is already per-tenant knowledge
- **New:** Document ingestion UI (drag-drop PDF/DOCX), citation surfacing in annotation dashboard, eval harness
- **Effort to demo:** 1 week to expose current brain as standalone Q&A endpoint

### 7. Lead-reactivation voice+WhatsApp loop (real-estate / home-services)
- **Market signal:** UK estate agency $500 repeatable, real-estate posts, HubSpot recruitment
- **Reuse:** Voice + brain + session manager + tools (CRM, calendar)
- **New:** Dormant-lead selection query per CRM, multi-touch cadence engine, WhatsApp adapter (shared with #1)
- **Effort to demo:** 1.5-2 weeks

### 8. Post-call summarizer / annotation studio (productized dashboard)
- **Market signal:** "AI Transcript Summary MVP" $850, HubSpot recruitment, real-estate ("transcribe → summarize → write back to contact record")
- **Reuse:** Annotation dashboard IS already this
- **New:** Structured extraction schema per vertical, CRM write-back connectors (already exist as stubs — task #100)
- **Effort to demo:** ~1 week to package the dashboard + 2 CRM connectors

## Recommended sequencing for the other-agent-work chat

**Sprint 1 (Weeks 1-2):** WhatsApp Booking Agent + one vertical (vet or physio). Uses ~85% of receptionist code + one new adapter. Ships in a week.

**Sprint 2 (Weeks 3-5):** Multi-tenant agency mode + per-tenant prompt editor UI. Turns single-tenant receptionist into resellable SaaS. Highest MRR ceiling.

**Sprint 3 (Weeks 6-7):** Clinic + EHR (Cliniko first). Unlocks healthcare-buyer segment ($2K+ per deal).

**Later:** Cold-outbound qualification agent (needs AMD + DNC + pacing — bigger build).

## What's NOT in the queue and why

| Idea | Why deferred |
|------|--------------|
| Predictive dialer platform | Months of telephony work; only 1 post at $10K + 12mo. Wait until customer requests. |
| Content/SEO/social-media agents | Low reuse (~30%), commoditized by ChatGPT wrappers |
| Data extraction / OCR agents | Low reuse (~20%), specialty tools (Textract, Azure DI) dominate |
| Personal assistant / email triage | Low differentiation, Lindy/Relevance already own this |
| Coding agents / dev-tooling | Adversarial market, GitHub Copilot + Cursor + Claude own it |

## Anti-patterns to avoid on Upwork

- Fixed-price $5-$50 "expert" builds with 100+ requirement bullets (arbitrage bait)
- "Fix my GHL AI voice agent for $20" (inherit platform blame)
- Commission-only appointment-setter postings
- "Start proposal with [emoji]" at low budget (filter theater)
- Off-topic AI (Unity XR, Godot games, PPG signal ML)

## For the new chat — first moves

Once spawned, that chat should:

1. Read this brief (you're doing that now).
2. Read `docs/market-research/jobs-analysis-2026-08-30.md` for the raw job data + top 12 verbatim opportunities.
3. Read `packages/integrations/sinks.py` to understand what CRM sinks already exist as stubs (GHL/HubSpot/Pipedrive/Sheets).
4. Read `packages/core_agent/brain.py` (just skim structure) to see what's reusable.
5. Pick ONE product (recommend WhatsApp Booking Agent), scope a 1-week build, ship a demo.

Do NOT start touching `feat/architectural-networking` code from the new chat without coordinating — this session + voice-agent session are actively iterating on it. Fork into a new branch for other-agent work.

## Coordination notes

- **This session (networking lane)** owns: backend, auth, CRM sinks, Terraform, deploy pipeline, annotation dashboard.
- **Voice-agent session (humanness lane)** owns: brain, prompts, sub-agents, discovery drill, humanness events, voice pipeline patterns.
- **Other-agent-work chat (you'll spawn)** should own: new products only. Don't touch the receptionist unless coordinating.

When in doubt about which chat should do what, ask this one — I can `SendMessage` to voice-agent to check what they're working on.
