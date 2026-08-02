# VoiceOps Codebase Re-Audit

**Repository:** `voiceops-codebase-2026-08-02.zip`  
**Audit date:** 2026-08-02  
**Comparison baseline:** the prior VoiceOps audit plus the subsequent recommendations for tenant isolation, ElevenLabs voice cloning, expressive speech, full-duplex turn-taking, and enterprise deployment.

---

## Executive verdict

Claude made **real, material improvements**. This is not a documentation-only rewrite. The repository now contains authentication middleware, tenant and API-key models, tenant context propagation, webhook signature checks, idempotency scaffolding, Alembic migrations, safer booking validation, improved browser TTS handling, and a larger test suite.

However, the remediation is concentrated around the **HTTP and ORM shell**. The two systems that actually determine whether VoiceOps can safely serve multiple customers and sound human on live calls remain mostly unchanged:

1. **The live call runtime is not tenant-owned.** The database has tenant columns, but active sessions, business profiles, calendars, retrievers, voice settings, and provider objects remain global or keyed only by external session IDs.
2. **The live phone path is not genuinely streaming.** Twilio still buffers an utterance, performs batch STT, waits for a complete LLM turn, waits for complete TTS, then plays it. ElevenLabs still returns MP3 while the Twilio converter explicitly rejects MP3.

### Current release judgment

| Deployment target | Judgment |
|---|---|
| Local single-tenant engineering demo | **Usable with caveats** |
| Controlled internal phone demo | **Possible after fixing ElevenLabs/Twilio audio and disabling outbound** |
| External SMB pilot with real customer data | **No-go** |
| Multi-tenant SaaS | **No-go** |
| Clinic/HIPAA deployment | **No-go** |
| Enterprise procurement | **No-go** |

The new repository should be considered a **security-scaffolded prototype**, not an enterprise system.

---

## What was verified

The repository was extracted after checking archive paths and links. No provider-facing service, outbound call, webhook delivery, or arbitrary application startup script was executed.

Validation performed:

- Compared the new repository against the prior version at file level.
- Reviewed authentication, tenancy, migrations, live session ownership, Twilio, Vapi, WhatsApp, Telegram, STT, TTS, LLM orchestration, RAG, bookings, idempotency, admin provisioning, static frontends, and deployment configuration.
- Compiled the Python tree.
- Ran the full test suite.
- Performed isolated local probes against the ASGI application and temporary databases.
- Demonstrated a cross-tenant live-session access flaw with controlled in-memory data and local API keys.
- Inspected the schema produced by normal application startup on a fresh SQLite database.

### Repository and change size

- 287 repository files.
- 175 Python files.
- Approximately 22,350 Python lines.
- Approximately 15,419 lines across 70 documentation files.
- Relative to the previous archive, only about 15 existing Python files changed and 9 Python files were added.
- Crucial voice/runtime modules such as `session_manager.py`, `brain.py`, `elevenlabs_tts.py`, `qwen3_tts.py`, `deepgram_stt.py`, provider base interfaces, and the speech sanitizer were not substantially redesigned.

### Build and test results

- `python -m compileall`: **passed**.
- Full test run: **37 failed, 464 passed, 37 skipped, 123 warnings**.

Failure groups:

| Group | Failures | Meaning |
|---|---:|---|
| Speech sanitizer | 13 | Numeric speech normalization does not satisfy its own contract when `num2words` is unavailable. |
| Multi-tenancy | 8 | The new SQL leak guard blocks legitimate operations and DB-backed-key behavior is not stable. |
| SQLite RAG | 7 | Default RAG path requires `sqlite-vec`, which is not declared/available. |
| ElevenLabs compatibility API | 5 | Global auth middleware conflicts with the endpoint’s own compatibility/auth contract. |
| Cartesia | 4 | Cartesia SDK unavailable in the audit environment; reproducibility/installation contract is incomplete. |

The failure count is not merely cosmetic. The red groups cover the exact subsystems newly advertised as remediated: multi-tenancy, provider compatibility, voice normalization, and default RAG.

---

# Delta from the previous audit

## Fully or substantially fixed

### 1. Booking write validation now fails closed

The prior system could continue a booking when its LLM write validator failed. The new write guard rejects uncertain or failed validation instead of silently permitting an irreversible action. This is a meaningful correctness improvement.

### 2. General API authentication now fails closed by default

`apps/api/app/middleware/auth.py` introduces global bearer-token enforcement and an explicit public-path allowlist. Non-public endpoints no longer default to world-open behavior.

### 3. Database-backed API keys are hashed

Plaintext tenant API keys are returned once, while SHA-256 hashes and display prefixes are stored. Constant-time comparison is used for environment-based keys.

### 4. Basic webhook authentication was added

- Vapi shared-secret enforcement was strengthened.
- Twilio HTTP webhook signature verification was added.
- WhatsApp and Telegram verification logic was added.

This reduces trivial HTTP webhook spoofing, though WebSocket and replay problems remain.

### 5. Tenant columns and migration scaffolding were introduced

Tenant, API-key, and idempotency models now exist. Core rows gained tenant fields, and Alembic scaffolding/migrations were added.

### 6. SQLite pragmas were improved

WAL, foreign keys, and busy-timeout handling improve the development database’s resilience.

### 7. Browser TTS repetition was fixed

The browser sentinel is no longer emitted once per sentence, eliminating the prior behavior in which the whole response could be spoken repeatedly.

### 8. Gemini credential handling and some dependency declarations improved

Previously identified provider-header and `audioop-lts` dependency issues were addressed.

### 9. Outbound has a stronger kill-switch posture

Outbound remains unsafe as a complete product, but disabling it by default is a valid containment control.

### 10. Baseline headers and CORS safeguards improved

Production wildcard-CORS protection and several response headers were added.

---

## Partially fixed

### Authentication and tenant isolation

The request and ORM boundaries now know about tenants, but the live runtime and external-provider routing do not. This creates the appearance of isolation without an end-to-end isolation invariant.

### Idempotency

A persistence table and helper exist, but the implementation does not atomically reserve work, bind a key to a request body, safely expire/reuse keys, or stop concurrent duplicates.

### RAG confidence

The previous top-score normalization was replaced, but rank-fusion scoring still presents weak top-ranked results as highly confident and remains uncalibrated.

### Migrations

Alembic files exist, but application startup continues to call `metadata.create_all`, bypassing migration state and creating a different schema.

### Twilio audio handling

A conversion helper and more explicit failure behavior exist, but the configured ElevenLabs adapter still emits MP3 and the phone path still refuses it.

