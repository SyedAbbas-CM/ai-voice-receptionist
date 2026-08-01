# VoiceOps Codebase Audit

**Repository:** `voiceops-codebase-2026-08-01`  
**Audit date:** 2026-08-01  
**Scope:** Static application-security, correctness, architecture, reliability, privacy, integration, frontend, test, and deployment review.

## Executive verdict

This repository is a **well-developed R&D/demo prototype**, not a production-ready voice-operations platform. It has meaningful modularity and a large test suite, but its production surface is unsafe by default and several core workflows are not transactionally or operationally reliable.

The most serious issue is the combination of:

1. **No general authentication or tenant authorization** on routes that expose transcripts, bookings, debug traces, paid provider calls, Google Sheets reads, and real outbound dialing.
2. **Outbound consent enforcement disabled by default** and not wired into the production outbound route.
3. **Unauthenticated or weakly authenticated webhooks**, allowing fabricated provider events and duplicate side effects.
4. **In-memory session and outbound state**, making multi-worker deployment, restarts, retries, and concurrent requests unsafe.
5. **Non-idempotent booking and outbound workflows**, which can double-book, redial, lose webhook context, or acknowledge failures as successes.
6. **Fail-open and swallowed-error behavior** in booking validation, extraction, sinks, RAG, and outbound disposition processing.
7. **A clean installation is not reproducible or fully runnable** because required dependencies and configuration assets are missing.

A public deployment of the current repository could leak customer information, incur LLM/STT/TTS/telephony charges, permit unauthorized outbound calls, and produce duplicate or lost bookings.

---

## Audit method and limitations

I inspected approximately **196 files / 34,500 lines**, including all application Python, browser clients, tests, configuration, docs, schemas, provider adapters, compliance code, RAG, calendar/CRM integrations, and outbound dialing logic.

I did **not** execute arbitrary application scripts or contact external providers. I performed safe static inspection, Python bytecode compilation, repository searches, and the included test suite. This is not a formal penetration test, cloud configuration audit, legal opinion, or dependency CVE scan. A complete CVE scan was not possible because there is no lockfile or resolved dependency manifest.

### Verification results

- `python -m compileall`: **passed**.
- Full API tests on the available environment: **20 failed, 368 passed, 36 skipped** before the configured failure limit stopped collection.
- Excluding the three known dependency/contract groups: **444 passed, 36 skipped**.
- Failing groups:
  - Cartesia tests: undeclared `cartesia` dependency.
  - SQLite RAG tests: undeclared `sqlite_vec` dependency; local embedding dependencies are also absent from runtime requirements.
  - Speech sanitizer tests: implementation leaves numeric digits where the contract expects speakable words.
- Warnings include repeated `datetime.utcnow()` deprecations and unclosed SQLite resource warnings in compliance tests.

### Positive findings

- No committed API keys, private keys, or obvious active credentials were found in the snapshot.
- The committed SQLite database and fake calendar were empty.
- The code is divided into sensible domains: providers, core agent, integrations, compliance, RAG, schemas, and observability.
- The repository has broad unit-test coverage for many local components.
- The code compiles successfully.

These positives do not offset the release blockers below.

---

# Immediate release blockers

| Priority | Blocker | Why it blocks production |
|---|---|---|
| P0 | Add authentication, authorization, tenant scoping, and rate limits | Current routes expose PII and paid/real-world actions to any caller. |
| P0 | Disable outbound dialing until consent/DNC and caller-ID controls are server-owned | The caller can initiate real calls while consent is disabled by default. |
| P0 | Validate Vapi, Twilio, WhatsApp, and Telegram webhook authenticity | Spoofed/replayed events can trigger turns, bookings, writes, and dispositions. |
| P0 | Introduce durable, idempotent workflow state | Process memory and non-idempotent retries can duplicate or lose actions. |
| P0 | Make booking/outbound failures fail closed and retry durably | Current code acknowledges or swallows failures and loses recovery context. |
| P0 | Fix default configuration/dependencies and add readiness checks | The documented/default install cannot run all enabled features reliably. |
| P1 | Add data retention, deletion, encryption, and accurate PII handling | Raw caller data is persisted despite the redaction claims. |
| P1 | Remove blocking work from the async event loop | Model inference, SQLite, Google APIs, and subprocess work can stall all calls. |
| P1 | Fix RAG tenant filtering and confidence calculation | Weak or cross-tenant-crowded retrieval can be treated as high-confidence truth. |
| P1 | Add transactional booking and outbound attempt reservation | Check-then-write races can double-book and redial. |

---

# Detailed findings

## 1. Authentication, authorization, and abuse prevention

### SEC-001 — Critical — No application authentication
All operational routers are included without an authentication dependency in `apps/api/app/main.py:45-53`. The chat, voice, session, outbound, debug, compatibility, and webhook surfaces are therefore open unless an external reverse proxy happens to protect them.

**Impact:** Unauthorized use, data disclosure, billing abuse, and real-world side effects.  
**Fix:** Require authenticated principals globally, explicitly mark the few public webhook/health routes, and implement role/tenant authorization per resource.

### SEC-002 — Critical — No tenant isolation
Session IDs, booking IDs, Sheet IDs, business IDs, provider IDs, and debug data are accessed directly without checking that the caller owns the resource.

**Impact:** Horizontal data access across customers and businesses.  
**Fix:** Add a tenant ID to every durable model and every lookup; derive it from the authenticated principal, never from client-controlled payload alone.

### SEC-003 — Critical — Public session transcript and booking disclosure
`apps/api/app/routes/sessions.py` lists recent sessions and returns transcripts, extracted fields, tool arguments/results, caller names, phone numbers, notes, and booking records without authorization.

**Impact:** Direct PII and operational-data leak.  
**Fix:** Remove public listing, enforce tenant/resource authorization, minimize returned fields, and add audit logs for access.

### SEC-004 — Critical — Public paid provider endpoints
`/voice/stt`, `/voice/tts`, `/voice/tts-base64`, `/voice/tts-stream`, and chat endpoints invoke paid providers without authentication, quota, or rate limiting.

**Impact:** Credential-funded denial of wallet and resource exhaustion.  
**Fix:** Authenticate, apply per-tenant quotas, request-size limits, concurrency limits, and provider budget ceilings.

### SEC-005 — Critical — Public real outbound dialer
`/outbound/start_batch` accepts client-selected Sheet, assistant, phone-number, policy, and call-count inputs and can dispatch actual calls.

**Impact:** Toll fraud, harassment, regulatory exposure, and direct cost.  
**Fix:** Disable by default; require a privileged role, server-owned campaign configuration, approved caller IDs, immutable consent policy, and durable campaign audit records.

### SEC-006 — Critical — Google Sheets confused-deputy read
`/outbound/dry_run` accepts an arbitrary `sheet_id`; the server uses its configured service account and returns lead information from that sheet if the account can access it.

**Impact:** A caller can use the application as a confused deputy to read data the service account can access.  
**Fix:** Store an allowlisted sheet per tenant/campaign; never accept unrestricted resource IDs from untrusted callers.

### SEC-007 — High — No rate limiting
No route-level or global request throttling exists.

**Impact:** Brute force, provider cost abuse, webhook floods, memory pressure, and denial of service.  
**Fix:** Add edge and application rate limits keyed by tenant, IP, route, provider, and active call.

### SEC-008 — High — No payload or upload limits
`apps/api/app/routes/voice.py:24` reads an entire uploaded audio file into memory. Text, JSON, base64 media, WebSocket frames, and external media downloads are also unbounded.

**Impact:** Memory exhaustion, oversized provider charges, and event-loop starvation.  
**Fix:** Enforce content length, streaming reads, decoded-audio duration, text/token limits, JSON depth, frame limits, and media download caps.

### SEC-009 — High — Wildcard CORS
The default CORS policy permits `*` origins, methods, and headers (`apps/api/app/main.py:36-43`).

**Impact:** Any website can invoke the open API from a victim browser; this amplifies the lack of authentication.  
**Fix:** Use an explicit environment-specific allowlist and deny credentials/headers/methods not required.

### SEC-010 — High — Public debug endpoints
Trace listing, summaries, configuration, and trace clearing are exposed through `apps/api/app/routes/debug.py` without access control.

**Impact:** Internal errors, model/provider metadata, business/profile details, tool behavior, and operational timing can leak; trace clearing destroys evidence.  
**Fix:** Disable debug routes in production or require an administrator role and network restriction.

### SEC-011 — High — Public compatibility TTS API by default
The ElevenLabs-compatible endpoint only checks a key when one is configured; the default is effectively open.

**Impact:** Paid TTS abuse and unbounded synthesis.  
**Fix:** Require a key in non-development environments and apply quota/size limits.

### SEC-012 — High — No security headers or CSP
The FastAPI app does not set CSP, HSTS, frame restrictions, nosniff, referrer policy, or related headers.

**Impact:** Greater XSS/clickjacking/supply-chain blast radius.  
**Fix:** Add an edge or middleware security-header policy, tuned separately for embeddable widget paths.

### SEC-013 — High — DOM XSS in graph session selector
`apps/graph/app.js` inserts session IDs into `innerHTML` without escaping. Session IDs can originate from externally supplied provider metadata.

**Impact:** A crafted session ID can execute script in an operator's graph UI.  
**Fix:** Build `<option>` nodes with `textContent` and validate session-ID syntax server-side.

### SEC-014 — Medium — Floating third-party CDN scripts without integrity
`apps/graph/index.html` loads React/ReactDOM/ReactFlow from `unpkg` using floating versions and no Subresource Integrity.

**Impact:** Non-reproducible frontend behavior and supply-chain compromise exposure.  
**Fix:** Bundle pinned dependencies locally or pin exact immutable versions with integrity hashes and CSP.

### SEC-015 — Medium — Raw internal errors returned to clients
Provider, tool, and streaming TTS exceptions are often returned verbatim; `/voice/tts-stream` emits `str(e)` at `apps/api/app/routes/voice.py:178-180`.

**Impact:** Information disclosure and unstable public API contracts.  
**Fix:** Return stable error codes; retain sanitized details only in protected logs/traces.

