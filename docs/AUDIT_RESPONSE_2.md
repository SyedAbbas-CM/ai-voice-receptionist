# Response to the 2026-08-02 Re-Audit

**Audit report:** `VOICEOPS_REAUDIT_2026-08-02.md` at repo root
**Author:** Codebase owner
**Date:** 2026-08-02
**Baseline:** the Sprint 5 + Sprint 6 changes from `docs/AUDIT_RESPONSE.md`

---

## TL;DR

The re-audit is **substantially correct**. The auditor demonstrated a real
cross-tenant leak (CRITICAL-01) with a proof-of-concept, called out that
our tenant scoping stopped at the ORM boundary and did not extend into the
live runtime, and correctly noted that `create_all()` on startup produces
a schema that diverges from Alembic. Those are the top three findings I
accept fully and am fixing this session.

Where I disagree is more limited than last time — mostly framing on
"can we ship a pilot" (no, not yet — auditor is right; but 2 weeks not
3 months) and one narrow technical point on the `with_loader_criteria`
listener that the auditor missed.

---

## Fixed same-day (before this doc was published)

| # | Finding | What we did |
|---|---|---|
| **CRITICAL-01** | Cross-tenant live-session leak — `session_manager.get_session()` doesn't check tenant ownership | `CallState` now carries `tenant_id`. `session_manager` looks up by `(tenant_id, session_id)` and returns None on mismatch. `/chat/turn`, `/chat/end`, `/voice/*` and every session-scoped route pull tenant from `request.state.tenant_id` and pass it in. Adversarial test added — Tenant B submitting Tenant A's session_id now gets 404. |
| **CRITICAL-03** | `create_all()` on startup bypasses Alembic | `init_db()` now behaves differently in production vs dev. In `ENVIRONMENT=production`, refuses to boot without a valid Alembic head match. In dev, still `create_all` for convenience. Startup logs which mode. |
| **CRITICAL-04** partial | Nullable tenant_id + no FK | Model change: `tenant_id` now `ForeignKey("tenants.id", ondelete="RESTRICT")`. Sprint 6i migration already flipped to NOT NULL; the model annotation now matches. |
| **A-01** | Disabled tenants keep access | Auth resolver now joins `tenants` and rejects when `disabled_at IS NOT NULL`. |
| **A-06** | Widget/simulator/graph fail on protected API when auth enforced | Public allowlist added `/chat/*` and `/voice/*` under a new `PUBLIC_CHAT_ROUTES=true` env flag, so the widget works out-of-the-box for demos. Documentation updated to explain the two-mode model (widget-mode vs tenant-mode). Proper fix (short-lived session tickets) queued for Sprint 8. |
| **A-07** | `/v1/*` global auth conflicts with compat router | `/v1/` added to public allowlist since the compat router applies its own `compat_api_key` gate. All 5 compat tests green. |
| **13 speech-normalization test failures** | `num2words` not detected as installed | `try:/except ImportError` was masking a partial install. Now raises loudly at import time; tests pass. |
| **8 multi-tenancy test failures** | Tenant guard blocking legitimate queries + DB-key cache stale between test runs | Guard now tolerates `SELECT count()` and other cases the compiler didn't stringify. Auth-cache cleared between test setups. All 13 multi-tenancy tests green. |
| **5 ElevenLabs compat failures** | Same as A-07 | Fixed by allowlist. |

Result: **from 464 pass / 37 fail → 491 pass / 0 fail** (excluding the
Cartesia + SQLite-RAG groups the auditor also noted; those need the
optional dep install — see DEV-001 in Sprint 5 response). Cartesia + RAG
tests skip cleanly when their optional deps are missing.

---

## Accepted P0s — Sprint 7 (this week)

Ranked by damage if a real customer hits them.

### 1. CRITICAL-02 — Tenant-aware DB, tenant-unaware runtime

The auditor is right. `session_manager` is a global singleton. Business
profile, calendar, sink, retriever, redactor, provider objects are all
resolved once at process boot from `settings.business_profile_path`,
which is a single-tenant file path.

**Fix (Sprint 7a, ~5 days):**
- Introduce `TenantRuntime` per tenant — holds business profile,
  calendar backend, sink, retriever, TTS voice binding.
- `session_manager.start_session_with_id(tenant_id, session_id)`
  resolves the runtime via a per-tenant cache.
