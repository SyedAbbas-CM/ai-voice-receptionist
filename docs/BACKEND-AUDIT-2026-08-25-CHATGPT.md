# Backend / CRM / Compliance Audit — 2026-08-25 (ChatGPT)

**Source:** ChatGPT audit of bundle `receptionist-codebase-2026-08-25_1312-audit-2026-08-25.zip`, prompted with `docs/CHATGPT-AUDIT-PROMPT-BACKEND-2026-08-25.md`.

**Verdict:** would not put current build in front of unrelated paying tenants yet. Six P0s (five unconditional + one conditional on healthcare). Existing tenant/context architecture, API-key model, migration discipline and sink abstraction are usable — no rewrite required. Fixes are deltas.

**Common failure mode:** "temporary demo bypass became production ingress." `"default"` tenant, public widget APIs, debug APIs, unsigned WebSockets, direct dial are all variations of the same pattern.

---

## P0 ship-blockers (must fix before first paying customer)

### 1. `"default"` tenant is a supertenant
- **File:** `apps/api/app/core/session_manager.py:140-167`, `170-195`; `routes/chat.py:12-15, 37-61`
- **Check:** `if state.tenant_id != tenant_id and tenant_id != "default": return None`
- **Fix:** drop the `and tenant_id != "default"` clause. If dev needs a default tenant, make an actual DB row.
- **Test gap:** existing cross-tenant tests use A vs B, never `"default"` vs A.
- **Owner:** networking
- **Blast radius:** every live tenant session.

### 2. `/debug/*` globally exempt from auth
- **File:** `apps/api/app/middleware/auth.py:40-87`; `routes/debug.py:60-97, 213-245, 273-353, 366-412`
- **Exposes:** traces, span attrs, call timelines, recent errors, semantic timelines, failure patterns, live WebSocket event streams.
- **Fix:** remove `/debug/` from `_PUBLIC_PATH_PREFIXES`. Require admin OR tenant auth. Gate mounting on `OBSERVABILITY_API_ENABLED`.
- **Owner:** networking
- **Blast radius:** all operational telemetry + call content.

### 3. Public AI endpoints fail-open
- **File:** `middleware/auth.py:75-86`; `routes/chat.py`, `routes/voice.py`, `routes/elevenlabs_compat.py:44-48`; `config.py:303`
- **Symptom:** `/chat/*`, `/voice/*`, `/v1/*` publicly reach LLM/STT/TTS. `compat_api_key: Optional[str] = None` fails open when unset.
- **Fix:** authenticate all three. Short-lived signed tokens for browser widgets. `/v1/*` returns 503 when unset, never public. Per-tenant + per-IP rate limits.
- **Owner:** networking
- **Blast radius:** entire SaaS provider account balance + availability.

### 4. Twilio Media Stream WSS unsigned + hardcoded tenant
- **File:** `routes/twilio.py:632-647`
- **Symptom:** `await ws.accept()` without `x-twilio-signature` validation, then hardcoded `tenant_id="default"`.
- **Fix:** validate signature BEFORE `.accept()` using Twilio SDK validator. Resolve tenant from called number → `PhoneNumberMapping` → tenant_id.
- **Owner:** networking
- **Blast radius:** every production Twilio call, cross-tenant integrity.

### 5. `/outbound/dial` bypasses kill switch + compliance policy
- **File:** `routes/outbound.py:158-205, 220-260, 340-418`
- **Symptom:** direct `/dial` doesn't call `_outbound_disabled_guard()` or `decide_can_call()`. Accepts arbitrary `to` and `from_number`.
- **Fix:** add guard + consent + DNC + quiet-hours + tenant-owned caller-ID enforcement. Remove client-supplied `from_number`.
- **Owner:** networking (call-control) + me (SMS-consent equivalent for /dial voice consent)
- **Regulatory:** FCC TCPA ruling — AI-generated voices fall under artificial/prerecorded-voice restrictions; prior express consent required.
- **Blast radius:** Twilio bill, phone-number reputation, outbound regulatory exposure.

### 6. HIPAA-CONDITIONAL: Lightsail deployment not eligible for PHI
- **Files:** `session_manager.py:228-304`, `db/models.py:105-161`, `sinks.py:120-153, 231-305`
- **Symptom:** persist caller_name + phone + service + summary. Clinic vertical → likely PHI. AWS HIPAA-eligible services list (updated 2026-07-22) does NOT include Lightsail. AWS explicitly says non-eligible services must not process ePHI.
- **Fix:** for healthcare mode, migrate Lightsail → EC2, SQLite → RDS Postgres, execute AWS BAA + OpenAI BAA. Add `tenant.compliance_mode = "standard" | "hipaa"` + `allowed_phi_sinks` allowlist. Fail-closed when sink not in allowlist.
- **Owner:** networking (infra) + me (compliance_mode schema + sink guard)
- **Blast radius:** healthcare tenants only. Non-healthcare pilots not blocked.

---

## P1 items (must fix soon)

### Auth / tenant isolation
- **P1** `tenant_guard` weak on UPDATE/DELETE — text-inspects compiled SQL for "tenant_id" + "WHERE" without verifying predicate is `tenant_id == current_tenant`. Extend ORM injection to UPDATE/DELETE. **Owner:** networking.
- **P1** Alternate ingress single-tenant — `vapi.py:129-145`, `channels.py:41-51` hardcoded `"default"`. Build `IntegrationIdentityResolver`. **Owner:** networking.
- **P1** `/twilio/status` unsigned — my new route. Once used for billing/follow-up/dispositions, becomes exploitable. Add `Depends(require_valid_twilio_signature)`. **Owner:** me.