### SEC-016 — Medium — No global exception boundary
There is no application-level exception middleware producing request IDs, safe responses, and structured logging.

**Impact:** Inconsistent 500 responses, leaked internals, and poor incident correlation.  
**Fix:** Add centralized exception handling and correlation IDs.

### SEC-017 — Medium — Voice IDs and generic payload dictionaries are weakly validated
Several request bodies are untyped `dict` objects and path fields are not tightly bounded.

**Impact:** Unexpected provider inputs, crashes, oversized values, and contract drift.  
**Fix:** Use strict Pydantic request models with enums, lengths, numeric bounds, and forbidden extra fields where appropriate.

---

## 2. Webhook and transport authenticity

### WH-001 — Critical — Twilio signature is not verified
The Twilio voice webhook does not verify `X-Twilio-Signature`.

**Impact:** Anyone can request generated TwiML and manipulate call setup.  
**Fix:** Verify against the exact external URL/body using the Twilio auth token and reject failures.

### WH-002 — Critical — Twilio WebSocket accepts arbitrary clients
`/twilio/stream` accepts the WebSocket without an authenticated, short-lived stream token or verified call binding.

**Impact:** Attackers can create fake streams, consume STT/LLM/TTS, inject audio, and interfere with sessions.  
**Fix:** Put an HMAC-signed, expiring call token in the stream URL and bind it to the expected Call SID/tenant.

### WH-003 — Critical — WhatsApp webhook signature is not verified
The WhatsApp route does not validate `X-Hub-Signature-256`.

**Impact:** Spoofed messages can trigger LLM and tool actions.  
**Fix:** Verify the raw body with the Meta app secret before parsing.

### WH-004 — Critical — Telegram secret token is not verified
The Telegram route does not verify `X-Telegram-Bot-Api-Secret-Token`.

**Impact:** Spoofed updates can create conversations and side effects.  
**Fix:** Configure and strictly verify the webhook secret token.

### WH-005 — High — Vapi authentication is optional and weakly compared
Vapi requests are accepted when no secret is configured. When configured, the code uses suffix matching rather than strict bearer parsing and constant-time comparison.

**Impact:** Spoofing and accidental secret acceptance.  
**Fix:** Require a secret in production, parse the exact scheme/value, use constant-time comparison, and support signed event verification if offered.

### WH-006 — High — No webhook replay/idempotency protection
Vapi, Twilio, WhatsApp, and Telegram messages do not persist provider event IDs with a uniqueness constraint.

**Impact:** Provider retries or malicious replays can repeat LLM calls, bookings, sends, and write-backs.  
**Fix:** Persist event IDs before processing and return the prior result for duplicates.

### WH-007 — High — Vapi retries can duplicate booking actions
The Vapi handler processes the last user message again on each retry, with no message/turn idempotency key.

**Impact:** Duplicate bookings and duplicate sink writes.  
**Fix:** Bind each provider message/turn ID to a durable processed-turn record and make tool side effects idempotent.

### WH-008 — High — Vapi disposition failures are acknowledged as success
The event route catches disposition errors and still returns success, suppressing provider retries.

**Impact:** Lost CRM/Sheet outcomes with no recovery.  
**Fix:** Durably enqueue first, then acknowledge; return retryable failure when enqueue persistence fails.

### WH-009 — Medium — Webhook schemas are too loose or too rigid in the wrong places
Some routes accept arbitrary nested dictionaries, while Vapi event modeling assumes a specific `message` shape and can fail when the provider evolves.

**Impact:** Silent misprocessing or avoidable 422 failures.  
**Fix:** Version schemas, tolerate documented provider variants, reject unknown critical fields, and log schema drift.

### WH-010 — Medium — No external URL canonicalization
TwiML construction and signature-sensitive webhook operation depend on externally visible URLs, but trusted proxy/host handling is not clearly configured.

**Impact:** Broken signatures, wrong callback URLs, and host-header ambiguity.  
**Fix:** Configure one canonical public base URL and trusted proxy headers; do not infer security-sensitive URLs from arbitrary request headers.

---

## 3. Privacy, PII, and data governance

### PRIV-001 — High — The redaction claim does not match persistence behavior
`_persist_session` stores `state.extracted.model_dump()` and booking fields containing raw names, phone numbers, and notes. Only selected transcript/tool payload paths are redacted.

**Impact:** Raw PII remains in SQLite despite comments suggesting otherwise.  
**Fix:** Document exactly which fields are retained, tokenize/encrypt necessary contact data, and redact or omit all nonessential fields.

### PRIV-002 — High — PII redaction defaults can be disabled
`NoopPIIRedactor` exists and configuration permits redaction to be off.

**Impact:** A production misconfiguration silently stores raw transcript PII.  
**Fix:** Make strong redaction mandatory in production and fail startup if disabled.

### PRIV-003 — High — Regex redaction is materially incomplete
The regex layer does not detect spoken-number forms, most names, addresses, non-NANP phones, varied international formats, or many date patterns.

**Impact:** Users can believe data is protected when common PII remains.  
**Fix:** Treat regex as a baseline only; use data minimization plus tested NER/tokenization and region-specific rules.

### PRIV-004 — Medium — Nested list values are not redacted
`PIIRedactor.redact_dict` recurses into dictionaries but not lists (`packages/compliance/pii.py:49-70`).

**Impact:** PII inside tool arrays or nested list objects bypasses redaction.  
**Fix:** Traverse arbitrary JSON structures, including lists/tuples, with cycle/depth limits.

### PRIV-005 — High — Raw transcript snippets are logged
Vapi/Twilio logging includes caller text/transcript content without a guaranteed redaction layer.

**Impact:** PII can enter console logs, platform log retention, and third-party collectors.  
**Fix:** Never log transcript bodies by default; log redacted lengths, IDs, and classified metadata.

### PRIV-006 — High — Local Whisper writes raw audio to predictable temporary files
The local STT provider writes each input to `/tmp/stt_debug_<seconds>.bin` and does not clean it up.

**Impact:** Raw caller audio remains on disk, filenames collide, and storage grows.  
**Fix:** Remove debug dumping; when explicitly enabled, use secure temporary files, restrictive permissions, unique names, and guaranteed deletion.

### PRIV-007 — High — Local voice orchestration persists audio/transcript artifacts without policy
Local outbound orchestration writes recordings/transcripts to disk without explicit retention, access-control, or cleanup controls.

**Impact:** Long-lived sensitive call data on local/shared storage.  
**Fix:** Encrypt or avoid persistence, set retention schedules, isolate tenants, and provide deletion tooling.

### PRIV-008 — High — No data retention or deletion workflow
There is no supported way to expire/delete calls, transcripts, bookings, consent records, recordings, or derived data by tenant or person.

**Impact:** Indefinite data accumulation and inability to honor operational/privacy requirements.  
**Fix:** Define retention classes, scheduled deletion, legal holds, and authenticated deletion/export endpoints.

### PRIV-009 — High — No encryption-at-rest application strategy
SQLite, fake calendar JSON, consent data, audio, and transcript artifacts are stored in plaintext.

**Impact:** Host, backup, or volume compromise reveals customer data.  
**Fix:** Use managed encrypted storage plus field-level encryption/tokenization for contact data and protected key management.

### PRIV-010 — Medium — Entire conversations are sent to third-party providers
The brain repeatedly submits the full transcript, including PII and tool results, without per-provider minimization.

**Impact:** Excess data exposure and provider retention risk.  
**Fix:** Minimize context, redact/tokenize before provider calls where possible, and define provider-specific data-processing settings.

### PRIV-011 — Medium — Raw provider responses may be retained
`LLMResponse.raw` can carry full provider payloads and sensitive content.

**Impact:** Accidental logging, memory retention, and oversized traces.  
**Fix:** Do not retain raw payloads in normal execution; gate sanitized diagnostics behind protected development flags.

### PRIV-012 — Medium — Consent records lack evidence depth
Consent storage does not clearly retain purpose, disclosure/version, source evidence, campaign scope, terms version, or revocation lineage.

**Impact:** The system may be unable to demonstrate why a call was considered permitted.  
**Fix:** Store immutable consent evidence and policy version, not only a phone/status decision.

---

## 4. Session state, concurrency, and persistence

### STATE-001 — Critical — Active sessions exist only in process memory
`_states` and `_brains` in `apps/api/app/core/session_manager.py` are process-local dictionaries.

**Impact:** Restarts lose calls; multiple workers see different state; webhook requests can recreate divergent sessions.  
**Fix:** Move active state to a durable shared store or a single-owner actor/queue model.

### STATE-002 — High — Abandoned sessions have no TTL
In-memory state is retained until explicit end, which many transport failures will not call.

**Impact:** Memory leak and stale state reuse.  
**Fix:** Add heartbeat/idle TTL, call-duration caps, cleanup jobs, and terminal-state persistence.

### STATE-003 — Critical — No per-session synchronization
The global lock protects dictionary access, not the full turn. Concurrent chat/Vapi/Twilio tasks can mutate one transcript and invoke tools simultaneously.

**Impact:** Out-of-order transcript entries, duplicated bookings, stale extraction, and inconsistent responses.  
**Fix:** Serialize turns per session with a distributed lock/actor and include monotonic turn numbers.

### STATE-004 — High — Starting a duplicate session ID can overwrite active state
`start_session_with_id` replaces entries without a collision/error policy.

**Impact:** Session hijacking or accidental state destruction.  
**Fix:** Make creation conditional, validate owner, and return conflict for active IDs.

### STATE-005 — High — Transcript persistence uses a race-prone count/slice algorithm
The code counts existing DB messages and writes `state.transcript[existing:]`.

**Impact:** Concurrent writes can duplicate, omit, or reorder messages.  
**Fix:** Assign immutable message IDs and sequence numbers; insert with uniqueness constraints in one transaction.

### STATE-006 — High — End timestamps are not consistently set
The state end path does not reliably set `state.ended_at`, so persisted sessions may remain apparently active.

**Impact:** Incorrect analytics, cleanup, billing, and lifecycle state.  
**Fix:** Use an explicit state transition transaction and set terminal reason/time once.