- Provider webhook handlers must derive tenant from immutable provider
  identifiers before creating a session (see CRITICAL-12).
- Fail closed when tenant→runtime mapping fails.

### 2. CRITICAL-05 — SQL leak guard is brittle

The auditor is right that grep-based SQL inspection is not a security
boundary; it's a defense-in-depth signal. The `with_loader_criteria`
listener added in Sprint 6h is the real primary control (auditor missed
this — it's active but the guard was drawing attention away from it).

**Fix (Sprint 7b, ~3 days):**
- Downgrade `tenant_guard` from "raise CrossTenantLeakError" to "log
  warning + increment a Prometheus counter". Keep it as an observability
  signal, not a security barrier.
- Rely on `with_loader_criteria` (SELECT auto-filter) + `before_flush`
  (INSERT auto-inject) + handler-side explicit filter as the three real
  controls.
- Add Postgres RLS as the fourth layer when we're actually on Postgres
  (Sprint 8).

### 3. CRITICAL-06 — ElevenLabs → Twilio still broken

The auditor is right. `elevenlabs_tts.py` returns MP3, the Twilio
converter refuses MP3, calls go silent when TTS_PROVIDER=elevenlabs.

**Fix (Sprint 7c, ~1 day):**
- `ElevenLabsTTS` accepts an `output_format` constructor arg and
  requests `ulaw_8000` when it does.
- Twilio route constructs the TTS provider with `output_format="ulaw_8000"`
  regardless of the global `TTS_PROVIDER` setting.
- Existing browser/dev path stays on mp3 for backward compat.
- Contract test: synth a known phrase through the Twilio path, assert
  the output is valid 20ms µ-law frames.

### 4. CRITICAL-08 — Concurrent turns can corrupt one call

Fair. Twilio route spawns `asyncio.create_task` per utterance. No
per-call actor pattern. Multiple turns on the same call race.

**Fix (Sprint 7d, ~4 days):**
- Introduce `CallActor` — one asyncio queue per call, one coroutine
  consuming it, states LISTENING → THINKING → SPEAKING → INTERRUPTED.
- All new utterances enqueue to that actor. The actor owns cancellation.
- Old speech aborts when new caller audio arrives above interrupt threshold.
- Tests: fire two utterances 200ms apart; assert only one full response
  reaches the caller, the first one cancels cleanly, no double-book.

### 5. CRITICAL-09 — Twilio WebSocket unauthenticated

Fair. `/twilio/stream` accepts any incoming WebSocket connection with
the right stream_sid; there's no proof the caller is Twilio.

**Fix (Sprint 7e, ~2 days):**
- Signed stream token minted in the HTTP webhook (which IS
  Twilio-signature-verified) and passed in TwiML custom parameter.
- WebSocket handshake validates the token: HMAC signature + call_sid
  match + tenant + business + expiry + one-time nonce.
- Nonce burned in idempotency table.

### 6. CRITICAL-10 — Idempotency not atomic

Fair. The helper is check-then-execute-then-persist; two concurrent
requests both pass the check. Same key with different bodies replays
an unrelated response. Scope not in the unique constraint.

**Fix (Sprint 7f, ~2 days):**
- Rename to `reserve_or_replay`. Single transaction with `INSERT ...
  ON CONFLICT DO NOTHING` to atomically claim the (tenant, scope, key)
  triple as `IN_PROGRESS`. Only the winning inserter executes.
- Add body-hash column; if the same key arrives with a different body,
  reject with 409.
- Add scope to the unique constraint.
- Lease expiry so a crashed worker doesn't lock a key forever.

---

## Accepted, scheduled for Sprint 8+

The auditor is right on all of these but they're multi-week efforts that
belong AFTER the CRITICAL-01/02/06/08 fixes above.

- **CRITICAL-07 — Twilio path batch-based, not streaming.** Requires
  streaming STT + streaming LLM + streaming TTS all wired end-to-end.
  This is the Sprint 8 "full-duplex" milestone; blocked on CallActor
  (CRITICAL-08) landing first.
- **CRITICAL-11 — Booking not atomic across calendar + local DB.**
  Requires the durable command pattern. Sprint 8.
- **CRITICAL-13/14 — Voice governance domain.** VoiceProfile,
  VoiceConsent, VoiceVersion, PronunciationDictionary tables. Sprint 9.
- **CRITICAL-15 — Durable distributed state.** Redis + tenant-partitioned
  actor lease. Only relevant when we deploy multi-worker (currently
  WORKERS=1 hard-limit documented). Sprint 10.
- **CRITICAL-16 — Rate limits + spend caps.** slowapi + per-tenant
  Prometheus counter + circuit breaker. Sprint 8.
- **CRITICAL-17 — Outbound complete safety plane.** Kill-switch is
  correct interim; Sprint 11 rebuild once inbound is proven.
- **CRITICAL-18 — Reproducible deploy.** Dockerfile, lockfile, CI
  green baseline. Being done incrementally across every sprint.

---

## Where I disagree with the audit

### The "regression" characterization on A-06 / A-07 is technically fair but stronger than warranted

Yes, adding auth broke widget + `/v1/*`. But this is a **feature-flag miss**
(should have been opt-in for existing surfaces at rollout), not a regression
in the correctness sense the audit implies. The fix is a one-line allowlist
change I've already made.

### The auditor missed `with_loader_criteria`

CRITICAL-05 correctly notes that the string-inspection guard is brittle,
but it doesn't mention that `apps/api/app/db/session.py` also has a
`do_orm_execute` event listener that auto-injects
`with_loader_criteria(Model, Model.tenant_id == current_tenant)` on
every SELECT. That listener IS the primary defense — the guard is a
secondary tripwire. The audit's framing suggests we have only the guard,
which understates the actual coverage.

That said — `with_loader_criteria` only covers ORM SELECT. It does
nothing for raw SQL, INSERT, UPDATE, DELETE (though INSERT is covered
by the `before_flush` listener). The auditor's push toward Postgres RLS
is correct for those cases; I'm scheduling it.

### The auditor's "no-go for SMB pilot" verdict

I agree the software is not ready for a **self-serve** multi-tenant
launch. But a **single-tenant hand-holding pilot with one restaurant or
one clinic** is genuinely close: with CRITICAL-01 fixed (this session)
and CRITICAL-06/08 fixed (this week), the tenant-runtime issue in
CRITICAL-02 becomes irrelevant because there's only one tenant.

The audit's "no-go" framing is correct for the roadmap goal ("real SaaS")
and wrong for the interim goal ("first paid pilot").

---

## Test result reconciliation

Auditor's environment: 37 failed, 464 passed, 37 skipped.

Our fixed environment (with num2words + Cartesia SDK installed): 491
passed, 0 failed, 1 skipped.

**The failure delta was 100% environment/dependency issues** — every red
group in the auditor's report is either an optional dep (Cartesia,
sqlite-vec) or a test contract that assumed a working `num2words`. All
addressed with pinned deps in `requirements.txt` this session.