### Webhook verification

HTTP signatures are checked, but event replay, tenant resolution, WebSocket authentication, durable processing, and failure semantics remain incomplete.

---

## Regressions or newly exposed contradictions

1. The default authentication middleware breaks public simulator/call/graph pages because their JavaScript does not attach authentication to protected API calls.
2. The global middleware blocks `/v1/*` before the ElevenLabs-compatible API can apply its own optional key contract.
3. The SQL leak guard blocks queries the new tenancy tests expect to succeed.
4. Normal fresh-database startup bypasses Alembic and creates nullable tenant columns, contradicting the migration’s NOT NULL target.
5. Documentation describes a stronger Postgres/RLS/async design than the source implements.
6. Documentation claims a green test state that the repository does not currently achieve.

---

# Critical release blockers

## CRITICAL-01 — Cross-tenant live-session hijacking

**Evidence**

`apps/api/app/routes/chat.py:44` retrieves a session using only the caller-supplied `session_id`:

```python
handle = session_manager.get_session(req.session_id)
```

The session manager stores live state in process dictionaries keyed only by session ID. It does not require or compare the authenticated tenant.

A local probe created a session for tenant A, authenticated as tenant B, then submitted tenant A’s known session ID to `/chat/turn`. The request returned HTTP 200 and accessed tenant A’s live business/session state.

**Impact**

A guessed, logged, leaked, or provider-exposed session ID can become a cross-tenant capability token. Attackers could read or influence another customer’s active conversation, trigger tools under the wrong business, or pollute transcripts and bookings.

**Required fix**

- Make the runtime key `(tenant_id, session_id)`, not `session_id`.
- Store immutable tenant ownership in every `CallSession`.
- Require tenant context in every `start_session`, `get_session`, `turn`, `end_session`, channel, webhook, and background-task API.
- Reject tenant mismatch before any transcript, LLM, RAG, calendar, CRM, TTS, or state access.
- Use opaque random internal session IDs and separately store provider call IDs.

---

## CRITICAL-02 — Tenant-aware database, tenant-unaware runtime

`SessionManager` remains a global singleton. Its business profile, calendar, sink, retriever, redactor, provider objects, and in-memory state are not resolved per tenant. Public provider routes set tenant context to `None`, and there is no authoritative mapping from inbound number, Vapi assistant, WhatsApp account, Telegram bot, or Twilio stream to a tenant and business.

**Impact**

Even after ORM filtering is repaired, live calls can use the wrong customer’s:

- business instructions;
- calendar;
- knowledge base;
- CRM sink;
- cloned voice;
- escalation destination;
- consent policy;
- pricing/policy data.

This is the core enterprise isolation failure.

**Required fix**

Build a tenant runtime resolver:

```text
provider account / inbound number / assistant ID / signed session ticket
    -> tenant_id
    -> business_id
    -> versioned runtime configuration
    -> provider credentials and voice profile
```

No public provider event should enter the agent before this resolution succeeds.

---

## CRITICAL-03 — Fresh startup creates a schema that bypasses migrations

`apps/api/app/db/session.py` continues to call `Base.metadata.create_all()` during normal initialization. On an empty SQLite database this creates `sessions.tenant_id`, `transcript.tenant_id`, and `bookings.tenant_id` as nullable, with no Alembic revision table.

The migration separately attempts to backfill and make those fields non-null.

**Impact**

- Production can silently run an unmigrated schema.
- A later Alembic upgrade may collide with already-created tables.
- Application behavior differs depending on whether the database was born through startup or Alembic.
- Tenant guarantees can be absent while code assumes they exist.

**Required fix**

- Remove `create_all` from production startup.
- Make startup fail when DB revision is behind or ahead of the application’s required revision.
- Keep `create_all` only in isolated unit-test fixtures, if at all.
- Add a migration integration test that starts from empty, upgrades to head, boots the app, and verifies constraints and indexes.

---

## CRITICAL-04 — Tenant fields are nullable and not relationally protected in models

`apps/api/app/db/models.py:104-159` defines nullable tenant IDs on core rows. `SessionRow.tenant_id`, `TranscriptRow.tenant_id`, and `BookingRow.tenant_id` have no foreign key to `tenants.id`. Transcript and booking ownership is connected through a globally keyed session ID rather than a composite tenant/session relationship.

**Impact**

Rows can be orphaned, assigned inconsistent tenant IDs, or linked to another tenant’s session. Application filters cannot compensate for an invalid underlying data model.

**Required fix**

- `tenant_id NOT NULL REFERENCES tenants(id)` everywhere.
- Unique/composite keys that include tenant where IDs are not globally generated and unguessable.
- Composite foreign keys for child rows: `(tenant_id, session_id)` -> sessions.
- Database row-level security in Postgres as defense in depth.
- Immutable tenant ID after insert.

---

## CRITICAL-05 — The SQL “tenant leak guard” is not a security boundary

The guard compiles SQL and looks for textual indications of `tenant_id` in `WHERE` or `ON`. It is brittle and already causes eight tenancy-test failures.

Weaknesses include:

- permissive behavior when compilation fails;
- false positives when tenant text appears but does not enforce the correct tenant;
- false negatives around aliases, joins, CTEs, subqueries, and generated SQL;
- select-only coverage;
- no protection for raw SQL or operations outside the ORM path;
- no database-enforced tenant policy.

**Impact**

The control can block valid code while missing actual data leaks—an especially dangerous combination because it creates false confidence.

**Required fix**

Use explicit repository methods plus Postgres RLS based on a transaction-local tenant setting. Retain tests that assert no cross-tenant reads/writes, but do not use SQL-string inspection as the primary security mechanism.

---

## CRITICAL-06 — ElevenLabs still cannot reliably drive a Twilio call

`apps/api/app/providers/tts/elevenlabs_tts.py` performs a complete HTTP synthesis and returns `audio/mpeg`.

`apps/api/app/routes/twilio.py:159-200` explicitly refuses MP3 rather than decoding it. The route itself warns that browser TTS cannot drive a phone call, while `.env.example` still defaults to `TTS_PROVIDER=browser`.

**Impact**

The customer can configure the paid ElevenLabs clone correctly and still receive silence or a failed audio conversion on the actual phone channel.

**Required fix**

- Request native 8 kHz μ-law output from ElevenLabs for Twilio.
- Configure format per transport, not globally.
- Add a contract test that synthesizes a known phrase, sends it through the Twilio framing path, and verifies valid 20 ms μ-law frames.
- Refuse application startup when a selected telephony transport and TTS format are incompatible.

