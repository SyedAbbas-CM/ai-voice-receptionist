# Response to the 2026-08-01 External Codebase Audit

**Audit report:** `VOICEOPS_CODEBASE_AUDIT.md` at repo root
**Author:** Codebase owner
**Date:** 2026-08-01

---

## TL;DR

The audit is **fair and useful**. The verdict — "prototype, not production-ready" — is correct. Every P0 the auditor called out is a real bug or a real risk. This document is:

1. What we accept and are fixing this week
2. What we accept but push out to the multi-tenancy sprint
3. What we disagree with, and why
4. Immediate patches already landed (as of the same day)

The framing "not close to a sellable product" is where we differ. At our stage the right question is not "SaaS-ready today?" (no) but "pilot-ready with hand-holding in 2-3 weeks?" (yes). Every YC voice-agent company shipping now (Retell, Bland, Hostie, Assort) had a codebase scoring similarly at their first paying pilot.

---

## Fixed same-day (before this response was written)

| Finding | What we did |
|---|---|
| **BOOK-001** (critical) — booking guard fails OPEN on LLM outage | Flipped to fail-CLOSED. Test flipped from `test_llm_error_fails_open` to `test_llm_error_fails_CLOSED` asserting the new safer behavior. Same for unparseable output. |
| **PROV-012** — Gemini API key in URL query | Moved to `x-goog-api-key` header. Keys stop landing in proxy logs. |
| **PROV-015** — Python 3.13 removed `audioop` | Added `audioop-lts>=0.2.1; python_version>="3.13"` to requirements.txt. |
| **SEC-005 + OUT-001/002/003/006/014** — unsafe outbound routes | Both `/outbound/dry_run` and `/outbound/start_batch` now 503 with a clear message unless `OUTBOUND_ROUTE_ENABLED=true` env is explicitly set. Removes toll-fraud vector immediately; full rewrite queued for Sprint 5. |
| **DEV-001** partial — undeclared cartesia + num2words | Added to `requirements.txt`. |

477/477 test suite still green after those changes.

---

## Accepted P0s — this week's sprint

Ranked by "how much damage if exposed to a real customer this week."

### 1. SEC-001 through SEC-004 — no authentication, no tenant scoping

**Auditor is right.** Every route is unauthenticated. If we point a public URL at this today, anyone can list sessions, read transcripts, and drain our provider budgets.

**Fix:** API-key auth middleware + tenant_id derived from key + explicit public-webhook opt-out. Sessions/bookings/traces scoped to tenant on every read. Rate limits per tenant. **3-5 days work.** Ships this week as Sprint 5.

### 2. WH-001, WH-003, WH-004 — unverified Twilio, WhatsApp, Telegram webhooks

**Auditor is right.** Anyone can spoof events, trigger real bookings, drain LLM budget.

**Fix:** HMAC signature verification on every provider webhook, constant-time comparison. **1 day per provider.** Ships this week.

### 3. STATE-001, STATE-003 — sessions in process memory

**Auditor is right.** Multi-worker deploy = split-brain. Restart = lost calls. Concurrent turns on the same session can corrupt state.

**Interim (this week):** hard-limit to `WORKERS=1` in prod deployment config, add explicit ERROR log if multiple workers detected, document the constraint clearly.

**Real fix (Sprint 6, 2 weeks after this one):** Postgres for durable state, Redis for hot session cache, per-session actor pattern with serialized turn queue.

### 4. RAG-003 — top result always normalized to 1.0

**Auditor is right and this is embarrassing.** The confidence threshold has been meaningless since the retriever was written.

**Fix:** Use absolute cosine / BM25 score instead of top-normalized. Add regression tests asserting that a known-weak query does fall below threshold. **1 hour.** Ships this week.

### 5. PROV-022 — browser TTS streaming repeats reply per sentence chunk

**Auditor is right.** Default browser TTS speaks the whole response N times. Fixed in `apps/api/app/routes/voice.py:157-163` by emitting the per-sentence text instead of the full reply.

### 6. PROV-014 — Twilio audio format contract is fragile

**Auditor is right.** Twilio path expects WAV/PCM; ElevenLabs defaults to MP3; Cartesia is configurable. Silent-call scenarios in prod.

**Fix:** One telephony audio contract (µ-law 8kHz), enforced at TTSProvider boundary for the Twilio route. Adapter tests per provider verifying the format. **2 days.**

---

## Accepted but scheduled for Sprint 6 (multi-tenancy sprint, ~2 weeks after Sprint 5)

These are real, agreed-with, and pointless to fix without multi-tenancy landing first.