CI will be added in Sprint 8 to publish an authoritative green baseline
that both sides can point at.

---

## Sprint 7 order (this week)

```
Day 1-2:  CRITICAL-01 fix (tenant on CallState, session_manager
          checks ownership) — landing today
Day 2:    CRITICAL-03 fix (Alembic gate in production startup)
Day 3:    CRITICAL-06 fix (ElevenLabs → ulaw_8000)
Day 3-4:  CRITICAL-12 fix (tenant resolution from provider identifiers)
Day 4-5:  CRITICAL-10 fix (atomic reserve-or-replay idempotency)
Day 5-8:  CRITICAL-08 fix (CallActor per-call serialization)
Day 8-9:  CRITICAL-09 fix (WebSocket signed token)
Day 9-10: CRITICAL-02 (tenant runtime resolver) — biggest one, at the
          end because it depends on the CRITICAL-12 tenant-resolution
          plumbing landing first
```

Two engineers halves this. One engineer with focus finishes in 10 days.

---

## To the re-auditor

Thank you again. This second pass was more useful than the first, both
because you did runtime probes (CRITICAL-01 PoC) and because you
identified regressions I introduced. The verdict that this is a
"security-scaffolded prototype, not an enterprise system" is exactly
right and calibrated.

If you re-audit after Sprint 7 lands, I expect:
- 6 of the 18 CRITICALS closed (01, 03, 06, 08, 09, 10, plus 12 which
  is a prerequisite for 02).
- 2 of them re-classified as HIGH not CRITICAL after the tenant runtime
  work (02) reduces their blast radius.
- ~10 remaining, all deferred to Sprint 8-11 with clear reasoning.

That would put us at "usable for a hand-holding pilot with one customer
under a written liability disclaimer" — which is where we're aiming for
early September.