---

## CRITICAL-07 — The Twilio path remains batch/turn-based, not human-like streaming

The file header still describes the MVP as “turn-based, not streaming.” The active path buffers audio, detects endpointing, calls batch STT, waits for a full `brain.turn`, waits for complete TTS, converts the whole result, then sends frames.

Existing streaming-capable STT/TTS adapters are not wired into the call path.

**Impact**

- Slow time to first audio.
- Long awkward silences.
- Poor interruption behavior.
- No incremental acknowledgment while tools run.
- Higher perceived robotic quality regardless of clone fidelity.

**Required fix**

Implement one full-duplex pipeline:

```text
Twilio frames -> streaming STT partials/finals -> semantic turn manager
-> streaming LLM clauses -> provider-native streaming TTS -> Twilio frames
```

Every layer must support cancellation and deadlines.

---

## CRITICAL-08 — Concurrent turns can corrupt one call

The Twilio route creates independent async tasks for utterance processing. There is no authoritative per-call actor, monotonic turn generation, or cancellation hierarchy.

**Failure sequence**

1. Utterance A starts processing.
2. Caller adds utterance B.
3. B starts independently.
4. A mutates state and starts speech.
5. B mutates the same state and starts another response.
6. Audio, tools, transcripts, and booking decisions race.

**Required fix**

Use one serialized `CallActor` per call with states such as:

```text
LISTENING -> THINKING -> TOOL_WAIT -> SPEAKING
              ^                         |
              +------ INTERRUPTED <-----+
```

The actor owns current STT, LLM, tool, and TTS task handles and cancels the entire superseded turn.

---

## CRITICAL-09 — Twilio Media Stream WebSocket is not authenticated

Twilio HTTP webhook HMAC verification does not authenticate the subsequent WebSocket by itself. `/twilio/stream` accepts a connection without a signed, short-lived session token bound to the call, tenant, stream, and expiration.

**Impact**

An attacker can open fake media streams, consume paid STT/LLM/TTS resources, inject transcript content, and potentially attach to or overwrite session state.

**Required fix**

- Mint a one-time signed stream token in the verified Twilio HTTP webhook.
- Put it in TwiML custom parameters.
- Verify signature, call SID, tenant, business, nonce, and expiry before accepting frames.
- Mark nonce consumed atomically.
- Apply frame, duration, and concurrency limits.

---

## CRITICAL-10 — Idempotency does not reserve work atomically

The helper is described as `check_or_reserve`, but it checks, executes the side effect, and inserts the completed response afterward.

Additional defects:

- unique constraint is `(tenant_id, key)` while lookup also includes `scope`;
- request-body hash helper is unused;
- same key with different payload can replay an unrelated result;
- simultaneous duplicates can both execute;
- expired rows remain and can block key reuse;
- persistence conflicts are swallowed;
- no in-progress, owner, lease, retry, or failure state.

**Impact**

Provider retries or concurrent client requests can double-book, redial, duplicate CRM writes, or send multiple messages.

**Required fix**

Use one transaction to insert an `IN_PROGRESS` reservation with unique `(tenant_id, scope, key)`, request hash, owner token, and lease expiry. Only the winner executes. Persist success/failure atomically, and use an outbox for external side effects.

---

## CRITICAL-11 — External booking is not atomic or idempotent

Google Calendar availability and event creation are separate calls. Calendar write and local `BookingRow` persistence are separate transactions. No deterministic event ID or provider idempotency token binds retries.

**Impact**

Two calls can reserve the same slot; the calendar can succeed while local persistence fails; a retry can create duplicate appointments.

**Required fix**

- Introduce a durable booking command with an idempotency key.
- Reserve a slot/version locally.
- Use provider-supported idempotency or deterministic extended properties.
- Reconcile provider events asynchronously.
- Store provider event ID and command state.
- Treat the local DB as a state machine, not a one-shot log.

---

## CRITICAL-12 — Public provider routes have no authoritative tenant resolution

Public routes deliberately bypass bearer auth. That is correct for signed provider webhooks, but they set `request.state.tenant_id=None`. The code does not consistently derive tenant ownership from verified provider identifiers.

**Impact**

Signed webhooks can still be processed under a default/global business. Signature verification proves the sender, not the intended tenant.

**Required fix**

Resolve and bind tenant from immutable provider-side identifiers:

- Twilio account SID + inbound number + call SID;
- Vapi organization/assistant/phone-number IDs;
- WhatsApp business account + phone-number ID;
- Telegram bot identity or dedicated webhook secret;
- signed browser session ticket.

Reject unknown mappings rather than falling back to “default.”

---

## CRITICAL-13 — Arbitrary Qwen reference voice inputs can become SSRF/local-file/resource abuse

The Qwen TTS adapter treats dynamic voice/reference inputs as paths or URLs and performs heavyweight synchronous inference inside async methods. Generic authenticated voice endpoints accept a caller-supplied voice value without a first-class consent-approved voice registry.

**Impact**

Depending on the exact runtime and libraries, callers may trigger remote fetches, local-file access attempts, unexpected model work, GPU exhaustion, or unauthorized voice cloning.

**Required fix**

- Never accept arbitrary reference paths/URLs from API callers.
- Accept only an internal `voice_profile_id` owned by the tenant.
- Resolve to immutable encrypted artifacts under server control.
- Queue GPU work with concurrency and memory limits.
- Store consent, allowed use, version, and revocation state.

---

## CRITICAL-14 — No voice-clone governance domain exists

The system still lacks a complete model for:

- voice owner identity;
- signed consent artifact;
- allowed business/use cases;
- source audio provenance;
- clone provider and model version;
- approval status;
- revocation and deletion;
- fallback voice;
- per-generation audit trail;
- pronunciation dictionaries;
- tenant-specific voice assignment.

**Impact**

A “voice ID” environment variable is not enough for enterprise voice cloning. It creates publicity-rights, impersonation, employee-consent, incident-response, and vendor-portability risks.

**Required fix**

Create first-class `VoiceProfile`, `VoiceConsent`, `VoiceVersion`, `PronunciationDictionary`, and `VoiceUsageEvent` records. The live runtime must resolve voices through this registry only.

---

## CRITICAL-15 — No durable distributed live state

Live state remains in process-local dictionaries. Restarting a worker loses calls; multiple workers see different states; load balancing can route the next turn to a worker that has never seen the session.

**Impact**