### STATE-007 — Medium — Ending a missing session reports success
`/chat/end` returns an ended result without distinguishing missing/already-ended sessions.

**Impact:** Hides client bugs and failed lifecycle tracking.  
**Fix:** Return an idempotent but truthful status (`already_ended`, `not_found`, `ended`).

### STATE-008 — High — Outbound context is also process-local
`outbound_registry.py` keeps call-to-sheet context in memory.

**Impact:** Restart or worker routing loses disposition write-back context.  
**Fix:** Persist call context keyed by provider call ID before dispatch.

### STATE-009 — High — Registry entries lack TTL and lifecycle audit
Orphaned context remains in memory, while popped context disappears permanently.

**Impact:** Memory growth and no forensic recovery.  
**Fix:** Durable status model with expiration, retry counts, and immutable event log.

### STATE-010 — High — Context is removed before durable completion
Disposition handling pops registry context before LLM classification and Sheet update.

**Impact:** Any downstream failure makes retry impossible.  
**Fix:** Mark processing with a lease; delete/complete only after durable success.

### STATE-011 — High — Sink failures are swallowed
Session completion catches sink/export errors without retry, failure state, or dead-letter storage.

**Impact:** CRM/Sheet records silently disappear while the API reports success.  
**Fix:** Use an outbox table and retry worker; expose failed delivery state.

### STATE-012 — High — Extraction failures are swallowed
Post-turn extraction failures leave stale fields and are not visible to callers/operators.

**Impact:** Incorrect routing and booking fields can persist unnoticed.  
**Fix:** Track extraction status/version/error and require confirmed fields for irreversible actions.

### STATE-013 — Medium — Lazy singletons are not consistently synchronized
Business, calendar, RAG, sink, and redactor instances are lazily initialized without a consistent concurrency strategy.

**Impact:** Duplicate expensive initialization and inconsistent object state.  
**Fix:** Initialize through application lifespan or use thread-safe single-flight initialization.

### STATE-014 — High — RAG initialization failures retry repeatedly
A failed initialization is swallowed while the cached object remains unset.

**Impact:** Every new session may repeat expensive failing initialization and silently run without knowledge.  
**Fix:** Persist a failed readiness state and alert; retry under controlled backoff.

### STATE-015 — High — Synchronous persistence is used in an async application
The application uses a synchronous SQLAlchemy engine/session from async routes and turn handling.

**Impact:** Database operations block the event loop and reduce concurrent call capacity.  
**Fix:** Use an async driver/session or offload bounded synchronous work to a dedicated executor.

### STATE-016 — Medium — SQLite is not configured for production concurrency
There is no explicit WAL mode, busy timeout, foreign-key pragma, or write-serialization strategy.

**Impact:** Lock errors, weak constraint enforcement, and unpredictable concurrent writes.  
**Fix:** Use PostgreSQL for production; if SQLite remains for local use, configure pragmas and strict single-writer behavior.

### STATE-017 — High — No schema migrations
`create_all` is called at app creation instead of applying versioned migrations.

**Impact:** Existing deployments cannot safely evolve or roll back schemas.  
**Fix:** Add Alembic migrations and a controlled deployment migration step.

### STATE-018 — Medium — Database initialization occurs during app construction/import
`create_app()` calls `init_db()`, and module import creates the app.

**Impact:** Import side effects, test interference, read-only filesystem failures, and surprising worker startup behavior.  
**Fix:** Move initialization to application lifespan and fail readiness clearly.

### STATE-019 — Medium — SQLite URL/path handling is brittle
The path is derived with string replacement rather than robust URL parsing.

**Impact:** Incorrect paths for nontrivial SQLite URLs/platforms.  
**Fix:** Use SQLAlchemy URL parsing and explicit local path configuration.

### STATE-020 — Medium — Naive UTC timestamps are pervasive
`datetime.utcnow()` is deprecated and creates timezone-naive values.

**Impact:** Ambiguous comparisons, serialization errors, and timezone bugs.  
**Fix:** Use timezone-aware UTC timestamps and convert only at presentation/business-boundary layers.

### STATE-021 — High — No transaction spans external booking and local records
Calendar creation, booking-row insertion, and sink writes are separate operations.

**Impact:** Partial success: calendar booked but DB missing, DB saved but CRM missing, or retries duplicate the event.  
**Fix:** Use an idempotent booking command, external idempotency key where available, local outbox, and compensating status.

### STATE-022 — High — Booking event IDs are not protected against duplicate inserts
Provider event/booking IDs can collide, and duplicate/integrity errors are not consistently converted into idempotent success.

**Impact:** 500 responses after a successful external booking and possible retry duplication.  
**Fix:** Add uniqueness constraints and deterministic command IDs; return existing outcome for duplicates.

### STATE-023 — Medium — Booking duration is hardcoded during persistence
The persisted booking row uses a fixed 30-minute duration rather than the actual service/event duration.

**Impact:** Incorrect records and downstream scheduling analytics.  
**Fix:** Persist the authoritative end time/duration returned by the calendar service.

### STATE-024 — Medium — Foreign-key/cascade behavior is incomplete
SQLite foreign-key enforcement is not explicitly enabled, and model relationships/cascades are limited.

**Impact:** Orphaned rows and unreliable cleanup.  
**Fix:** Enable constraints, add indexes/relationships, and test deletion behavior.

---

## 5. Calendar and booking correctness

### BOOK-001 — Critical — Booking validation fails open on LLM failure
The write guard explicitly permits a booking when its LLM validation call fails.

**Impact:** Invalid, hallucinated, or maliciously supplied booking data can become a real write during provider outages.  
**Fix:** Fail closed for irreversible operations; ask the caller to retry or transfer.

### BOOK-002 — High — Tool JSON schemas are advisory, not enforced at runtime
LLM-generated tool arguments are passed to handlers without a common strict schema-validation boundary.

**Impact:** Missing, malformed, or extra fields reach external systems.  
**Fix:** Validate each call against typed Pydantic models before handler invocation.

### BOOK-003 — High — Availability check and event insertion are not atomic
Google Calendar is queried, then an event is inserted in a separate operation.

**Impact:** Two concurrent calls can both observe availability and double-book.  
**Fix:** Use provider-supported conflict controls where possible, a server-side slot reservation/lock, and a final conflict check.

### BOOK-004 — High — Google Calendar timezone handling is incorrect
Naive datetimes have `Z` appended, which declares UTC even when values represent business-local time.

**Impact:** Appointments can be created or checked at the wrong hour.  
**Fix:** Require timezone-aware datetimes, use the business IANA timezone, and send explicit event timezone fields.

### BOOK-005 — High — Slot listing performs one remote API call per candidate slot
`list_slots` repeatedly calls availability for individual slots.

**Impact:** High latency, quota use, and event-loop blocking.  
**Fix:** Fetch busy intervals once for the requested window and compute all slots locally.

### BOOK-006 — High — Fake calendar treats malformed JSON as an empty calendar
Corruption/read failure falls back to no events.

**Impact:** Existing bookings are ignored and can be overwritten/double-booked.  
**Fix:** Fail closed on corruption, preserve a backup, validate schema, and alert.

### BOOK-007 — High — Fake calendar writes are non-atomic
The calendar JSON file is overwritten directly.

**Impact:** Crash or concurrent write can truncate/corrupt all bookings.  
**Fix:** Write to a temporary file, fsync, and atomic rename; use a real database for anything beyond demo use.

### BOOK-008 — High — Fake calendar lock is only process-local
Multiple workers/processes can concurrently read-modify-write the same file.

**Impact:** Lost updates and double bookings.  
**Fix:** Do not use it in multi-process production; use a transactional store.

### BOOK-009 — High — Business hours and booking horizon are not centrally enforced
Booking handlers do not consistently reject past times, closed hours, excessively distant dates, or unavailable services.

**Impact:** Invalid bookings and inconsistent behavior by vertical/provider.  
**Fix:** Apply one authoritative scheduling-policy validator before all writes.

### BOOK-010 — High — Unknown services can fall back to a default duration
Invalid service names may be accepted with a generic duration instead of rejected.

**Impact:** Hallucinated services get booked.  
**Fix:** Require a catalog service ID and reject unknown values.

### BOOK-011 — High — No explicit user confirmation token before write
The agent can transition from extracted values to booking without a durable confirmation record tied to exact details.

**Impact:** Misheard values or retries can create unwanted appointments.  
**Fix:** Generate a canonical booking summary/hash, obtain confirmation, and submit that immutable command once.

### BOOK-012 — Medium — Caller identity fields have weak domain validation
Name and phone checks are heuristic and not region-aware; phone normalization/verification is inconsistent.

**Impact:** Invalid contact records and failed notifications.  
**Fix:** Use E.164 normalization, tenant region settings, and explicit caller confirmation.

### BOOK-013 — Medium — Booking cancellation/rescheduling is not a complete workflow
Intent schemas include broader possibilities, but durable tools and authorization for cancel/reschedule are incomplete.

**Impact:** The agent can recognize needs it cannot safely complete, creating misleading UX.  
**Fix:** Either implement idempotent workflows or explicitly transfer/decline them.

---

## 6. Outbound dialing and consent controls

### OUT-001 — Critical — Consent provider is disabled by default
Configuration defaults to no consent provider and explicitly skips consent enforcement.

**Impact:** The code's intended consent gate is absent in the default real-dial path.  
**Fix:** Make consent enforcement mandatory for outbound production startup.

### OUT-002 — Critical — Production route does not call the consent-aware decision path
Outbound routes use the synchronous dial policy decision rather than the consent-integrated decision helper.

**Impact:** Even a configured consent provider may not protect the real route.  
**Fix:** Route every dispatch through one mandatory policy service that combines consent, DNC, quiet hours, attempts, and campaign authorization.

### OUT-003 — Critical — DNC input is client-controlled
The request supplies DNC values and defaults to an empty list.

**Impact:** A caller can omit protected numbers.  
**Fix:** DNC must come from a trusted tenant-owned store and cannot be weakened by request payload.

### OUT-004 — High — DNC numbers are not normalized consistently
Lead phones are normalized, but the policy DNC set is not normalized equivalently.

