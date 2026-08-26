# ChatGPT Audit Prompt — 2026-08-26 (Progress + CRM Research + Call Drop)

**Bundle:** `receptionist-codebase-2026-08-26_1359-audit-2026-08-26.zip` (7.7 MB, 697 files)
**Two ChatGPT audits already landed:** backend security (docs/BACKEND-AUDIT-2026-08-25-CHATGPT.md) and humanness (docs/HUMANNESS-AUDIT-2026-08-25-CHATGPT.md). This is a **progress + focused-followup** audit, not a fresh full sweep.

---

## Paste this into ChatGPT along with the zip:

You audited this codebase yesterday (2026-08-25) in two passes:
- **Backend / CRM / Compliance** — you found 6 P0s (default supertenant, /debug/* auth, public AI endpoints, Twilio WSS unsigned, /outbound/dial guard, HIPAA-conditional Lightsail-not-PHI-eligible) plus a stack of P1s.
- **Humanness / Receptionist Capability** — you rated the overall product 5/10 and gave a 4-phase plan (wire intelligence to control conversation, complete receptionist fundamentals like END_CALL/transfer/take_message, build the business ops model, build the operating loop).

This audit is the **progress check plus three specific new questions**. Both prior audit docs are in the bundle; read them first.

## 1. Progress against your prior findings

Read `docs/BACKEND-AUDIT-2026-08-25-CHATGPT.md` and `docs/HUMANNESS-AUDIT-2026-08-25-CHATGPT.md`. Then walk through what has and has NOT been addressed in the bundled code. For each finding you flagged, mark:
- **DONE** with file:line pointing to the fix
- **PARTIAL** with what's on disk + what's still missing
- **PENDING** with your revised priority (P0/P1/P2) given what else has shipped since

Specifically I want to know about:

**Backend audit P0/P1s to check:**
- P0.1 `"default"` supertenant bypass — is `session_manager.py:140-200` still lettng `"default"` cross tenants?
- P0.2 `/debug/*` auth — `middleware/auth.py:40-87` + `main.py` gating?
- P0.3 public AI endpoints (`/chat`, `/voice`, `/v1`) — still fail-open?
- P0.4 Twilio Media Stream WSS signature verify + tenant resolution — check `routes/twilio.py` around the `@router.websocket` handler + the new `apps/api/app/telephony/tenant_from_phone.py` + `packages/auth/short_ticket.py`
- P0.5 `/outbound/dial` bypasses kill switch — `routes/outbound.py:158+`
- P1 `tenant_guard` weakness on UPDATE/DELETE — `db/tenant_guard.py`
- P1 ORM/Alembic drift on `tenant_id` nullability — `db/models.py`
- P1 idempotency check-then-act race — `db/idempotency.py`
- P1 SMS consent capture + suppression table (should the `sms_consent` table exist yet?)
- P1 HubSpot 429/retry policy — `packages/integrations/hubspot_client.py`
- P1 Utah AI-disclosure prompt — `packages/core_agent/prompt.py` IDENTITY LOCK section

**Humanness audit P0/P1s to check:**
- P0.1 NextActionPolicy wired to runtime — `packages/dialogue/next_action_policy.py` + `packages/core_agent/brain.py` intercept + `packages/core_agent/next_action_synthesizer.py`
- P0.2 Semantic acknowledgment as a dialogue primitive — is `AcknowledgmentKind` shipped? Wired to a reducer?
- P0.3 ReactiveBrain activation (silence/backchannel/commit)
- P0.4 TransferCoordinator (blind + warm)
- P0.5 `take_message` tool — does it exist yet or still promised in prompt but absent in code?
- P0.6 END_CALL as semantic action — has the timer-flag pile in `twilio_actor.py:660-687` been simplified?
- P0.7 Google Calendar `cancel/reschedule/find_by_phone` parity with FakeCalendar — check `packages/integrations/google_calendar.py`
- P1 CallerContext / returning-caller resolver
- P1 Acoustic features wired to `NextActionPolicy` delivery intent
- P1 Receptionist Inbox extension of `routes/dashboard.py`

For each: **DONE / PARTIAL / PENDING**, and if PARTIAL what's the smallest remaining delta.

## 2. Can the agent reliably drop calls?

**Question the user asked directly:** "can the agent drop calls?"

Answer this with specific detail:
- What paths currently END a call from OUR side?
- Is `_end_twilio_call()` in `apps/api/app/routes/twilio_actor.py` actually invoked reliably?
- Does the farewell → grace window → `stop("farewell")` → REST hangup chain work end-to-end?
- What are the failure modes (Twilio auth issue, caller drops first, WebSocket already closed, etc.)?
- Is there a way the call can be left "in-progress" on Twilio's side after our WSS closes?
- What's the observability — can we tell from logs whether a specific call was cleanly hung up by us vs by the caller vs timed out?
- If a caller says "goodbye" and hangs up their phone, does our system know within seconds, or does it stay listening until timeout?

Give me the concrete truth. If it works most of the time but fails in specific scenarios, list those scenarios. If it's broken, tell me clearly.

## 3. Comprehensive free-tier CRM research (user's ask)

The user was **frustrated** that my earlier CRM research (`docs/FREE-CRM-INTEGRATIONS-2026-08-25.md`) missed the GoHighLevel Marketplace Developer Sandbox — that IS free even though the customer sub-account tier costs $97/mo. I've since corrected the doc.

Please do a THOROUGH sweep of every paid CRM commonly used by service-industry SMBs and report which ones have free developer / sandbox / test-account tiers with API access. Include the sign-up URL, the specific limits, and any "gotcha" like "sandbox goes inactive after 45 days" or "API only on paid plan even though CRM is free". Cover at minimum:

- **HubSpot** — free tier with API? Free developer account? Both?
- **GoHighLevel** — Marketplace Developer Sandbox (I now know this exists, verify)
- **Pipedrive** — Developer Sandbox
- **Zoho CRM** — I concluded free tier has NO API. Verify or correct.
- **Salesforce** — Developer Edition free with API
- **Freshsales / Freshworks CRM** — do they have free API access?
- **Copper CRM** (was ProsperWorks) — dev tier?
- **Insightly** — free tier limits?
- **Bitrix24** — free tier API access?
- **Close** — dev/trial API?
- **Monday.com** — API on free tier or only paid?
- **Airtable** — 1000 req/mo confirmed?
- **Notion** — API on free workspace
- **Attio** — dev tier?
- **Keap (Infusionsoft)** — dev account?
- **Zendesk** (Sell + Support) — sandbox?
- **Intercom** — dev workspace API access?
- **ActiveCampaign** — trial API?
- **Kajabi**, **Kartra**, **ClickFunnels** — API access on any tier?
- **Excel / OneDrive Graph API** — free personal Microsoft account
- **Google Sheets / Workspace** — free with personal Gmail

For each, give me:
- ✅ / ⚠️ / ❌
- Sign-up URL if free-dev-tier exists
- Rate limits + record limits
- Any gotcha (inactivity timeouts, verification requirements, region restrictions)
- Priority for our project (HIGH — real-estate/SMB fit / MEDIUM / LOW)

The output should be one table the user can screenshot and reference during pilot onboarding of any client. Also list the ones that are **completely blocked** (no free API path exists at all) so we know to price paid tiers into any deal that includes them.

## 4. WhatsApp Business Calling API — worth adding as a second telephony transport?

Meta released the WhatsApp Business Calling API. Third-party AI (like ours) can plug into the audio media stream via WebRTC + SIP. Rules per Meta:

- Explicit opt-in template required before ANY outbound AI call
- Cap 5 connected / 24h / user
- Auto-revoke after 4 consecutive unanswered
- Outbound blocked in US, CA, EG, VN, NG
- Inbound global, no restrictions

Docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/calling

Questions:
- **What is a realistic effort estimate to add WhatsApp Calling as a second transport (parallel to Twilio Media Streams) reusing our existing brain / STT / LLM / TTS / sinks?** Specifically: what pieces of `apps/api/app/routes/twilio.py` + `apps/api/app/routes/twilio_actor.py` + `apps/api/app/telephony/` would need to be abstracted so the brain doesn't care which transport delivered the audio?
- **Is the "chat-to-voice escalation" use case real for the SMB market (dental/real-estate/car-wash), or is this only a play for larger customer-support use cases?** The gap: users don't dial our AI on WhatsApp; they text a chatbot and get escalated. Do our current tenants have text chatbots at all?
- **What's the Meta App Review + Business Verification path look like?** How long does approval take for a new voice app?
- **Any competitor already offers this via Vapi / Retell?** If yes, what's the differentiator we'd bring? If no, are we early or is there a reason nobody's built it?
- **Priority relative to the pending humanness / P0 backend work.** Do we build this next, in 6 months, or never (because the SMB market doesn't ask for it)?

## 5. Real reason it "still isn't that human" — LLM-facing behavior audit

User's direct feedback: "its still not that human btw"

Two ChatGPT audits ago you rated humanness 5/10 and gave a 4-phase plan. The gap between where we are (5/10) and where Retell/ElevenLabs are (8/10) is not model quality — it's LIVE WIRING. Please walk through the following specifically:

- **Is `settings.next_action_policy_enabled` actually True on the running production tenant?** Grep the .env pattern in `docs/*` for what user was told to set. If it's False in .env, the whole A1/A2 wiring is dark and the humanness fix hasn't shipped even though the code did.
- **Does `brain.py` actually POPULATE the ConversationDecisionState fields (`caller_shared_hardship`, `caller_corrected_us`, `caller_is_dictating`) from anything real?** If those fields are always False, `_select_ack` always falls to canonical acks and the "match ack to context" behavior doesn't happen.
- **Does the `SemanticPlan` ever get produced?** `packages/dialogue/plan.py::SemanticPlan` is defined. `render_from_semantic_plan` consumes it. Is there any code path that INSTANTIATES a SemanticPlan and calls `state._semantic_plan = plan`? Or is it entirely dead code?
- **Is `_reply_lies_about_booking` firing when the LLM confabulates?** If a caller test-called and got "booked on May 12th" without book_appointment ever firing, either the guard didn't detect or the guard is disabled.
- **Real turn examples from a recent test call.** Pull 3 turns from `data/logs/calls/` or the transcript files under `docs/transcripts/` and evaluate: (a) what did the agent say, (b) what SHOULD it have said per the persona, (c) what does the code have to change to get from (a) to (b)?

Give me the specific delta. If the answer is "the code shipped but the runtime bits are switched off," tell me exactly what env changes flip them on. If the answer is "the code shipped but doesn't actually run the right path," give me the file:line where the path skips the wiring.

I'm out of theory. What's the ACTUAL blocker to sounding human on a live call?

## 6. What ELSE is missing from the plan that we haven't caught?

Given your two prior audits + this bundle's current state — what class of gap have you NOT flagged yet that would matter for a European real-estate SMB paying customer going live? Off-the-top-of-my-head things I worry about:

- **Data-subject-request handler** — GDPR right to erasure, right to data portability. Do we have a `/gdpr/dsr` route? Is there a subject-deletion pathway that also fires against sinks (HubSpot / Pipedrive / GHL)?
- **Multi-tenant business-hours model** — currently a single `BusinessHours` per profile. What about tenants with multi-location + per-location hours?
- **Warm-transfer failure modes** — if the human doesn't pick up, does the agent gracefully take a message or does it hang?
- **Cost-cap per tenant** — is there any protection against a runaway prompt or infinite tool loop from bankrupting a tenant on OpenAI tokens?
- **Fraud / abuse detection** — repeated hang-up-and-redial, script-abusing callers, pumping the system for LLM tokens
- **Call quality metrics** — do we have per-tenant dashboards showing mean turn latency, hangup-vs-drop ratio, tool-error rate?
- **A/B testing infrastructure** — how do we ship a prompt change to 10% of a tenant's calls first?

Rank by impact for a real-estate SMB pilot customer.

---

## Output format for all four sections

Each finding:
```
[Section #, P#] <one-line title>
File: <path:line> OR "N/A - infra"
Status: DONE | PARTIAL | PENDING
What's on disk: <one-line>
What's missing: <one-line> (if PARTIAL/PENDING)
Concrete fix: <2-4 lines>
```

For section 3 (CRM research) use a table.

For section 4 (missing gaps) rank globally by pilot-customer impact.

Skip anything already fully DONE that doesn't affect the pilot customer path.

## What NOT to do

- Don't re-cover P0.1 / P0.2 in fresh detail if they're clearly DONE — just mark DONE with file:line
- Don't recommend rewriting anything from scratch
- Don't audit voice latency / TTS quality / STT tuning — that's a separate audit lane
- Don't score humanness again — you did that yesterday, we're tracking your baseline