- calls break during deploys or crashes;
- no horizontal scaling;
- duplicate or missing tools;
- no reliable supervisor takeover;
- no multi-region continuity.

**Required fix**

Use sticky transport only as an optimization. Put durable session metadata and event sequencing in Postgres/Redis, and use a single-owner call actor with lease/heartbeat semantics.

---

## CRITICAL-16 — No rate, quota, size, or provider-spend enforcement

Authentication is not resource control. The code still lacks robust per-tenant:

- requests/minute;
- concurrent calls;
- audio frame rate and maximum call duration;
- upload/media size;
- STT/TTS/LLM token/credit budgets;
- GPU queue limits;
- outbound batch caps enforced independently of request data;
- circuit breakers tied to provider cost and tenant plan.

**Impact**

One key, compromised integration, malformed stream, or customer bug can exhaust credits, GPU memory, workers, or provider limits.

---

## CRITICAL-17 — Outbound remains unsafe beyond the kill switch

The disabled-by-default posture is good, but the underlying product still lacks a complete durable consent and campaign-control plane. Client-supplied DNC lists and calling parameters cannot be the authority for legal compliance.

Required before enabling any real outbound tenant:

- immutable consent evidence and purpose;
- jurisdiction/time-zone resolution;
- federal, state, and tenant DNC checks;
- frequency caps;
- AI disclosure policy;
- campaign approval;
- attempt reservation;
- abandonment/AMD tracking;
- caller-ID ownership verification;
- audit and suppression propagation.

---

## CRITICAL-18 — The deployment is not reproducible or enforceably healthy

There is no complete lockfile, container build, CI workflow, migration gate, lint/type/security configuration, coverage gate, or production-readiness health check.

The default environment enables SQLite RAG but does not declare all required dependencies. `.env.example` defaults to browser TTS, which cannot drive Twilio. Documentation and tests disagree on authentication contracts.

**Impact**

A system can pass on one developer machine and fail at startup or mid-call elsewhere. Enterprise claims are not auditable without deterministic builds and evidence-producing CI.

---

# Detailed findings by subsystem

## A. Authentication and authorization

### A-01 — Disabled tenants can retain access

The DB key resolver filters revoked keys but does not join/check `Tenant.disabled_at`. Disabling a tenant does not reliably disable its unrevoked API keys.

### A-02 — Key revocation is eventually consistent per worker

The resolver caches DB key hashes for 30 seconds in process memory. Cache invalidation only affects the process receiving the admin action; other workers can continue accepting a revoked key until expiry.

### A-03 — No key-scoped permissions

All tenant API keys are effectively full-tenant keys. There are no scopes such as `calls:write`, `sessions:read`, `voices:use`, `admin:billing`, or `webhooks:ingest`.

### A-04 — Admin uses one shared bearer secret

The admin control plane has no human identity, SSO, RBAC, MFA, session audit, approval workflow, or individual accountability.

### A-05 — Admin key lifecycle is incomplete

The code can create keys, but a complete list/revoke/rotate/expire workflow is absent or incomplete. Rotation should support overlapping validity and audit attribution.

### A-06 — Public static applications and protected APIs conflict

The call widget, simulator, and observability graph are public static assets, but their fetches do not supply a bearer token to protected APIs. Under default auth they fail with 401.

Embedding a permanent tenant key in JavaScript would be worse. Use a backend exchange for a short-lived, narrowly scoped call/session token.

### A-07 — ElevenLabs-compatible API has conflicting auth layers

`/v1/*` is not on the public allowlist, so global bearer auth intercepts requests before the compatibility router’s own API-key behavior. Five compatibility tests fail for this reason.

### A-08 — Authentication performs synchronous DB work in async middleware

Every uncached DB-backed key request performs a synchronous SQLAlchemy query and attempted write to `last_used_at` on the event loop.

### A-09 — `last_used_at` creates avoidable write amplification

Updating a key row on request authentication can turn every cache miss into a write and lock hotspot. Sample/aggregate usage asynchronously instead.

### A-10 — No nonce/replay scheme for browser sessions

The browser call experience needs one-time or short-lived session capabilities, not a reusable tenant API key.

---

## B. Multi-tenancy and data isolation

### B-01 — `business_id` is still used as a tenant proxy

`apps/api/app/routes/sessions.py:14-34` explicitly documents interim behavior in which non-default tenant ownership is inferred from `business_id == tenant_id`, while the `default` tenant can see everything.

This contradicts the new tenant columns and is not safe for a SaaS model where one tenant can own multiple businesses/locations.

### B-02 — Session detail child queries are not independently tenant constrained

After an interim ownership check, transcript/bookings are queried only by `session_id`. Composite ownership should be enforced in the query and schema.

### B-03 — Tenant ID is not propagated into all background tasks

Context variables propagate to newly created tasks in many Python cases, but they are not a substitute for explicit ownership. Detached tasks, provider callbacks, worker queues, and process boundaries can lose context.

### B-04 — Tenant configuration is stored but not used by runtime

Admin provisioning appends arbitrary business-profile dictionaries to `Tenant.metadata_json`. The live agent still loads profile files/global configuration, so onboarding can report success without changing actual agent behavior.

### B-05 — No first-class business/location model

Enterprise requirements need tenant -> organization -> business/location -> phone/channel -> assistant/config version. A JSON array on Tenant cannot provide referential integrity, uniqueness, lifecycle, auditability, or efficient routing.

### B-06 — No tenant-specific provider credentials

Global environment credentials prevent secure BYO-key, per-tenant spend, residency, provider selection, and revocation.

### B-07 — No encryption boundary per tenant

Recordings, voice artifacts, transcripts, and secrets do not have per-tenant KMS/envelope-encryption design in code.

### B-08 — No tenant deletion/export workflow

GDPR/CCPA and enterprise offboarding require inventory, export, legal hold, deletion, provider propagation, and audit evidence.

---

## C. Live call state and concurrency

### C-01 — Session IDs can be overwritten

Starting a session with an existing external ID can replace process state unless explicitly guarded everywhere.

### C-02 — No per-call mutual exclusion

Chat, channel, Twilio, webhook, and end-session operations can access the same `CallState` concurrently.

### C-03 — State removal precedes durable completion

End flows can remove active state before sink/extraction/write-back completes, making retries and recovery difficult.

### C-04 — Transcript persistence uses count/slice-style incremental logic

Concurrent writes can cause duplicate or skipped transcript entries. Persist events by immutable turn/event IDs.

### C-05 — No TTL/idle reaper with durable finalization

