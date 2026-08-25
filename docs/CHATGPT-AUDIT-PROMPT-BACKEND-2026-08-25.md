# ChatGPT Audit Prompt — Backend / CRM / Compliance
**Bundle:** `receptionist-codebase-2026-08-25_1247-audit-2026-08-25.zip`
**Scope:** everything EXCEPT voice quality, TTS latency, STT tuning (those live in a separate audit)
**Audit type:** security + compliance + integration-correctness sweep

---

## Paste this into ChatGPT along with the zip:

You are auditing a production Twilio-based voice receptionist SaaS. It's Python + FastAPI + SQLAlchemy + SQLite (moving to Postgres). Multi-tenant. Currently in demo/pilot phase. First customers land within weeks.

The project already ships with:
- Voice pipeline (Deepgram STT → OpenAI LLM → ElevenLabs TTS → Twilio Media Streams)
- Multi-tenant tables (Tenant / ApiKey / Session / Transcript / Booking / Idempotency)
- Auth middleware (Bearer key → tenant_id contextvar → SQLAlchemy auto-filter)
- CRM sinks (GoHighLevel, HubSpot, Google Sheets, SMS+email follow-up)
- Server-rendered dashboard for tenants
- Twilio call-status webhook

Audit for the following. Rank findings **P0 / P1 / P2** by "how badly does this bite a paying customer or leak data." Concrete file:line citations required.

### 1. AUTH & TENANT ISOLATION
- Are all read paths tenant-scoped? Can tenant A observe tenant B's data via any endpoint (dashboard, admin, sessions, chat, /vapi/*, /elevenlabs-compat/*, /twilio/*)?
- Does the SQLAlchemy `_auto_filter_tenant` middleware catch every ORM query, or are there raw-SQL / execution_options escapes that skip it?
- Is `skip_tenant_filter=True` used only where an EXPLICIT tenant filter also runs? (Dashboard uses it — verify safety.)
- API-key hashing: are keys stored hashed, and is comparison constant-time?
- Rate limiting: any per-tenant limits on `/twilio/voice`, `/twilio/stream`, `/chat/*`, `/admin/*`?