**Impact:** Formatting differences bypass DNC matching.  
**Fix:** Normalize both sides to one canonical E.164 representation at ingestion.

### OUT-005 — High — Phone validation is insufficient
The dialer largely checks for digits/nonempty values rather than valid country/length/routing.

**Impact:** Invalid or unintended numbers reach the provider.  
**Fix:** Use a phone-number library, required region/country, and explicit allow/deny policy.

### OUT-006 — High — Invalid timezones can raise despite a “never raises” contract
`ZoneInfo` failures are not fully converted into a safe policy rejection.

**Impact:** A malformed request can 500 the batch.  
**Fix:** Validate timezone at campaign creation and fail closed with a typed error.

### OUT-007 — High — Policy fields lack strict bounds
Hours, weekdays, cooldown, max attempts, and max calls accept unsafe or nonsensical values.

**Impact:** Runtime errors, disabled safeguards, negative counters, or huge batches.  
**Fix:** Use strict typed models with ranges and server-side maximums.

### OUT-008 — High — The wrong business profile can be selected
`_load_business_profile` can return the first configured profile whenever it exists, regardless of requested business ID.

**Impact:** Wrong greeting, vertical, policy, identity, or tools can be used for a campaign.  
**Fix:** Resolve by exact tenant/business ID and fail if absent; never silently fall back.

### OUT-009 — Critical — No atomic “reserve before dial” operation
The code evaluates a row and dispatches without first durably reserving the lead/attempt.

**Impact:** Concurrent batch requests can dial the same lead multiple times.  
**Fix:** Atomically claim rows/campaign leads with a unique attempt record before provider dispatch.

### OUT-010 — High — Attempt counts update only at end of call
Failed dispatches, lost callbacks, and hung calls may never update “last called” or total attempts.

**Impact:** Repeated calling because the cooldown/attempt policy sees stale data.  
**Fix:** Record an attempt before dispatch, then transition it through provider/connected/completed/failed states.

### OUT-011 — High — Batch dispatch is performed sequentially inside the HTTP request
The endpoint awaits each provider request while claiming to be non-blocking.

**Impact:** Request timeouts, poor throughput, and ambiguous partial completion.  
**Fix:** Persist a campaign job and return 202; process through a bounded worker queue.

### OUT-012 — High — No idempotency key or durable campaign model
Repeated client requests create new call attempts with no stable command identity.

**Impact:** Duplicate campaigns and hard-to-audit outcomes.  
**Fix:** Require an idempotency key and persist campaign/batch/lead attempt records.

### OUT-013 — High — No global or per-tenant outbound concurrency cap
Request `max_calls` limits selection, not necessarily active call concurrency across requests/workers.

**Impact:** Provider overload, account limits, cost spikes, and caller experience degradation.  
**Fix:** Enforce distributed active-call limits and provider quotas.

### OUT-014 — Critical — Caller can override assistant and caller-ID identifiers
`assistant_id` and `phone_number_id` are accepted from the request.

**Impact:** Unauthorized use of other assistants/numbers and policy bypass.  
**Fix:** Resolve these server-side from an authorized campaign configuration.

### OUT-015 — High — Outbound registry is populated after dispatch
A very fast provider callback can arrive before the call context is remembered.

**Impact:** End-of-call event becomes unrecognized and write-back is lost.  
**Fix:** Persist pending context before dispatch and attach provider call ID transactionally immediately after response.

### OUT-016 — High — Duplicate end-of-call webhook is not idempotent
After first processing pops context, a retry is classified as not outbound.

**Impact:** Provider retries produce inconsistent results and cannot confirm prior success.  
**Fix:** Persist completed disposition keyed by event/call ID and return it for duplicates.

### OUT-017 — High — LLM classification is a single point of failure for write-back
Disposition and extracted fields depend on one model call with no confidence/manual-review state.

**Impact:** Incorrect lead status/rent/appointment values are written as fact.  
**Fix:** Use deterministic provider fields first, schema-constrained extraction, confidence thresholds, and a review queue.

### OUT-018 — Medium — Rent parsing removes all non-digits
Decimal points, negatives, ranges, and unrelated digits are collapsed.

**Impact:** Materially incorrect monetary values.  
**Fix:** Parse a strict currency schema and reject ambiguous values.

### OUT-019 — High — Phone fallback can update the wrong Sheet row
When direct row context is unavailable, matching by phone may affect duplicate/reformatted records.

**Impact:** Incorrect person/lead record is changed.  
**Fix:** Persist immutable sheet row/campaign lead IDs; never infer write target after the call.

### OUT-020 — High — Sheet counters use read-modify-write
“Total Calls” is derived from stale row data and then written.

**Impact:** Concurrent completions lose increments.  
**Fix:** Keep authoritative counters in a transactional database; export snapshots to Sheets.

### OUT-021 — Medium — Dry-run accounting is incomplete
Rows beyond the selected limit are not necessarily represented as skipped, so totals do not describe the full source sheet.

**Impact:** Misleading preview and audit counts.  
**Fix:** Return source total, eligible total, selected total, and every skip category separately.

### OUT-022 — Medium — Consent HTTP provider contract contradicts implementation
Documentation says POST while implementation uses GET.

**Impact:** Integration mistakes and possible sensitive values in URLs/logs.  
**Fix:** Define one versioned API contract, use POST with authenticated body, and test it end to end.

### OUT-023 — Medium — Consent HTTP integration has no first-class auth configuration
The adapter accepts a URL but does not expose a robust typed auth/signing configuration.

**Impact:** Weak service-to-service security or credentials embedded in URLs.  
**Fix:** Support headers/mTLS/signing via secret references and redact request logs.

### OUT-024 — High — `AlwaysConsentProvider` lacks a production guard
A permissive provider can be selected without an environment-level prohibition.

**Impact:** Accidental disabling of the key safety gate.  
**Fix:** Make permissive consent available only under an explicit test/dev mode that cannot boot with real dialing.

### OUT-025 — High — Disclosure helpers do not guarantee actual outbound disclosure
A greeting override can bypass expected disclosure language, and route dispatch does not verify the final greeting.

**Impact:** Code-level policy intent is not enforced at the call boundary.  
**Fix:** Compose mandatory disclosure server-side outside editable prompt/greeting text and test the exact provider payload.

### OUT-026 — Medium — Spreadsheet is used as operational database
Eligibility, attempts, statuses, and write-backs rely on mutable Sheet cells.

**Impact:** No atomicity, weak auditability, races, manual edits, and schema drift.  
**Fix:** Use a database as source of truth and treat Sheets as import/export or operator UI.

---

## 7. Agent, extraction, and tool orchestration

### AGENT-001 — High — Full transcript is resent on every turn
Conversation context grows without summarization, token budget, or truncation.

**Impact:** Increasing cost/latency, provider context failures, and privacy exposure.  
**Fix:** Maintain a bounded structured state plus recent-turn window and verified summary.

### AGENT-002 — High — Excessive model calls per user turn
A turn may invoke multiple tool-loop completions, a forced final completion, extraction, and write guard.

**Impact:** High latency/cost and more failure points during a real-time call.  
**Fix:** Collapse deterministic extraction/state updates, cap wall-clock/provider budget, and benchmark the full turn path.

### AGENT-003 — High — No end-to-end turn deadline/cancellation budget
Individual adapters have inconsistent timeouts, while the brain can continue through several calls.

**Impact:** Long silence and tasks continuing after disconnect/barge-in.  
**Fix:** Propagate a call/turn cancellation token and one wall-clock deadline through every provider/tool.

### AGENT-004 — High — Tool-handler exceptions can escape the brain loop
Not every handler invocation is wrapped in a controlled error boundary.

**Impact:** One integration exception can terminate a call after partial transcript/tool state.  
**Fix:** Validate, execute under timeout, map exceptions to typed retry/transfer behavior, and persist side-effect status.

### AGENT-005 — High — LLM failure is disguised as caller misunderstanding
Provider failure can produce “say that again” behavior rather than an outage-specific response.

**Impact:** Caller repeats themselves while the service remains unavailable; diagnostics are obscured.  
**Fix:** Distinguish no-speech, low-confidence STT, model timeout, policy rejection, and service outage.

### AGENT-006 — High — Extraction runs even when it should not
Extraction is invoked after greetings/fallback/safety paths where no new reliable caller facts may exist.

**Impact:** Added latency/cost and hallucinated/stale field changes.  
**Fix:** Run extraction only on eligible user turns and only update fields with evidence.

### AGENT-007 — High — Malformed extraction can erase valid prior state
The extractor returns blank/default fields on parse failure and does not safely merge confirmed fields.

**Impact:** Previously confirmed caller information disappears or regresses.  
**Fix:** Use patch semantics with field-level provenance/confidence; never replace confirmed data with missing output.

### AGENT-008 — High — Extraction includes assistant text
The extractor can treat assistant-generated content as evidence.

**Impact:** Model hallucinations can become structured caller facts.  
**Fix:** Extract from caller utterances and verified tool results only, with message-role provenance.

### AGENT-009 — Medium — Code-fence cleanup is unsafe
`text.strip("`").lstrip("json")` strips character sets rather than exact delimiters.

**Impact:** Valid JSON can be corrupted in edge cases.  
**Fix:** Parse fenced blocks explicitly or use provider-native structured output.

### AGENT-010 — Medium — Lead score conversion is not robustly validated
A direct integer conversion can raise; the schema does not consistently constrain 0–100.

**Impact:** One malformed model value breaks extraction.  
**Fix:** Use strict constrained types and graceful field-level validation.

### AGENT-011 — High — Prompt-injection resistance is heuristic
Regex input guards and prompt instructions are not reliable authorization boundaries.

**Impact:** A caller or retrieved document can influence tool behavior beyond intended business rules.  
**Fix:** Enforce permissions, allowed transitions, and data validation outside the model.

### AGENT-012 — High — RAG content is inserted as trusted prompt material
Retrieved text is directly embedded in the answering prompt.

**Impact:** Malicious/accidental instructions in business documents can redirect behavior.  
**Fix:** Treat retrieval as quoted data, strip active instructions, enforce a strict answer schema, and test injection corpora.