Process-local sessions need cleanup, but cleanup also needs to finalize transcripts, disposition, provider resources, and billing exactly once.

### C-06 — No deploy draining

There is no readiness transition and worker drain protocol that stops new calls, finishes/transfers existing calls, and releases leases during deploys.

### C-07 — No call event log

A durable append-only event stream would make ordering, replay, debugging, idempotency, and supervisor takeover much safer than mutating one in-memory object.

### C-08 — No explicit ended/escalated invariant

The brain and routes can continue accepting turns after a logical escalation/end unless every caller checks state consistently.

---

## D. Voice quality and cloned-voice productization

### D-01 — ElevenLabs adapter is non-streaming

It opens a new HTTP client/request and waits for the complete audio response. It does not use provider WebSocket streaming or a persistent client.

### D-02 — The phone path does not use `stream_sentences`

Even the repository’s sentence/chunk streaming abstraction is not applied to Twilio. It remains mainly a browser/API feature.

### D-03 — Base “streaming” is post-hoc chunking

The base TTS interface often synthesizes sequential sentence chunks after complete LLM text exists. That is not token-to-audio streaming.

### D-04 — ElevenLabs-compatible “stream” buffers the whole file

The compatibility endpoint synthesizes complete audio, then yields memory slices. It lowers client memory burst slightly but does not improve time-to-first-audio from the provider.

### D-05 — Compatibility request controls are ignored

Requested model, output format, and voice settings are not fully honored, so SDK clients receive an API shape without equivalent semantics.

### D-06 — Speech sanitizer strips expression tags

`packages/core_agent/speech_sanitizer.py:38-44` removes all square-bracketed content. This deletes ElevenLabs v3-style controls such as `[laughs]`, `[whispers]`, and `[sighs]` along with unsafe metadata.

The right fix is not to allow arbitrary tags. The agent should return structured delivery metadata and a provider renderer should map an allowlisted emotion/action into vendor syntax.

### D-07 — The system infers emotion from text while claiming acoustic understanding

The brain sees transcript text, not pitch, energy, speaking rate, overlap, hesitation, or acoustic emotion. Prompt language about caller “tone” can cause unsupported emotional judgments.

### D-08 — No structured speech plan

There is no canonical response object such as:

```json
{
  "spoken_text": "I understand. Let me check Tuesday afternoon.",
  "delivery": {
    "style": "reassuring",
    "intensity": 0.3,
    "pace": "slightly_slow",
    "pause_ms": [0, 180]
  }
}
```

Without this, factual content and provider-specific expressive markup are entangled.

### D-09 — No pronunciation service

A production clone needs per-tenant dictionaries for names, medicines, streets, menu items, Urdu/Arabic terms, acronyms, and brand vocabulary.

### D-10 — Numeric normalization silently degrades

When `num2words` is unavailable, `_int_to_words` returns digits rather than failing startup or using a correct internal fallback. Thirteen tests demonstrate mispronounced currency, times, dates, years, phone numbers, percentages, and counts.

### D-11 — Qwen inference blocks the event loop

Heavy local model generation runs synchronously inside async methods. One GPU inference can stall unrelated calls on the worker.

### D-12 — Qwen model/config does not guarantee actual cloning

The default model/configuration can point to a custom/preset voice path rather than the dedicated cloning workflow. A “Qwen3-TTS” label does not prove reference-voice cloning is active.

### D-13 — No model warm-up/readiness contract

First-call model download/load or `torch.hub` VAD loading can create seconds/minutes of blocking latency and supply-chain/network dependence.

### D-14 — No transport-specific codec quality tests

A clone that sounds excellent at high sample rate can degrade after 8 kHz μ-law. The repository lacks golden telephony samples and objective/human comparison through the actual codec.

### D-15 — No voice fallback policy

If a voice is revoked, provider quota is exhausted, or synthesis fails, the system needs a tenant-approved fallback and an auditable reason—not arbitrary provider fallback with a different identity.

### D-16 — No clone-quality evaluation

Required measures include identity similarity, pronunciation accuracy, naturalness, emotional appropriateness, time-to-first-audio, interruption cancellation, and blind human preference.

### D-17 — Fixed greetings are not optimized separately

For best perceived quality, fixed greeting/hold/transfer/closing prompts can be pre-generated with a high-expression model while arbitrary responses use a low-latency model. No such asset/version strategy exists.

### D-18 — No multilingual voice policy

Language detection, code switching, accent policy, clone quality by language, pronunciation, and approved language support are not first-class configuration.

---

## E. Turn-taking, interruption, and latency

### E-01 — Fixed silence threshold is not semantic endpointing

Approximately 700 ms of silence cannot distinguish a completed answer from a thinking pause.

### E-02 — Greeting blocks inbound receive handling

Awaiting greeting generation/playback in the receive loop prevents robust early barge-in and can build up inbound frames.

### E-03 — Barge-in repeatedly batch-transcribes audio snapshots

This is expensive, introduces latency, and can trigger duplicate/misaligned transcripts.

### E-04 — Tail audio can be lost after interruption

Pending barge text and buffer transitions do not guarantee that audio arriving around the cancellation boundary is preserved and attributed to the new turn.

### E-05 — Cancellation is incomplete

Filler, LLM, TTS, and playback tasks are not managed under one cancellation tree. Cancelled tasks are not always awaited, and concurrent WebSocket sends can remain.

### E-06 — No latency budget enforcement

There are no enforced budgets for:

- partial transcript latency;
- endpoint decision;
- first LLM token;
- first audio;
- tool acknowledgment;
- interruption stop time.

### E-07 — No incremental LLM response policy

The system waits for a complete reply rather than emitting safe speakable clauses while the remainder generates.

### E-08 — No safe filler/acknowledgment controller

Acknowledgments should be selected based on expected tool latency and repetition history, not generated freely or played concurrently with final output.

### E-09 — No backchannel distinction

The runtime does not robustly distinguish “mm-hm,” “yeah,” or background speech from a true interruption.

### E-10 — No caller-overlap metrics

Human-likeness cannot improve without measuring false interruptions, ignored interruptions, overlap duration, and repair rate.

---

## F. STT and audio ingestion

### F-01 — Deepgram streaming adapter is unused by Twilio

Streaming code exists but is not connected to the main phone path.

### F-02 — Streaming STT queue is unbounded

No backpressure or memory limit protects against provider delay or producer overload.

### F-03 — STT errors can look like normal stream completion