### 2. SECRET & LOG HYGIENE
- Grep for anywhere API keys / auth tokens could leak: exception messages, logger.info calls, HTTP error bodies, Twilio webhook replies, dashboard rendering.
- Do any exception handlers `log.exception(...)` on requests that contain Authorization headers?
- Does the per-call log emitter redact PII (phone, name) or write raw?
- Is `.env` referenced by relative path anywhere that could be traversed?
- Is there a `.claude/settings.local.json` or similar user-scoped file with real bearer tokens baked into it? (Historical leak — bundle script now excludes but check the code doesn't rely on any credentials from there.)

### 3. COMPLIANCE (TCPA / CCPA / GDPR / state consent)
- Call recording: two-party-consent states (California, Florida, Illinois, Maryland, Massachusetts, Montana, Nevada, New Hampshire, Pennsylvania, Washington) require disclosure at the start of the call. Does the greeting contain "this call may be recorded" or similar? Is it configurable per-tenant?
- SMS: is TCPA opt-in explicit? Does the FIRST message include disclosure? Is opt-out (STOP) honored? Is there a suppression list preventing re-messaging after STOP?
- Transcript PII: are transcripts retained indefinitely by default? Is there a per-tenant retention window? Can a caller request deletion (CCPA "right to be forgotten")?
- HIPAA-adjacent: dental/clinic vertical stores treatment reasons ("root canal", "pain since Monday") — is this PHI under HIPAA? If so, we need a BAA with Twilio/Deepgram/OpenAI. If not, note why.
- Prompt system: are we telling the caller they're speaking to an AI when asked? (We now redirect without confirming/denying — is that legally defensible?)

### 4. CRM SINK CORRECTNESS
- HubSpot / GHL / Sheets / FollowupSink — do all four failure-isolate (one sink failing must not crash others)?
- Retry semantics: is a failed CRM write retried, or lost forever? Should be an outbox pattern.
- PII flow: what caller data reaches each CRM? Is that documented for tenant-onboarding disclosure?
- Idempotency: if a booking tool call fires twice (LLM retry), does the sink dedup, or does the tenant see two HubSpot contacts?
- HubSpot free-tier: are we hitting the 100 req/10s / 250k/day quotas? Any backoff?
- FollowupSink email: HTML injection was found and fixed (escapes applied). Verify the fix is complete and no other paths interpolate caller-controlled data unescaped.

### 5. DATABASE HARDENING
- Schema — is PII encrypted at rest, or plaintext? (Currently plaintext SQLite. Recommend approach.)
- Indexes — are the dashboard queries indexed? What's slow at 10k / 100k / 1M rows?
- Retention — no worker deletes old data. Design the smallest correct retention worker (age-based, tenant-configurable).
- Migrations — is Alembic wired? Are there hand-written CREATE TABLE fallbacks that drift from Alembic?
- Backups — where's the SQLite file backed up? What's the recovery-point-objective?
- SQLite → Postgres — what breaks in the move? (SQLite JSON columns, autoincrement, WAL, etc.)

### 6. WEBHOOK & INTEGRATION SURFACE
- `/twilio/voice`, `/twilio/status`, `/vapi/*`, `/elevenlabs-compat/*` — are Twilio-signature verifications enforced or optional? What happens if a spoofed webhook arrives?
- Are webhook response bodies leaking internal state (tenant IDs, session IDs, error stack traces)?
- Idempotency-Key handling on booking mutations — is it enforced at the DB layer or only advisory?

### 7. OBSERVABILITY & INCIDENT RESPONSE
- If a customer says "you cut off my call at 3:47pm yesterday" — what commands recover the trace? Is there a single call_id → transcript → tool_calls → CRM_writes lookup?
- If a customer says "you sent 30 SMS to my caller by accident" — what's the emergency STOP button?
- If we discover an API-key leak, what's the rotation path? Which files reference the key?

### 8. STYLE & TECHNICAL DEBT (advisory, not blocking)
- Files >500 lines that should be split
- Dead code (unused routes, orphaned modules like packages/slot_parsers with zero production callers)
- Comment-vs-code drift where a comment says "TODO fix" that's been there >30d

---

## Output format

For each finding:

```
[Pn] <one-line title>
File: <path:line>
Severity rationale: <one sentence>
Fix (concrete): <2-4 lines of code or a specific approach>
Blast radius: <who is affected>
```

Rank findings globally. Group by category. At the end, give a **"ship-blocker" list** — items that must be fixed before the first paying customer.

Skip P2 findings entirely if there are more than 10; prioritize depth on P0/P1.

Do NOT recommend rewriting anything from scratch. Every recommendation must be a delta against the current code.

Bias toward findings that require code changes, not process changes. "Add a compliance review" is not useful; "add a `retention_days` column to `tenants` + a nightly worker at scripts/retention_gc.py" is.

---

## Follow-up prompts (for after initial audit)

After ChatGPT returns the findings, use these to drill in:

**Compliance drill-down:**
> Of your compliance findings, which are actual laws (I could get sued) vs best-practices (nice to have)? Give me the statute citation for each "actual law" finding.

**Fix ordering:**
> Order the P0/P1 findings by (a) time to fix and (b) blast radius if unfixed. Give me a 3-day and 7-day sequence.

**Secret-leak drill:**
> Walk me through every place a Twilio auth token could touch a log file, error response, or webhook reply. Include third-party libraries.

**CRM edge cases:**
> For each CRM sink, describe the failure mode when: (1) network drops mid-request, (2) API returns 429, (3) tenant's API key expires, (4) caller data violates the CRM's field validation (name too long, invalid phone).

**Database migration risks:**
> When we move from SQLite to Postgres, list every column definition, index, or trigger that will behave differently. Include the `JSON` column type, `DEFAULT`s, and case-sensitivity of `VARCHAR` PKs.

**Multi-tenant paranoia:**
> Design 5 test cases that a malicious tenant would use to try to read another tenant's data. For each, show whether our current code blocks it.

**HIPAA question:**
> If a dental clinic uses this tool and a caller mentions a diagnosis ("I have gum disease"), does storing that transcript trigger HIPAA obligations? Cite the specific HHS guidance. If yes, what's the minimum-viable BAA path with our providers (Twilio, Deepgram, OpenAI)?

---

## What NOT to ask ChatGPT this round

These are being audited separately by the voice-agent chat and don't belong here:
- ElevenLabs vs Vapi vs Grok TTS voice quality
- Latency (Twilio → us → provider → back)
- Deepgram Flux tuning
- Prompt-cache prefix optimization
- SpeechCommitGate + turn-taking logic
- NextActionPolicy activation

If ChatGPT drifts into those areas, redirect: "That's out of scope — focus on backend/compliance."