### AGENT-013 — High — Escalation does not necessarily stop the tool loop
Escalation state can be set while processing continues.

**Impact:** The agent may perform actions after deciding a human is required.  
**Fix:** Make escalation a terminal turn transition and cancel remaining tool/model work.

### AGENT-014 — Medium — Tool-loop exhaustion message does not set durable escalation
The assistant can promise a teammate callback without recording an escalation task/state.

**Impact:** Caller expectation is created with no operational follow-through.  
**Fix:** Require a real handoff/callback ticket before using that wording.

### AGENT-015 — Medium — Escalated/ended sessions can still accept turns
Lifecycle state is not consistently enforced at every entry point.

**Impact:** Additional actions after terminal state.  
**Fix:** Reject or route terminal sessions through a defined post-call path.

### AGENT-016 — Medium — Tool result serialization omits the tool name
Serialized tool history can lack function name and may use empty call IDs.

**Impact:** Provider adapters cannot reconstruct valid multi-turn tool histories.  
**Fix:** Define one canonical provider-neutral message schema with required tool name/call ID.

### AGENT-017 — Medium — Mutable list defaults are used in Pydantic models
`tool_calls=[]` and similar patterns are fragile even if current Pydantic versions copy defaults.

**Impact:** Version-sensitive shared state risk and poor model hygiene.  
**Fix:** Use `Field(default_factory=list)`.

### AGENT-018 — Medium — Tool errors are fed back as raw strings
Integration exception text can become model context.

**Impact:** Internal details leak into responses and prompt context; model behavior becomes unstable.  
**Fix:** Return typed, sanitized tool error codes with a safe caller message.

### AGENT-019 — Medium — No field provenance/version model
Extracted name, phone, intent, service, and time do not carry source turn, confidence, confirmation, or revision history.

**Impact:** The system cannot distinguish guessed, extracted, user-confirmed, and tool-verified data.  
**Fix:** Store field state as value + source + confidence + confirmed-at + version.

### AGENT-020 — Medium — Prompt/business configuration is not versioned per call
Changes to profile/prompt/tool config are not durably tied to the session.

**Impact:** Post-incident reconstruction cannot determine exactly what rules the call used.  
**Fix:** Snapshot immutable configuration/version IDs at session creation.

---

## 8. LLM/STT/TTS provider adapters

### PROV-001 — High — Default configuration is not operationally complete
Defaults select cloud LLM/STT providers without keys and enable local SQLite RAG without its dependencies; browser TTS is incompatible with phone audio.

**Impact:** Health reports success while normal call paths fail or produce silence.  
**Fix:** Validate a coherent provider bundle at startup and provide explicit dev/browser/phone profiles.

### PROV-002 — High — Health endpoint is liveness only
`/health` returns `ok: true` and configured provider names without testing DB, credentials, dependencies, storage, or provider readiness.

**Impact:** Orchestrators route calls to a broken instance.  
**Fix:** Separate liveness and readiness; readiness should verify required dependencies/config and bounded provider checks.

### PROV-003 — High — Gemini adapter loses assistant tool-call history
Assistant tool calls are reduced to text, function response naming is not reliably linked, and generated IDs are synthetic.

**Impact:** Multi-turn tool use can fail or diverge with native Gemini.  
**Fix:** Preserve provider-native function-call/function-response parts and stable IDs in both directions.

### PROV-004 — High — Several adapters assume tool arguments are valid JSON
OpenAI-compatible adapters parse function arguments without a consistent malformed-JSON fallback.

**Impact:** A single malformed model response crashes the turn.  
**Fix:** Parse defensively, validate against tool schema, and request a repair/retry under a strict budget.

### PROV-005 — Medium — New HTTP client is created per provider call
Several adapters instantiate `httpx.AsyncClient` inside each request.

**Impact:** Lost connection pooling, higher latency, socket churn, and harder shutdown.  
**Fix:** Create lifespan-managed shared clients with connection limits and close them on shutdown.

### PROV-006 — High — Retry/fallback policy is duplicated and inconsistent
The router implements fallback/cooldowns while Groq has another long fallback ladder.

**Impact:** Nested retries, unexpected provider switching, excessive latency, and hard-to-reason costs.  
**Fix:** Centralize retry/fallback policy in one layer and make adapters single-attempt primitives.

### PROV-007 — High — Groq fallback can sleep for minutes
Standalone fallback behavior can pause up to hundreds of seconds.

**Impact:** Unacceptable dead air and worker occupancy during calls.  
**Fix:** Use sub-second/low-second real-time budgets and transfer/fail gracefully rather than long sleeps.

### PROV-008 — Medium — Router configuration reads unvalidated raw environment values
Float/time values can fail at startup or behave unexpectedly.

**Impact:** Configuration errors outside the typed settings system.  
**Fix:** Put every value in validated `Settings` models with bounds.

### PROV-009 — Medium — Router cooldown state is process-local and unsynchronized
Workers maintain different provider health states.

**Impact:** Continued traffic to failing providers or uneven failover.  
**Fix:** Use a shared circuit-breaker state or accept per-instance state with explicit load-balancer behavior.

### PROV-010 — Medium — All errors can poison provider health
Bad requests caused by malformed caller/history may be treated as provider-wide failure.

**Impact:** Healthy providers are unnecessarily cooled down.  
**Fix:** Classify retryable transport/server/rate errors separately from deterministic request errors.

### PROV-011 — Medium — Cancellation may not stop underlying provider work
`asyncio.wait_for` alone does not guarantee an external SDK/request has stopped and released resources.

**Impact:** Cost and work continue after timeout/disconnect.  
**Fix:** Use cancellable transports, close streams, and track abandoned requests.

### PROV-012 — Medium — Gemini key is placed in the URL query
The API key is embedded in the request URL.

**Impact:** URLs are more likely to appear in proxy/access logs.  
**Fix:** Use an authorization header where the API supports it and redact URLs in logs.

### PROV-013 — High — Provider singletons/resources are never closed
Cached clients/models can define close methods, but the app has no comprehensive shutdown cleanup.

**Impact:** Leaked sockets/tasks/resources and unclean deploys.  
**Fix:** Manage every provider through FastAPI lifespan and close deterministically.

### PROV-014 — High — Twilio audio supports only a narrow TTS output path
The transport expects WAV/convertible audio, while configured providers may return MP3, raw PCM, or browser-only sentinel output.

**Impact:** Phone calls can be silent or fail despite TTS working in the browser.  
**Fix:** Define one telephony audio contract (e.g., 8 kHz μ-law) and transcode/test every supported provider.

### PROV-015 — High — Python 3.13 audio compatibility dependency is absent
The code uses `audioop`, removed from Python 3.13, without declaring the compatibility package.

**Impact:** Clean Python 3.13 deployments can fail.  
**Fix:** Pin supported Python versions or add/test `audioop-lts`/replace the conversion code.

### PROV-016 — High — Local Whisper blocks the event loop
Model loading, inference, NumPy work, and `subprocess.run` occur in an async method.

**Impact:** One local transcription can stall every concurrent request.  
**Fix:** Run inference in a worker process/thread pool with bounded concurrency; preload during lifespan.

### PROV-017 — High — Local embedding inference blocks the event loop
Sentence-transformer loading/encoding is synchronous inside async code.

**Impact:** Retrieval stalls the server under local inference.  
**Fix:** Dedicated model worker/executor and startup preload.

### PROV-018 — High — Runtime model downloads are possible on first request
Local providers may download large models lazily.

**Impact:** First call times out, requires network unexpectedly, and produces non-reproducible deployments.  
**Fix:** Prepackage/prewarm models and fail readiness when unavailable.

### PROV-019 — Medium — “Streaming” compatibility endpoint buffers full synthesis
The ElevenLabs-compatible stream endpoint synthesizes all audio before yielding chunks.

**Impact:** Misleading latency semantics and no true time-to-first-audio benefit.  
**Fix:** Use native provider streaming or label endpoint as chunked download.

### PROV-020 — Medium — Compatibility request fields are ignored
Model, voice settings, output format, and other expected fields are not fully honored.

**Impact:** Clients believe they requested behavior that was silently discarded.  
**Fix:** Implement the contract or reject unsupported fields explicitly.

### PROV-021 — Medium — TTS streaming wire contract is inconsistent
Documentation says each chunk includes text; normal and cached audio chunks omit it, while browser chunks use a different shape.

**Impact:** Fragile clients and untestable compatibility assumptions.  
**Fix:** Define/version one NDJSON schema and validate every emitted event.

### PROV-022 — High — Browser TTS streaming repeats the full reply per sentence
The base provider splits text into sentence chunks, but the route emits `speak: text` (the entire original reply) for every browser sentinel chunk at `apps/api/app/routes/voice.py:157-163`.

**Impact:** Default browser TTS can speak the entire response multiple times.  
**Fix:** Preserve and emit the actual sentence text alongside each chunk, or special-case browser TTS as one event.

### PROV-023 — Medium — Streaming errors expose internals and return an in-band partial success
Clients receive prior chunks followed by raw error text with HTTP 200.

**Impact:** Ambiguous completion and leaked diagnostics.  
**Fix:** Use typed terminal error events with safe codes and client retry semantics.

### PROV-024 — Medium — Compressed audio chunks may not be independently decodable
The browser assumes each emitted chunk can be passed separately to `decodeAudioData`; provider-native streams may split containers/frames arbitrarily.

**Impact:** Playback failures for MP3/streaming formats.  
**Fix:** Define chunk framing guaranteed to be independently decodable or use MediaSource/WebCodecs/native stream handling.

### PROV-025 — Medium — Speech sanitizer implementation violates its own tests
Currency, percentages, phone-like numbers, and numeric values remain digits rather than normalized speech.

**Impact:** TTS pronunciation is inconsistent and test suite is red.  
**Fix:** Choose/document a normalization contract and implement locale-aware number-to-words conversion.

---

## 9. Twilio, Vapi, and messaging channel behavior

### CHAN-001 — High — Twilio creates untracked background tasks per utterance
`asyncio.create_task` is used without lifecycle tracking or serialization.