Errors are logged and can terminate iteration without a typed failure reaching the call state machine.

### F-04 — Hard-coded language/endpoint settings

`en-US` and endpoint timings are not tenant/language/use-case specific.

### F-05 — Cancellation cleanup is incomplete

Background receive/send tasks should be cancelled and awaited deterministically.

### F-06 — VAD can load remote code/model on first live frame

Synchronous `torch.hub.load(..., trust_repo=True)` during a call can block the event loop and adds a supply-chain/network dependency at the worst time.

### F-07 — No audio pre-roll/ring buffer policy

Speech onset can be clipped when VAD transitions from silence to speech.

### F-08 — No maximum frame/call/idle policy

Malformed or endless streams can consume unbounded resources.

### F-09 — Raw transcript logging leaks PII

Multiple call/webhook paths log transcript snippets. Redaction must occur before persistence and routine logs, with a restricted forensic path for exceptional debugging.

---

## G. Agent reasoning and tools

### G-01 — Full transcript is repeatedly sent

Each loop reconstructs the complete conversational context, increasing latency, token cost, and prompt-injection surface.

### G-02 — One caller turn can trigger many model calls

The brain can perform multiple tool loops plus extraction. There is no strict per-turn deadline, cost budget, or maximum external side-effect plan.

### G-03 — Tool exceptions are not uniformly converted into typed outcomes

Unexpected handler errors can break the turn or produce inconsistent user messages.

### G-04 — Escalation is a tool result, not a guaranteed state transition

Calling an escalation tool should atomically close further autonomous actions and begin a transfer/fallback workflow.

### G-05 — LLM outage response is misleading

Asking the caller to repeat when the system itself failed implies recognition error rather than service outage. After one bounded retry, explain briefly and transfer/callback.

### G-06 — Extractor includes assistant statements

Assistant hallucinations can be re-ingested as facts during field extraction. Caller-provided and tool-verified facts need provenance.

### G-07 — Extraction failure can overwrite good state

Malformed model output should not replace previously verified values with empty/default values.

### G-08 — No field provenance/versioning

Each field should record source turn, confidence, verification, and supersession history.

### G-09 — Prompt-injected RAG remains a risk

Retrieved business documents are inserted into the model context without a robust untrusted-data boundary and policy engine. Documents can contain instructions that compete with the system prompt.

### G-10 — Unknown service behavior is too permissive

Booking duration/default behavior can fall back instead of requiring a configured service and authoritative duration.

### G-11 — Tool schemas are not sufficient authorization

Even well-formed tool arguments need deterministic tenant, role, caller-verification, consent, and state-policy checks.

### G-12 — No deterministic response planner

Facts, confirmations, action outcomes, and speech delivery should be assembled through a constrained planner before TTS.

---

## H. RAG and knowledge correctness

### H-01 — Default RAG cannot install cleanly

SQLite vector support is enabled by default, but `sqlite-vec` is absent from the declared runtime dependencies. Local embedding paths also rely on packages not included in a deterministic install profile.

### H-02 — Failures silently disable RAG

A startup/init failure can leave the agent operating without its knowledge source while appearing healthy.

### H-03 — Vector candidate search is global before business filtering

Searching globally for `top_k * 4`, then filtering by business, allows other businesses’ vectors to crowd out the correct tenant’s results. It is a correctness issue and can become a side-channel.

### H-04 — RAG scopes by business, not tenant and business

Business IDs are not guaranteed globally unique and should not be the security boundary.

### H-05 — Rank-fusion confidence is still not confidence

Reciprocal-rank fusion captures rank agreement, not semantic strength or probability of correctness. A weak result ranked first in both channels can appear near maximum confidence.

### H-06 — Rank ceiling math is internally inconsistent

Rank indexing and ceiling assumptions differ, introducing avoidable score distortion.

### H-07 — Embedding model/version/dimension are not persisted per index

Changing the model can leave incompatible vectors in one store without a migration/reindex contract.

### H-08 — Local embedding work blocks async paths

Model load and inference are synchronous and can download/load on first request.

### H-09 — Row-ID derivation can collide

Hash-derived integer IDs need collision detection or a stored mapping with a real primary key.

### H-10 — Mapping and scan behavior is inefficient

Reconstructing row mappings/scanning data creates avoidable O(N) work.

### H-11 — Connection cleanup is not uniformly protected by `finally`

Exceptions can leak resources/locks.

### H-12 — No document lifecycle

There is no strong version, effective date, approval, supersession, source authority, or rollback model for business policies.

### H-13 — No answer-level source enforcement

High-risk facts such as price, policy, availability, and medical/admin details should require retrieved authoritative evidence or refusal/transfer.

### H-14 — No tenant ingestion authorization and malware/content controls

Enterprise RAG needs file/type/size limits, content scanning, parsing isolation, access-control inheritance, and provenance.

---

## I. Webhooks, messaging, and external side effects

### I-01 — HTTP authenticity does not provide replay protection

Valid signed events can be resent. Routes need durable event IDs, timestamps, nonce windows, and idempotent processing.

### I-02 — Vapi logs PII snippets

Payload keys and recent user/reply text can enter ordinary logs.

### I-03 — Vapi processing failures can be acknowledged as success

Disposition or downstream errors are caught while a success response is still returned, preventing provider retry while losing work.

### I-04 — WhatsApp/Telegram dedup is incomplete

Repeated provider deliveries can generate repeated agent turns and replies.

### I-05 — Messaging tenant resolution is incomplete

Bot/account/phone-number identity must map to tenant and business before session lookup.

### I-06 — Send failures are swallowed

The webhook can return success even if the response was never delivered.

### I-07 — External media limits remain weak

Voice-note/media download needs strict URL allowlisting, timeout, redirect, content-length, streaming-byte, MIME, and decompression limits.

### I-08 — No webhook inbox/outbox

Provider events should be durably stored before acknowledgment, then processed by an idempotent worker; outgoing messages should use a transactional outbox.

### I-09 — No dead-letter/replay UI

Operations need visibility into failed events and controlled replay.

---

## J. Bookings, calendars, CRM, and sinks

### J-01 — Availability check and creation race

Two workers can both observe an open slot and write it.

### J-02 — Google API calls block async workers

The Google client is synchronous and should run in a bounded thread pool or worker.

### J-03 — Time-zone serialization is unsafe

Appending `Z` to `isoformat()` can label local/offset values as UTC incorrectly.

### J-04 — Slot listing causes repeated provider calls

N+1 availability checks increase latency and quota use.