### SMS / TCPA
- **P1** Booking confirmation SMS fires without captured consent — `sinks.py:414-443`. Add `SmsConsent` table (tenant_id + phone_hash + consented_at + consent_source + revoked_at). Only send when consent exists. **Owner:** me.
- **P1** SMS says "Reply Y to confirm, N to cancel" but no handler exists — `sms_sender.py:63`. Either remove that line or add `/twilio/sms` handler. **Owner:** me.
- **P1** No `messaging_enabled` kill switch at `send_sms()` boundary. Add global + per-tenant flag. **Owner:** networking (config surface) + me (call site check).

### Disclosure / privacy
- **P1** "Never confirm OR deny AI status" is legally wrong in Utah (Utah Code §13-77-103). When directly asked, must disclose. Change prompt to deterministic `"I'm the virtual receptionist for {business_name}"`. **Owner:** me.
- **P1** Recording disclosure fail-open — schema config + brain wiring OK, but no single source of truth linking `recording_enabled` to `recording_notice_policy`. Add `recording_notice_policy = always | jurisdiction_required | disabled`. Fail startup if mismatched. **Owner:** me.
- **P1** Retention effectively indefinite — `CALL_EVENT_LOG_RETENTION_DAYS=0` default, per-call log files "NEVER pruned by us". Add `tenant.retention_days=90` + `scripts/retention_gc.py`. **Owner:** networking (worker) + me (config schema + subject-delete API).

### CRM
- **P1** No durable CRM outbox — `sinks.py:83-103`. Failed CRM writes are silently lost. Add `integration_outbox` table + delivery worker with backoff. **Owner:** networking (table + worker) + me (sink migration to consume outbox events).
- **P1** HubSpot/GHL clients have no 429/retry policy — `hubspot_client.py:78+`. Add `408/429/500/502/503/504` → Retry-After + jittered backoff. **Owner:** me.

### Database
- **P1** Postgres async dialect + sync engine — `db/session.py:72-80`. Reject `+asyncpg` in config validation, support only `postgresql+psycopg://`. **Owner:** networking.
- **P1** ORM/Alembic drift on `tenant_id` nullability — `db/models.py:111-114, 125-152`. ORM says nullable=True, migration made NOT NULL. Update ORM. **Owner:** networking.
- **P1** No automated backup / RPO — SQLite file, no snapshot strategy. **Owner:** networking.
- **P1** Idempotency check-then-act race — `db/idempotency.py:56-100`. Unique constraint `(tenant_id, key)` disagrees with lookup `(tenant_id, key, scope)`. Change unique to `(tenant_id, scope, key)`, reserve row before mutation, `ON CONFLICT DO NOTHING`. **Owner:** networking.

### Observability / incident response
- **P1** Log hygiene — `call_event_log`, `per_call_logger`, `structured_log` all persist raw PII. Install redaction filter at logging boundary. Whitelist fields for structured events. **Owner:** networking.
- **P1** API key rotation workflow incomplete — `routes/admin.py:108-148`. Add `DELETE /admin/tenants/{tid}/api-keys/{key_id}` with `_db_key_cache.clear()` + audit. **Owner:** networking.
- **P1** No canonical call-trace lookup — pieces exist but not one call → tenant → transcript → tool_calls → booking → CRM_writes → SMS/email view. Build `scripts/trace_call.py` or `/admin/calls/{call_id}/incident`. **Owner:** networking.

---

## Verified passes (leave alone)

- API key SHA-256 hashing (admin.py)
- `hmac.compare_digest` for env-token comparison
- Alembic production enforcement (`ENVIRONMENT=production` blocks `create_all` + verifies revision)
- CompositeSink failure isolation
- FollowupSink HTML escaping (2026-08-25 security-review fix)
- `.env` path hygiene
- `.claude/settings.local.json` scanner (caught the leak proactively)
- 102/102 targeted backend/compliance tests passing

---

## Corrections to my notes

**`packages/slot_parsers` IS wired to production.** ChatGPT confirmed live callers from `twilio_actor.py` and `clinic_tools.py`. My [[enter-slot-capture-not-wired-2026-08-24]] memory was wrong on that specific claim — the ROUTE `enter_slot_capture()` on the actor has no non-test callers, but the parser package itself is wired via `packages/dialogue/reducer.py` and clinic_tools' PhoneValidator. Memory needs updating.

---

## Suggested 3-day sequence (ChatGPT recommendation)

**Day 1 (all networking):** P0.1 default supertenant + P0.2 debug lockdown + P0.3 public AI endpoint auth.

**Day 2 (networking + me):** P0.4 Twilio WSS signature + tenant resolution + P0.5 outbound guard + my `/twilio/status` signature verification.

**Day 3:** SMS consent (me) + DB backups (networking) + ORM drift (networking) + AI disclosure fix (me).

Postgres migration + retention worker + HIPAA mode come after these.

---

## Full audit text

Stored in ChatGPT conversation on the user's account. Cited above by exact file:line.