**Impact:** Overlapping replies race, tasks survive disconnect, and exceptions may be lost.  
**Fix:** Use a per-call task group/queue; cancel and await it at disconnect.

### CHAN-002 — High — Twilio utterance processing races shared flags and state
Multiple tasks can mutate `speaking_reply`, transcript, and tool state concurrently.

**Impact:** Crossed audio, duplicate turns, and corrupt state.  
**Fix:** Single ordered turn processor per call with explicit barge-in cancellation.

### CHAN-003 — High — Very short caller utterances can be dropped
The implementation threshold effectively requires about a second of μ-law audio despite a smaller documented constant.

**Impact:** Important responses such as “yes,” “no,” or names are ignored.  
**Fix:** Use VAD/STT endpointing confidence rather than a hard byte threshold; test short utterances.

### CHAN-004 — High — Barge-in transcription can block frame receipt
Periodic STT work occurs in the WebSocket receive flow.

**Impact:** Incoming audio frames backlog/drop while transcription runs.  
**Fix:** Decouple receive buffering from STT workers and bound queue latency.

### CHAN-005 — High — No maximum call duration or idle timeout
A WebSocket can remain connected indefinitely.

**Impact:** Resource/cost exhaustion and orphaned sessions.  
**Fix:** Enforce idle, absolute duration, and no-progress timeouts.

### CHAN-006 — Medium — Malformed WebSocket events can terminate the call abruptly
Direct dictionary indexing/base64 assumptions lack a robust protocol validator.

**Impact:** Minor provider variation or bad frame drops the call.  
**Fix:** Validate event type/schema and safely ignore/reject malformed frames.

### CHAN-007 — Medium — TTS failures are frequently swallowed as silence
The call may continue without telling the caller or recording a terminal error.

**Impact:** Apparent dead air and no reliable recovery.  
**Fix:** Retry within a small budget, play a cached failure prompt, then transfer/end and alert.

### CHAN-008 — Medium — TwiML stream URL construction is string-based
URL transformation does not establish canonical trust or robust escaping.

**Impact:** Broken callback URLs and host/proxy edge cases.  
**Fix:** Build from a configured public base URL using URL libraries and XML generation.

### CHAN-009 — Medium — Recursive speak/barge handling can grow call stack/control complexity
Reply interruption paths recursively invoke further speech behavior.

**Impact:** Hard-to-reason cancellation and pathological recursion.  
**Fix:** Use an event-driven state machine rather than recursive calls.

### CHAN-010 — High — Vapi ignores most provider conversation history
The provider sends history, but the server largely processes only the latest user text while maintaining separate local memory.

**Impact:** Retries/restarts/workers produce state divergence from what Vapi believes happened.  
**Fix:** Use one authoritative transcript keyed by provider message IDs or reconcile histories deterministically.

### CHAN-011 — Medium — Vapi empty-message greeting is not consistently persisted
An empty request can return a greeting without creating the same durable session state.

**Impact:** Displayed/spoken history and stored history diverge.  
**Fix:** Create/idempotently persist the session/greeting on the first provider event.

### CHAN-012 — Medium — Vapi session IDs are not tightly validated
Provider metadata can become arbitrary/unbounded session identifiers.

**Impact:** Storage/log/UI injection and resource abuse.  
**Fix:** Validate a safe length/character set or generate internal IDs and map external IDs.

### CHAN-013 — Medium — Unsafe assumption that Vapi call ID is a string
String slicing/logging can crash on unexpected types.

**Impact:** Avoidable webhook failures on schema drift.  
**Fix:** Validate/coerce through a typed schema before use.

### CHAN-014 — Medium — Streaming and usage metadata are misleading
The compatibility response can claim fields/usage that do not reflect actual provider work.

**Impact:** Broken client expectations and inaccurate analytics.  
**Fix:** Return truthful capability and usage values or omit unsupported metadata.

### CHAN-015 — High — Messaging channels use one indefinite session per external user
The session ID is deterministic by channel/user with no conversation boundary or tenant dimension.

**Impact:** Old context leaks into future conversations and businesses can collide.  
**Fix:** Add tenant, conversation window, reset/expiry, and external thread/message IDs.

### CHAN-016 — High — Messaging provider retries are not deduplicated
External message IDs are not recorded before processing.

**Impact:** Duplicate replies and repeated tool side effects.  
**Fix:** Unique `(provider, tenant, external_message_id)` persistence.

### CHAN-017 — High — Media downloads are unbounded
Voice/media content is downloaded and held in memory without size/type/duration controls.

**Impact:** Memory/resource abuse and provider cost amplification.  
**Fix:** Stream with strict byte/time/content-type limits and malware/content checks as appropriate.

### CHAN-018 — Medium — Per-message HTTP clients are repeatedly created
Send/download helpers do not consistently reuse a managed client.

**Impact:** Extra latency and socket churn.  
**Fix:** Lifespan-managed clients with provider-specific limits.

### CHAN-019 — High — Channel send failures are swallowed while webhook returns success
Voice/text delivery can fail, but upstream is told processing succeeded.

**Impact:** Lost customer replies with no retry.  
**Fix:** Persist outbound message state and retry asynchronously; acknowledge inbound only after durable enqueue.

### CHAN-020 — High — All messaging traffic uses one global business profile
Channel identity is not securely mapped to a tenant/business.

**Impact:** Wrong brand, tools, data, and responses.  
**Fix:** Resolve business from verified provider account/phone/bot identifiers.

---

## 10. RAG and knowledge retrieval

### RAG-001 — High — Enabled defaults depend on undeclared packages
SQLite-vector and local embedding dependencies are missing from runtime requirements.

**Impact:** Default RAG fails during clean installation.  
**Fix:** Provide pinned extras/profiles and verify dependencies during readiness.

### RAG-002 — High — RAG failure silently disables knowledge
Initialization exceptions are caught and the call proceeds without configured business knowledge.

**Impact:** Confident hallucination/fallback instead of visible degraded state.  
**Fix:** Make knowledge availability a readiness/call-routing decision, or clearly disclose/transfer.

### RAG-003 — Critical — Top result confidence is always normalized to 1.0
Scores are divided by the top score, so whenever any result exists the top result appears maximally confident.

**Impact:** Low-quality matches bypass the configured confidence threshold.  
**Fix:** Use calibrated absolute similarity/distance thresholds and validate them on an evaluation set.

### RAG-004 — High — Vector search is global before tenant filtering
KNN is run across all chunks and only then filtered by business.

**Impact:** Other tenants can crowd out relevant results; a tenant may receive no result even though relevant chunks exist.  
**Fix:** Partition/index by tenant or include tenant filtering in the vector query.

### RAG-005 — Medium — Hash-derived vector row IDs can collide
Chunk IDs are mapped to a 63-bit hash without a collision-safe mapping table.

**Impact:** Rare overwrite/deletion/misassociation of vectors.  
**Fix:** Use a database-generated integer key with a unique chunk-ID mapping.

### RAG-006 — High — Query path scans all chunks to rebuild ID mapping
The vector result mapping performs an O(N) scan.

**Impact:** Latency grows with corpus size and blocks the synchronous DB path.  
**Fix:** Persist/index the vector-row-to-chunk mapping and join directly.

### RAG-007 — Medium — Hybrid/RRF “confidence” is relative, not calibrated
RRF rank scores do not represent probability or answer correctness.

**Impact:** Threshold settings are misleading.  
**Fix:** Rename to score and calibrate an acceptance model using labeled questions.

### RAG-008 — Medium — Retrieval parameters are insufficiently validated
Alpha/top-k and related values can be nonsensical or overly expensive.

**Impact:** Empty results, poor ranking, or unnecessary load.  
**Fix:** Constrain settings and reject invalid combinations at startup.

### RAG-009 — Medium — FTS escaping is incomplete
Only limited punctuation handling is applied; FTS syntax errors are swallowed into empty results.

**Impact:** Legitimate queries fail silently and fall back to vector-only behavior.  
**Fix:** Use parameterized/safely tokenized FTS query construction and report degraded components.

### RAG-010 — Medium — Database connections are not uniformly guarded by context/finally
Failure paths can leave resources open or transactions unclear.

**Impact:** Resource leaks and locked database files.  
**Fix:** Use context managers for every connection/transaction.

### RAG-011 — High — Upsert silently truncates on chunk/vector count mismatch
`zip` can drop excess chunks/vectors.

**Impact:** Partial corpus ingestion with no error.  
**Fix:** Validate equal counts and embedding dimensions before transaction.

### RAG-012 — High — Embedding backend/dimension is not versioned in the index
An existing DB can be reused with a different model/dimension.

**Impact:** Search errors or semantically incompatible vectors.  
**Fix:** Store index metadata and rebuild/migrate on model/version/dimension change.

### RAG-013 — High — Updated documents leave stale chunks
Content-derived IDs create new rows for changed content but do not remove prior source chunks.

**Impact:** Old and new policy text coexist and may conflict.  
**Fix:** Ingest by source/version transaction: replace or tombstone all prior chunks for that source.

### RAG-014 — Medium — Only the top answer is effectively shaped for voice
Conflicting sources and answer provenance are not surfaced.

**Impact:** One accidental match becomes authoritative.  
**Fix:** Require corroboration/metadata, preserve source citations internally, and handle conflicts explicitly.

### RAG-015 — High — Retrieval exceptions are indistinguishable from “no answer”
Handlers suppress backend failures into an ordinary no-result path.

**Impact:** Outages are hidden and caller gets an incorrect knowledge fallback.  
**Fix:** Distinguish `no_match`, `backend_unavailable`, and `invalid_query`.

### RAG-016 — Medium — Compose handler exposes raw tool errors to the model
Exceptions can become model-visible strings.

**Impact:** Internal details leak and influence prompt behavior.  
**Fix:** Use typed sanitized error results.

### RAG-017 — Medium — Supabase retriever is advertised but unimplemented
The adapter raises `NotImplementedError`.

**Impact:** Configuration appears supported but crashes at runtime.  
**Fix:** Remove from selectable settings/docs until implemented or make startup reject it clearly.