### J-05 — Fake calendar fails open on corrupt data

Corrupted JSON can appear as an empty/available calendar, enabling invalid demo bookings.

### J-06 — Fake calendar writes are not crash-safe

Process-local locks and non-atomic file replacement do not protect multi-process use or partial writes.

### J-07 — Timestamp-derived event IDs can collide

Use random/deterministic idempotent identifiers.

### J-08 — Duration is hard-coded during persistence

The saved booking can disagree with the selected service/tool result.

### J-09 — Sink failures remain weakly surfaced

CRM/Sheets failures should produce durable retryable work, not be swallowed to keep the demo moving.

### J-10 — No reconciliation jobs

External calendars/CRMs can be edited independently. The system needs periodic reconciliation and conflict resolution.

### J-11 — No structured human transfer implementation

A real transfer requires target resolution, availability, whisper brief, failed-transfer fallback, transcript handoff, and takeover semantics—not merely an “escalated” flag.

---

## K. Provider and async architecture

### K-01 — Sync SQLAlchemy is used inside async endpoints

The code describes Postgres async behavior but uses `create_engine` and synchronous sessions. A `postgresql+asyncpg://` URL is not a complete async implementation.

### K-02 — Provider clients are frequently recreated

Repeated HTTP client construction loses connection pooling and increases latency.

### K-03 — No uniform timeout/retry/circuit policy

Timeouts and fallbacks are inconsistent across providers and not governed by a central deadline.

### K-04 — Fallback can violate tenant policy

A provider fallback may move data to a provider/region/model not approved for the tenant or change the cloned voice.

### K-05 — Mutable list defaults exist in response/result models

Shared mutable defaults such as `tool_calls=[]` and `tool_results=[]` can leak state between instances depending on model/dataclass behavior and should use factories.

### K-06 — No provider capability negotiation

The transport should select providers/formats based on required codec, streaming, language, expression, clone, compliance, and region capabilities.

### K-07 — No graceful shutdown/close hooks

Provider clients, DB resources, model workers, and active call actors are not consistently drained and closed.

---

## L. Deployment, observability, and quality engineering

### L-01 — No deterministic dependency lock

Requirements are not enough to reproduce exact provider SDK and transitive dependency behavior.

### L-02 — No production container contract

There is no authoritative Docker image, non-root runtime, health/readiness command, model/artifact strategy, or image vulnerability scan.

### L-03 — No CI gate

The repository contains no pipeline requiring tests, migration tests, type checks, lint, dependency audit, secret scan, or security tests before merge.

### L-04 — Health endpoint is mainly liveness

It does not prove DB revision, tenant resolver, Redis/session store, selected STT/LLM/TTS, calendar, or queue readiness.

### L-05 — Documentation overstates implementation

The enterprise documents describe Postgres RLS, async behavior, transactional tests, and stronger completion status not reflected by the source/test run.

### L-06 — No SLO-based telemetry

Required metrics include first partial, endpoint latency, first token, first audio, interruption stop time, tool latency, provider errors, call completion, transfer success, booking correctness, and cost.

### L-07 — No immutable audit trail

Operational traces are not a tamper-evident business audit log.

### L-08 — No recording/transcript retention engine

Retention must support tenant policy, legal hold, deletion, redaction status, encryption key, and export.

### L-09 — No secrets manager integration

Global environment variables do not provide tenant-scoped rotation, audit, KMS integration, or least privilege.

### L-10 — No incident controls

There is no system-wide provider kill switch, tenant quarantine, voice revocation, campaign stop, key mass revoke, or emergency read-only mode exposed through a controlled operations plane.

### L-11 — HSTS/CSP/environment policy needs refinement

HSTS should be set only behind verified HTTPS; public web UIs need a strong Content Security Policy and secure token flow.

### L-12 — Warnings are not treated as debt

123 warnings in the test run can hide deprecations and future breakage. Establish a warning budget and fail on new warnings.

---

# What is still missing for an amazing human-like cloned voice

The repository currently has **voice provider adapters**, not a complete voice product. The following subsystems are required.

## 1. Voice registry

A per-tenant registry should contain:

```text
VoiceProfile
- id
- tenant_id
- display_name
- owner_identity_id
- default_language
- status
- fallback_voice_profile_id

VoiceConsent
- voice_profile_id
- signed_artifact_uri
- verified_by
- verified_at
- permitted_uses
- prohibited_uses
- expires_at / revoked_at

VoiceVersion
- provider
- provider_voice_id
- model
- source_audio_version
- settings
- quality_scores
- approved_at

PronunciationEntry
- phrase
- language
- phoneme/alias
- scope
- version
```

The API accepts `voice_profile_id`, never arbitrary provider IDs, URLs, or file paths.

## 2. Full-duplex call actor

One actor owns each call. It receives audio/events sequentially, starts streaming STT, commits semantic turns, streams safe response clauses, and cancels superseded work.

Required cancellation hierarchy:

```text
Call
  Turn generation N
    STT endpoint task
    LLM stream
    Tool task(s)
    TTS stream
    Playback cursor
```

When the caller interrupts, generation N is invalidated and every child is cancelled/awaited.

## 3. Structured speech director

The LLM should not directly improvise vendor tags. It should emit constrained content and delivery intent. A deterministic policy verifies facts, confirmation requirements, empathy appropriateness, length, and sensitive-context rules. Then a provider renderer translates delivery intent into ElevenLabs/Qwen/Cartesia controls.

## 4. Prosody-aware input

Use acoustic features or an STT model capable of exposing relevant speech events. Do not claim to know emotion from transcript alone. At minimum capture:

- speaking rate;
- energy/pitch trend;
- hesitation/pause duration;
- overlap/interruption;
- repeated correction;
- explicit sentiment words.

Keep emotional response conservative and policy-bound.

## 5. Telephony-native audio

For Twilio, request/output native μ-law 8 kHz, frame into 20 ms packets, and measure the final phone-channel result. Avoid MP3 transcoding in the hot path.

## 6. Separate fixed and dynamic speech

Pre-generate approved high-expression greetings, hold phrases, transfer phrases, disclosure, and closings. Use low-latency streaming for dynamic responses. Version all assets by voice and policy.

## 7. Conversation repair library

Implement deterministic patterns for:

- low-confidence name/number/date;
- conflict between caller statement and tool result;
- missed audio;
- ambiguous time;
- failed tool;
- interruption;
- caller frustration;
- emergency/sensitive language;
- transfer failure.

Natural repair behavior matters more than random laughter or filler words.