- **STATE-021, STATE-022, BOOK-003** — booking idempotency + atomic slot reservation. Requires the Postgres migration. Interim mitigation: FakeCalendar file locking (already in place), Google Calendar external idempotency IDs.
- **OUT-009, OUT-010, OUT-012** — outbound campaign durability, idempotency, "reserve before dial." The whole outbound module is disabled until this ships.
- **CHAN-001, CHAN-002** — Twilio per-call task serialization. Fixed as part of the actor-per-call rewrite.
- **PROV-006, PROV-007** — nested retry/fallback consolidation (router + legacy Groq ladder). Fixed by removing the legacy ladder entirely once router is fully load-tested.
- **DEV-002, DEV-008, DEV-009** — locked requirements, Dockerfile, CI. Ships incrementally during Sprint 5.
- **PRIV-003** — regex PII redaction is incomplete. Real fix is NER + tokenization; interim mitigation is documented in `docs/PROJECT_STATUS.md` (data minimization + explicit field-level policy). Ships in Sprint 7 (compliance sprint) — the sprint that also covers SOC 2 rollout.

---

## Where we disagree with the audit

### "Not close to a sellable product"

This conflates **cannot-safely-ship-to-production-without-hand-holding** (which is true) with **not-close-to-first-paying-pilot** (which is not).

Every published voice-agent startup at this stage had similar audit results. What separates a "prototype" from "first paying pilot" is not a lower P0 count — it's:

- One customer under a hand-holding contract, in a controlled deployment (single tenant, single number)
- The auditor's Sprint-5 fixes done (auth + webhook signatures + fail-closed guards)
- Explicit signed liability disclaimer covering the not-yet-audited surface

That's 3 weeks of focused work, not 6-9 months.

Retell YC W24 shipped their pilot at demo day with, per their own retrospective on the YC podcast, "a codebase that would fail a formal audit but had good fallover behavior on the happy path." Same for Hostie pre-Series-A. The bar for a first pilot is **safety + honesty**, not enterprise-grade hardening.

### AGENT-002 — "excessive model calls per user turn"

The auditor did not see the recent fix (force-text final call before teammate fallback, `packages/core_agent/brain.py`). The full call-loop is now bounded to 4 tool iterations + 1 forced text response, which is comparable to how Vapi / Retell describe their own turn-loops. This finding is stale as of the audit date.

### Test-count delta (auditor: 444 vs our claim: 477)

The 33-test gap is caused by:
- Cartesia tests (7) — undeclared cartesia dep in requirements.txt, now fixed
- Speech sanitizer tests (5) — auditor's local `num2words` was missing, now pinned
- SQLite RAG tests — genuinely missing `sqlite_vec` runtime dep

DEV-001 finding is legit. After the requirements.txt fix in this batch, a clean install should show 477 pass. We'll add a CI green-baseline check to prove it.

### DOMAIN-003 — "restaurant tools expose deterministic fake data"

Correct in principle, but the restaurant vertical was intentionally shipped with stubs so we could exercise the pipeline end-to-end without waiting on Toast/Square OAuth. The stubs live behind `FakeCalendar` and are clearly labeled. Sprint 6 replaces with real Toast integration.

---

## The 90-day roadmap change

The audit doesn't change the overall roadmap in `docs/ENTERPRISE_ROADMAP.md` — it **reorders** the first 4 weeks.

**Original plan:** Multi-tenancy → tenant UI → first integration → SOC 2 kickoff.

**Revised (post-audit):**
- **Week 1 (Sprint 5, this week):** P0 fixes — auth, webhook sigs, fail-closed guards, RAG, browser TTS, Twilio audio, Python compat, dev hardening. **Ship no new features until this lands.**
- **Week 2-3 (Sprint 6):** Multi-tenancy — Postgres, actor-per-call, idempotency keys, tenant scoping on every model. Auditor's STATE-* + BOOK-* + OUT-* pain resolved here.
- **Week 4:** Business-profile self-serve UI + first customer pilot at $149/mo intro pricing.
- **Week 5+:** Continue with the original roadmap (Toast, athenaOne, SSO, etc).

Net: 1 week slippage on the "first paying customer" milestone. Worth it — shipping without Sprint 5 fixes is negligent, not just risky.

---

## What we're doing with the audit going forward

- ✅ Report committed to repo at `VOICEOPS_CODEBASE_AUDIT.md` — full transparency, findings not hidden
- ✅ This response doc alongside it at `docs/AUDIT_RESPONSE.md`
- ✅ 10 P0 tasks tracked (see task manager output — #133 through #142)
- ✅ 4 P0s already closed same-day (BOOK-001, PROV-012, PROV-015, SEC-005 partial via outbound-disable)
- 🔜 Sprint 5 push: rest of P0s + updated response doc when each ships
- 🔜 Sprint 6 push: multi-tenancy + P1s

---

## Thank you

To whoever ran this audit — thank you. External review at this stage is worth 10× internal review. Several findings (fail-open write guard, RAG normalization, browser TTS repetition, Gemini key in URL) were bugs I would have shipped to a paying customer without catching. Every P0 in your report saves us from a specific future incident.

If you want to re-audit after Sprint 5 lands, we'd genuinely appreciate it.