---

## 11. Frontend, streaming, and operator UI

### UI-001 — High — Audio chunks can play out of order
The browser decodes chunks concurrently and schedules based on whichever decode finishes first.

**Impact:** Later chunks may be scheduled before earlier ones.  
**Fix:** Buffer by sequence and schedule strictly in-order after each decode completes.

### UI-002 — High — No cancellation of active speech/audio/fetch work
Ending or restarting a call does not consistently abort speech synthesis, WebAudio sources, recorder work, or in-flight requests.

**Impact:** Old replies play into new sessions and background costs continue.  
**Fix:** Use `AbortController`, track audio nodes/tasks, and cancel all resources on state transition.

### UI-003 — High — Overlapping user turns are allowed
Text submissions/recording actions lack a strict turn mutex.

**Impact:** Concurrent backend turns and crossed audio.  
**Fix:** Disable/queue input per session until the current turn reaches a cancellable state.

### UI-004 — Medium — Widget infers business name from greeting text
The UI regexes the greeting rather than using structured `business_name` data.

**Impact:** Custom greetings produce wrong branding.  
**Fix:** Return and consume explicit business identity/config.

### UI-005 — Medium — Widget includes placeholder business identity
Fallback text such as “Our receptionist” can appear in customer-facing UI.

**Impact:** Unprofessional or misleading deployment.  
**Fix:** Require valid tenant branding before serving the widget.

### UI-006 — Medium — Graph polls every second without adaptive backoff
The operator UI continuously requests debug data and retains a growing span set.

**Impact:** Unnecessary load, especially across multiple operators/instances.  
**Fix:** Use server-sent events/WebSocket or backoff/visibility-aware polling with bounded state.

### UI-007 — Medium — Graph provider/model nodes are hardcoded
The visualization can show labels unrelated to actual runtime configuration.

**Impact:** Operators debug the wrong architecture.  
**Fix:** Derive graph metadata from protected runtime trace/config events.

### UI-008 — Medium — Static frontend has no build/lint/test pipeline
JavaScript is untyped, unbundled, and not covered by automated browser tests.

**Impact:** Streaming/order/security regressions are easy to ship.  
**Fix:** Add a minimal pinned frontend toolchain, ESLint, tests, and end-to-end call-flow coverage.

### UI-009 — Medium — Error UX relies on alerts/raw messages
Failures are not categorized or recoverable in a call-state model.

**Impact:** Poor operator/customer experience and accessibility.  
**Fix:** Display stable error states with retry/end/transfer actions and accessible status announcements.

### UI-010 — Medium — No offline/network retry semantics
The client does not distinguish safe retries from potentially duplicated turns.

**Impact:** Manual retries can duplicate side effects.  
**Fix:** Generate turn idempotency keys client-side and show pending/committed state.

### UI-011 — Medium — API-origin resolution is fragile across hosting modes
The simulator/widget assumptions can point at localhost or the wrong origin depending on deployment.

**Impact:** Production static hosting fails or calls unintended endpoints.  
**Fix:** Inject a signed/environment-specific API base URL at build/serve time.

### UI-012 — Medium — Cached and uncached stream event shapes differ
Clients must special-case missing provider/text fields.

**Impact:** Client bugs and divergent behavior after cache warmup.  
**Fix:** Emit the same schema for every source.

---

## 12. Observability and operational readiness

### OBS-001 — Medium — In-memory tracer is not concurrency-safe
Shared span lists are mutated without a consistent lock.

**Impact:** Races and corrupted/incomplete trace views.  
**Fix:** Use a thread-safe bounded queue or external telemetry backend.

### OBS-002 — Medium — Trace storage stops rather than behaving as a ring buffer
Once max capacity is reached, new spans may no longer be recorded.

**Impact:** The most recent incident disappears exactly when activity is high.  
**Fix:** Evict oldest spans or export continuously.

### OBS-003 — Medium — Parent/trace context propagation is incomplete
Fields exist but distributed/async parent relationships are not consistently carried.

**Impact:** Cannot reconstruct an end-to-end call across HTTP, provider, tools, and background work.  
**Fix:** Propagate trace IDs through session, turn, provider, queue, and webhook context.

### OBS-004 — High — Telemetry can contain sensitive attributes/errors
Print and OTel paths do not guarantee PII scrubbing.

**Impact:** PII enters external observability vendors.  
**Fix:** Central attribute allowlist/redactor and prohibit transcript/tool payload export.

### OBS-005 — Medium — OpenTelemetry initialization has global side effects
Repeated app creation/tests can conflict with a global tracer provider.

**Impact:** Duplicate exporters or ignored providers.  
**Fix:** Initialize once in lifespan and make tests inject a tracer.

### OBS-006 — Medium — Missing telemetry dependencies fail silently
The app can drop all traces and still report healthy.

**Impact:** Production has no diagnostics without a readiness signal.  
**Fix:** Make required observability configuration explicit per environment.

### OBS-007 — Medium — Cost numbers are estimates presented near operational data
Hardcoded provider rates and crude token/audio estimation can drift.

**Impact:** Incorrect margins and billing decisions.  
**Fix:** Version price tables, record actual provider usage, and label estimates clearly.

### OBS-008 — Low — Percentile calculation is crude
The debug p95 calculation is not a robust statistical implementation for small samples.

**Impact:** Misleading latency dashboards.  
**Fix:** Use a metrics library/histogram and define sample windows.

### OBS-009 — Medium — Startup uses `print` and broad exception swallowing
Failed tracer/cache initialization is only printed and does not affect readiness.

**Impact:** Broken functionality is easy to miss in deployment logs.  
**Fix:** Structured logging, severity, error codes, and explicit required/optional startup checks.

### OBS-010 — High — No core operational metrics
There are no reliable metrics for active calls, turn latency, queue depth, dropped audio, failed sinks, consent rejects, duplicate events, provider fallbacks, or booking outcomes.

**Impact:** Capacity and incident problems cannot be detected promptly.  
**Fix:** Add metrics and SLOs around the actual call lifecycle.

### OBS-011 — Medium — No request/call correlation ID standard
Logs and errors do not consistently include one internal call/session/turn identifier.

**Impact:** Manual debugging across components is difficult.  
**Fix:** Generate and propagate immutable correlation IDs.

---

## 13. Domain logic and product completeness

### DOMAIN-001 — High — Unknown vertical silently falls back to clinic
The vertical tool factory defaults unknown values to clinic behavior.

**Impact:** Wrong tools and business data are used instead of failing configuration.  
**Fix:** Reject unknown verticals at startup/session creation.

### DOMAIN-002 — Medium — FAQ empty-string matching can return every item
Substring matching with an empty topic can match all FAQ entries.

**Impact:** Oversized/unfocused responses and data leakage within the configured profile.  
**Fix:** Require a nonempty normalized query and use ranked search.

### DOMAIN-003 — High — Restaurant tools expose deterministic fake data as functional tools
Menu/loyalty and related handlers intentionally return realistic stubs.

**Impact:** A caller may receive fabricated balances/menu facts in a deployment that appears real.  
**Fix:** Mark demo-only tools, block them in production, and require live data connectors.

### DOMAIN-004 — High — Table booking reuses a generic single-capacity calendar model
The fake calendar does not model tables, party size, turn duration, or inventory capacity.

**Impact:** Incorrect restaurant availability and over/under-booking.  
**Fix:** Implement capacity-aware inventory or integrate an actual reservation system.

### DOMAIN-005 — Medium — Deposit requirement is only a flag
Large-party deposits do not create a payment link/state/expiry/refund workflow.

**Impact:** The agent can claim a process that is not operational.  
**Fix:** Implement payment orchestration or explicitly transfer.

### DOMAIN-006 — Medium — Real-estate qualification may not be durably persisted
Tool results can exist only in the conversation unless a sink captures the exact data.

**Impact:** Qualified leads are lost after the call.  
**Fix:** Persist structured qualification events transactionally.

### DOMAIN-007 — Medium — Wholesaler deterministic disposition data is ignored by final classification
Captured tool metadata is not necessarily used by the outbound end-of-call classifier.

**Impact:** A later LLM can overwrite stronger structured facts.  
**Fix:** Prefer deterministic tool/provider events and let the model fill only missing fields.

### DOMAIN-008 — Medium — No authoritative capability declaration per tenant
The agent can recognize intents for integrations that are absent or stubs.

**Impact:** It promises actions that cannot be completed.  
**Fix:** Generate prompts/tools from verified enabled capabilities only.

### DOMAIN-009 — Medium — Business profile fallback behavior hides bad configuration
Missing or mismatched profiles can result in a default business rather than a hard error.

**Impact:** Cross-brand responses and wrong workflows.  
**Fix:** Exact tenant/business lookup with readiness validation.

### DOMAIN-010 — Medium — No business-config schema migration/versioning
Profile semantics can change without preserving compatibility with existing calls.

**Impact:** Non-reproducible behavior and brittle deploys.  
**Fix:** Version the profile schema and snapshot config per session.

---

## 14. Packaging, deployment, tests, and repository hygiene

### DEV-001 — High — Runtime requirements are incomplete
The code/tests use Cartesia, sqlite-vec, sentence-transformers, NumPy, faster-whisper, system ffmpeg, and Python-version-specific audio support not represented in a coherent install profile.

**Impact:** Clean installs fail at import/runtime.  
**Fix:** Define pinned extras such as `browser-demo`, `twilio`, `local-voice`, `rag-local`, and `all`.

### DEV-002 — High — Dependencies are not pinned or locked
Requirements are mostly lower bounds; no lockfile is present.

**Impact:** Builds are non-reproducible and can break from upstream releases.  
**Fix:** Pin direct/resolved dependencies with hashes and automate updates.

### DEV-003 — High — Documented `.env.example` is missing
README setup instructs users to copy a file that is not in the repository.

**Impact:** Setup cannot be followed and required variables are unclear.  
**Fix:** Commit a safe, comprehensive example and validate it in CI.

### DEV-004 — High — `.env` lookup conflicts with documented working directory
Settings load `.env` relative to the process, while README startup changes into `apps/api`.