## 8. Voice QA harness

Every build should run scripted calls and record:

- time to first partial transcript;
- endpoint delay;
- first LLM token;
- first audio;
- interruption stop latency;
- false-interruption rate;
- pronunciation score;
- identity similarity;
- emotional appropriateness;
- factual/tool correctness;
- blind human preference;
- output after 8 kHz μ-law.

---

# Recommended remediation order

Claude’s roadmap should be reordered. Do not add more vertical integrations or enterprise claims until the isolation and runtime foundations are correct.

## Phase 0 — Freeze and establish truth (1–3 engineering days)

1. Stop feature additions.
2. Make the existing suite green in a clean environment.
3. Add a lockfile and CI.
4. Align README/docs with actual capabilities.
5. Add migration-from-empty and boot tests.
6. Classify every skipped test and prohibit unexplained skips.

**Exit gate:** clean checkout -> one documented command -> reproducible environment -> migrations -> all required tests pass.

## Phase 1 — Rebuild tenancy as an invariant (approximately 1–2 weeks)

1. First-class tenant/business/location/channel/config models.
2. Non-null tenant FKs and composite relationships.
3. Postgres as the production database.
4. Real RLS policies tested under multiple DB roles/transactions.
5. Tenant-aware repositories and explicit method parameters.
6. Provider identifier -> tenant/business mapping.
7. Tenant-owned provider credentials and voice profiles.
8. Remove `default sees all` behavior.
9. Remove production `create_all`.

**Exit gate:** automated adversarial tests cannot read, mutate, invoke tools, use voices, or consume provider spend across tenants—through HTTP, live sessions, webhooks, queues, or direct repository calls.

## Phase 2 — Durable per-call actor (approximately 1–2 weeks)

1. Replace process dictionaries with a call actor registry and durable metadata.
2. Use `(tenant_id, internal_call_id)` ownership.
3. Add one-time signed WebSocket tokens.
4. Serialize turns and add generation cancellation.
5. Add event IDs, sequence numbers, lease/heartbeat, idle/finalization.
6. Add deploy drain and recovery.

**Exit gate:** concurrent utterances, provider retries, worker restart, and multi-worker routing do not corrupt or cross calls.

## Phase 3 — Fix the actual voice path (approximately 1–2 weeks)

1. Native ElevenLabs 8 kHz μ-law.
2. Persistent provider clients.
3. Streaming STT wired to Twilio.
4. Streaming LLM clauses.
5. Streaming TTS frames.
6. Semantic endpointing and pre-roll.
7. Barge-in cancellation under a measurable target.
8. Structured speech director and pronunciation registry.
9. Two consent-approved voice variants for benchmarking.

**Exit gate:** real PSTN test achieves defined latency, no overlap corruption, correct interruption, and acceptable clone identity/naturalness.

## Phase 4 — Make side effects exactly-once in business terms (approximately 1 week)

1. Atomic idempotency reservation.
2. Request hashes and scoped keys.
3. Webhook inbox and transactional outbox.
4. Durable booking command/state machine.
5. Calendar/CRM reconciliation.
6. Retry/dead-letter operations UI.

**Exit gate:** replay and concurrency tests cannot create duplicate bookings, calls, CRM records, or messages.

## Phase 5 — Operational controls (approximately 1–2 weeks)

1. Tenant quotas, call duration, media size, provider budgets, GPU queue limits.
2. Readiness checks and provider capability validation.
3. Redaction-before-log/store.
4. Retention/deletion/legal hold.
5. Voice revocation and emergency kill switches.
6. SLO dashboards and alerts.
7. Secrets manager and key rotation.
8. Backup/restore and disaster exercises.

## Phase 6 — Then add vertical integrations and compliance evidence

Only after the above foundations:

- calendar/POS/EHR/CRM integrations;
- SSO/RBAC;
- billing;
- recording portal;
- human supervisor/takeover;
- SOC 2 evidence collection;
- BAA/subprocessor path;
- multi-region/VPC/on-prem packaging.

---

# Minimum external-pilot acceptance gates

The repository should not serve an external customer until all of these are true:

## Security and tenancy

- [ ] Every live session has immutable tenant/business ownership.
- [ ] Provider webhooks and WebSockets resolve and verify tenant ownership.
- [ ] Cross-tenant tests cover HTTP, runtime, DB, RAG, tools, voices, recordings, and admin.
- [ ] Tenant IDs are non-null FKs with DB-enforced policies.
- [ ] No `default tenant sees all` behavior in production.
- [ ] Rate, quota, and spend limits are active.

## Voice runtime

- [ ] ElevenLabs clone plays over Twilio using a supported telephony codec.
- [ ] STT, LLM, and TTS are truly streaming in the phone path.
- [ ] Per-call actor prevents overlapping turns.
- [ ] Barge-in cancels active speech and generation reliably.
- [ ] Numeric/name/date pronunciation tests pass.
- [ ] Voice consent, version, use scope, and revocation are enforced.
- [ ] Fallback voice is tenant-approved.

## Correctness

- [ ] Booking and external actions have atomic idempotency.
- [ ] Availability/write races are handled.
- [ ] RAG requires an authoritative source for sensitive facts.
- [ ] No silent degradation when RAG/provider dependencies are missing.
- [ ] Human transfer has a real operational path.

## Reliability and operations

- [ ] Clean CI is green.
- [ ] Database is created only through migrations.
- [ ] Production build is locked and reproducible.
- [ ] Readiness verifies DB revision and critical dependencies.
- [ ] Logs are redacted and tenant-scoped.
- [ ] Failed provider events are durable and replayable.
- [ ] Backup/restore and incident kill-switches are tested.

---

# Final assessment

The new repository is better than the prior archive, and Claude deserves credit for addressing a number of concrete audit items rather than merely changing labels. Authentication, tenant tables, webhook checks, migration files, and fail-closed booking validation are useful foundations.

But the implementation currently commits the classic enterprise-platform mistake: **it adds tenant IDs to requests and rows without making the runtime itself tenant-owned**. It also adds “streaming” adapters without replacing the batch phone-call execution path.

The most important conclusion is therefore:

> Do not spend the next sprint on more integrations, dashboards, SOC 2 paperwork, or extra voice models. First rebuild the live runtime around immutable tenant ownership and a single cancellable streaming call actor.

Until that is done, the product can still leak across tenants, fail to play the paid ElevenLabs clone on Twilio, overlap turns, lose state across workers, and duplicate irreversible actions. Those are product-defining failures, not edge cases.