**Impact:** Users place the file at repo root but it is not loaded.  
**Fix:** Resolve env file from an explicit repo/config path or document one working method.

### DEV-005 — Medium — Referenced n8n workflow directory is missing
README/docs mention `workflows/n8n/`, but it is absent.

**Impact:** Promised integration assets cannot be used.  
**Fix:** Add them or remove the claim.

### DEV-006 — Medium — Empty dashboard application directory
`apps/web-dashboard` exists without an implementation.

**Impact:** Misleading repository surface.  
**Fix:** Remove or clearly mark planned modules outside the shipped tree.

### DEV-007 — Medium — Referenced architecture image is missing
Docs refer to `docs/assets/architecture.png`, but only source diagram files are present.

**Impact:** Broken documentation.  
**Fix:** Generate it in docs CI or reference renderable source.

### DEV-008 — High — No container/deployment definition
No Dockerfile, Compose, process manager, reverse-proxy, TLS, volume, or worker guidance is provided.

**Impact:** Unsafe and inconsistent deployments.  
**Fix:** Supply hardened deployment manifests with explicit single/multi-worker support.

### DEV-009 — High — No CI pipeline
No GitHub Actions or equivalent workflow runs tests, linting, type checks, dependency scans, or frontend checks.

**Impact:** Regressions and insecure dependencies can ship unnoticed.  
**Fix:** Add CI gates for every supported profile.

### DEV-010 — Medium — No lint/type/security tooling configuration
No Ruff, MyPy/Pyright, Bandit/Semgrep, pre-commit, or equivalent configuration is present.

**Impact:** Many issues found here are mechanically preventable.  
**Fix:** Add staged static checks and enforce them in CI.

### DEV-011 — High — Included test suite is red on a clean environment
Twenty tests fail because of undeclared dependencies and sanitizer contract mismatch.

**Impact:** There is no trusted green baseline.  
**Fix:** Make the default profile green; separate integration/optional tests with explicit markers and install matrices.

### DEV-012 — Medium — Thirty-six tests are skipped, including adversarial/live paths
The most realistic provider and nightmare-caller behavior is not continuously validated.

**Impact:** Local unit confidence does not prove the deployed system works.  
**Fix:** Run deterministic mocks on every commit and scheduled sandbox-provider end-to-end tests.

### DEV-013 — Medium — Resource warnings indicate incomplete SQLite cleanup
Compliance tests report unclosed database resources.

**Impact:** Long-running processes/tests can leak descriptors or hold locks.  
**Fix:** Audit every connection/cursor/context path and make cleanup assertions.

### DEV-014 — Medium — No coverage threshold/report
A large test count does not show which production routes/branches remain untested.

**Impact:** False confidence from raw pass counts.  
**Fix:** Publish branch coverage and set sensible module thresholds.

### DEV-015 — High — No database migration tests
There are no upgrade/downgrade/backward compatibility tests.

**Impact:** Production data evolution is unproven.  
**Fix:** Add migration tooling and test from every supported prior schema.

### DEV-016 — Medium — Hardcoded developer filesystem paths
Scripts/docs contain `/Users/...` and `/mnt/c/Users/...` paths.

**Impact:** Non-portable tooling and accidental local-data assumptions.  
**Fix:** Use CLI arguments/config and repository-relative paths.

### DEV-017 — Medium — Generated artifacts are committed
`__pycache__`, `.pytest_cache`, SQLite DB, and calendar artifacts appear in the snapshot.

**Impact:** Repository noise, stale local state, and risk of future PII commits.  
**Fix:** Strengthen `.gitignore`, purge generated files, and add secret/PII pre-commit checks.

### DEV-018 — Medium — Package import relies on `sys.path` mutation
`apps/api/app/main.py:6-9` inserts repo root at runtime.

**Impact:** Import behavior differs by working directory and packaging environment.  
**Fix:** Make the repository installable with `pyproject.toml` and proper packages.

### DEV-019 — High — Documentation contradicts implementation phase/status
Architecture docs describe different current/future transport phases, while direct Vapi/Twilio routes coexist with explicit `NotImplementedError` transport adapters.

**Impact:** Maintainers cannot tell which path is supported.  
**Fix:** Maintain one capability matrix generated/tested against code.

### DEV-020 — Medium — Selectable adapters are explicit stubs
Twilio/Vapi/LiveKit transport abstractions, Supabase RAG, and GHL-primary calendar paths raise `NotImplementedError`.

**Impact:** Configuration can select unsupported capabilities at runtime.  
**Fix:** Do not expose unimplemented enum values; fail startup with a precise unsupported-capability message.

### DEV-021 — Medium — Deprecated FastAPI startup events are used
`@app.on_event("startup")` remains instead of lifespan management.

**Impact:** Harder resource ownership and future framework migration.  
**Fix:** Use one lifespan context for startup/readiness/shutdown.

### DEV-022 — Medium — No graceful shutdown strategy
Background tasks, provider clients, models, queued work, and active calls are not drained/closed coherently.

**Impact:** Deploys drop work and leak resources.  
**Fix:** Stop admission, drain/cancel with deadlines, persist call state, close providers, then exit.

### DEV-023 — High — No environment safety mode
There is no single validated distinction between demo, test, staging, and production that disables permissive providers/stubs/debug routes/real dialing appropriately.

**Impact:** Demo defaults can accidentally reach production side effects.  
**Fix:** Add an explicit environment mode with startup invariants and non-overridable production restrictions.

### DEV-024 — Medium — No dependency-vulnerability/SBOM process
Without a lockfile and CI scan, the deployed dependency set is unknown.

**Impact:** Vulnerable transitive packages may remain unnoticed.  
**Fix:** Produce an SBOM and run automated CVE/license scans on resolved builds.

### DEV-025 — Medium — No load, soak, or chaos tests
There is no evidence for concurrent calls, reconnects, provider latency, worker restarts, or database contention.

**Impact:** The architecture's most serious state/concurrency defects remain invisible to unit tests.  
**Fix:** Build deterministic multi-call/load/retry/failure-injection tests before production traffic.

---

# Recommended remediation sequence

## Phase 0 — Containment

1. Do not expose the current app directly to the internet.
2. Disable `/outbound/start_batch`, `/outbound/dry_run`, `/sessions`, `/debug`, paid voice routes, and compatibility routes at the edge.
3. Ensure real provider credentials are not present until authorization and quota controls exist.
4. Use one process/worker only for any demo because state is process-local.
5. Label restaurant/loyalty/fake-calendar behavior as demo data.

## Phase 1 — Security and tenant boundary

1. Introduce authenticated users/service principals and role-based authorization.
2. Add immutable tenant/business IDs to all sessions, bookings, calls, campaigns, messages, traces, and provider mappings.
3. Verify every webhook with provider-native signatures/secrets and add replay protection.
4. Add request, upload, media, duration, token, concurrency, and spend limits.
5. Protect or remove debug/config/session-list routes.
6. Fix graph XSS and pin/bundle frontend dependencies.

## Phase 2 — Durable workflow core

1. Replace process dictionaries with durable session/campaign/call state.
2. Serialize turns per session and add sequence/message IDs.
3. Add idempotency keys to turn, booking, outbound dispatch, webhook, sink, and Sheet/CRM operations.
4. Implement an outbox/queue for provider calls and downstream exports.
5. Record outbound attempts before dispatch; persist provider events before acknowledgement.
6. Introduce PostgreSQL and migrations.

## Phase 3 — Booking and outbound correctness

1. Make irreversible guards fail closed.
2. Add strict tool argument models and authoritative scheduling policy.
3. Fix timezone handling and slot reservation/double-book prevention.
4. Make consent/DNC/caller-ID/campaign settings server-owned and mandatory.
5. Replace Sheets as operational state with a database; export to Sheets.
6. Prefer deterministic provider/tool facts over LLM disposition guesses.

## Phase 4 — Real-time performance and provider contracts

1. Move all blocking model/subprocess/DB/Google work off the event loop.
2. Establish one real-time turn deadline and cancellation propagation.
3. Centralize retry/fallback/circuit-breaker behavior.
4. Define a single telephony audio format and test every STT/TTS combination.
5. Fix browser streaming repetition/order/cancellation.
6. Add startup provider-bundle validation and truthful readiness.

## Phase 5 — Knowledge, privacy, and operations

1. Correct RAG tenant filtering, scoring, index metadata, and source replacement.
2. Add prompt-injection defenses at the authorization/data layer.
3. Implement accurate PII minimization, encryption, retention, deletion, and consent evidence.
4. Add structured logs, SLO metrics, distributed traces, and incident runbooks.
5. Add CI, lockfiles, SBOM/CVE scans, type/lint/security checks, load tests, and end-to-end provider sandbox tests.

---

# Suggested target architecture

A safer production architecture would separate the current monolith into logical ownership boundaries even if initially deployed in one repository:

- **Public edge/API:** authentication, tenant resolution, rate/body limits, webhook verification, security headers.
- **Call/session actor:** one serialized owner per active call; durable event log and snapshots.
- **Turn orchestrator:** bounded deadlines, cancellation, typed tools, no direct irreversible writes.
- **Workflow workers:** idempotent booking, outbound dispatch, sink export, and disposition jobs through durable queues/outbox.
- **Operational database:** PostgreSQL as source of truth; Sheets/CRM/calendar as external projections/integrations.
- **Provider gateway:** shared clients, one retry policy, budgets, circuit breakers, normalized LLM/STT/TTS/audio contracts.
- **Knowledge service:** tenant-partitioned index, versioned embedding metadata, calibrated retrieval acceptance.
- **Data-governance layer:** retention, encryption/tokenization, consent evidence, deletion/export, audit access.

---

# Bottom line

The repository has enough substance to become a strong product, but the current implementation should be treated as a **single-process demonstration environment**. The primary risk is not code style. It is that the software controls consequential external actions—appointments, customer records, phone calls, and provider spend—without authenticated tenancy, durable idempotency, transactional state, or fail-closed enforcement.

The first production milestone should not be more providers or vertical features. It should be a secure, durable call/workflow kernel with exact tenant ownership, verified events, bounded execution, and recoverable side effects.
