# Sprint 8 Plan — Temporal Kernel + Voice DNA Foundations

**Prerequisite:** Sprint 7 remaining CRITICALs closed (CallActor design lands as part of CRITICAL-08 fix, tenant resolver for CRITICAL-12, atomic idempotency for CRITICAL-10, signed WS tokens for CRITICAL-09).
**Source:** `docs/rnd-2026-08/37-voiceops-moat-blueprint.md` — a 12-week technical strategy plan for turning this from prototype into a defensible product.
**Estimated duration:** 2 weeks solo, 1 week two-eng.

---

## The thesis this sprint implements

Every voice-agent startup will eventually have similar STT/LLM/TTS adapters, so those aren't a moat. The moat is:

1. **Temporal conversation kernel** — one actor per call owning cancellation, playback state, generation IDs
2. **Voice Performance Language (VPL)** — provider-agnostic delivery spec
3. **Voice DNA** — per-tenant voice registry with reference bank, pronunciation, adaptation

Sprint 8 delivers #1 in full and lays the schema foundation for #2 and #3.

---

## Deliverables

### 8a — `CallEvent` envelope (~1 day)

Every media/STT/LLM/tool/TTS/playback signal gets wrapped in a frozen dataclass with `call_id`, `tenant_id`, `sequence`, `monotonic_ns`, `source`, `turn_generation`, `speech_generation`, `kind`, `payload`. This is the invariant that makes cancellation correct — a newer `turn_generation` invalidates every event from older turns.

**File:** `packages/runtime/call_event.py` (new)

**Test:** deterministic actor test — replaying the same event log produces the same transcript.

### 8b — `CallActor` per-call serialization (~3 days)

Replaces the `asyncio.create_task()` per utterance in `apps/api/app/routes/twilio.py`. One `asyncio.Queue(maxsize=256)` per call, one coroutine consuming it, states `LISTENING → PREPARING → THINKING → TOOL_WAIT → SPEAKING → YIELDING → LISTENING`. Child tasks emit `CallEvent`s back to the actor; only the actor mutates state.

Nested cancellation: `call lifetime → turn_generation → { STT / LLM / tool / speech_generation → { synth / transcode / outbound } }`.

**File:** `packages/runtime/call_actor.py` (new) + refactor `twilio.py:TwilioStreamSession`

**Tests:** concurrency test — fire two utterances 200ms apart, assert first cancels cleanly, no double-book, only one full response reaches caller.

### 8c — Playback ledger (~2 days)

Three separate notions of speech: **generated** (exists in TTS worker), **queued** (sent to Twilio), **heard** (Twilio ack'd a mark past it). LLM transcript history only includes **heard**. Fixes the audit finding that our current code treats sent audio as spoken, corrupting conversation history after interruption.

Uses Twilio `mark` events per phrase boundary (~100-250ms). On `clear`, subsequent mark returns identify what was cleared vs completed.

**File:** `packages/runtime/playback_ledger.py` (new)

**Test:** interruption test — agent speaks "The address is 4592 Sengkang Way", caller interrupts at 4592, ledger's heard-text ends at "The address is". LLM sees only "The address is" in next-turn history.

### 8d — Rich `STTEvent` contract (~2 days)

Replace `{kind, text, is_final}` with `SpeechHypothesis` carrying `utterance_id, revision, words[], confidence, stability, endpoint_probability, target_speaker_probability, acoustic_state`. Wire Deepgram Flux's eager-vs-final endpointing.

**File:** modify `apps/api/app/providers/base.py` `STTEvent` → `SpeechHypothesis` + `apps/api/app/providers/stt/deepgram_stt.py`.

**Test:** replay a recorded Deepgram Flux stream, assert eager end-of-turn triggers speculative LLM, final end-of-turn commits it, resumed turn cancels speculation.

### 8e — VPL v0 schema + validator (~2 days)

JSON schema for `text`, `speech_act`, `delivery{style,intensity,rate,phrase_finality,pause_before_ms,pause_after_ms}`, `emphasis[]`, `pronunciation_refs[]`, `safety{allow_nonverbal,max_intensity}`. Strict ranges + enums, no vendor markup.

Two-stage planner: Semantic Response Planner (what to say) → Performance Planner (how to say it). The LLM proposes both, but deterministic validation enforces bounds (no laughter in medical emergencies, no exaggerated anger mirroring, pronunciation entries must be valid).

**File:** `packages/vpl/schema.py`, `packages/vpl/validator.py`.

**Test:** malformed VPL rejected, out-of-range values clamped/rejected, domain-forbidden fields rejected.

### 8f — VPL provider compilers (~2 days)

Three initial compilers: `ElevenLabsCompiler`, `CartesiaCompiler`, `Qwen3Compiler`. Each returns `CompiledSpeechPlan(request_payload, output_format, references, unsupported_fields, approximations, compiler_version)`. Unsupported VPL fields get logged, not silently dropped.

**File:** `packages/vpl/compilers/{elevenlabs,cartesia,qwen3}.py`.

**Test:** given VPL with `phrase_finality=continuing`, ElevenLabs compiler outputs text with no terminal period; Cartesia compiler sets `context_id` continuation; Qwen sets instruction.

### 8g — Voice profile registry v0 (~2 days)

Tables: `voice_profile`, `voice_profile_version`, `voice_consent_artifact`, `voice_provider_binding`. Admin API: `POST /admin/voice-profiles`, `POST /admin/voice-profiles/{id}/versions`. Call APIs receive only internal `voice_profile_version_id` — never raw provider IDs.

**File:** `apps/api/app/db/models.py` (add tables), `apps/api/app/routes/admin.py` (add routes), Alembic migration `20260803_0001_voice_registry.py`.

**Test:** create profile → create version → resolve version to provider binding at call time. Attempt to pass a raw ElevenLabs voice ID to `/chat/turn` — rejected.

---

## Explicitly NOT in Sprint 8

- Reference bank (ingestion pipeline + retrieval ranker) — Sprint 9
- Pronunciation dictionary with synth-ASR loop — Sprint 9
- Codec simulator (µ-law + jitter + noise grid) — Sprint 9
- Blind listening interface + Bradley-Terry ranking — Sprint 10
- The 11 numbered experiments in the moat doc — Sprint 10+
- GPU worker split for Qwen (bidirectional gRPC) — Sprint 10
- Full-duplex research (Moshi/PersonaPlex) — post-launch

---

## Acceptance gates for Sprint 8

Every one of these must pass before Sprint 9 starts:

1. All existing tests green (Sprint 5+6+7 baseline of 493 pass maintained).
2. Deterministic actor test: same event log → same transcript.
3. Concurrency test: two overlapping utterances → one clean response, no double-book.
4. Interruption test: LLM next-turn history contains only *heard* text.
5. Deepgram Flux replay test: eager-vs-final endpointing wired.
6. VPL malformed input rejected; every out-of-range value rejected.
7. Every VPL compiler unsupported-field rate logged and <10%.
8. Voice profile registry: raw provider voice IDs rejected at call APIs.

---

## What this unlocks

After Sprint 8:
- The temporal correctness issues from re-audit CRITICAL-08 are gone.
- We have the schema surface (`VPL`, `voice_profile`) that lets Sprint 9's reference bank + pronunciation work fit into it.
- ElevenLabs `[laughs]`/`[sighs]` markup can be exposed to the LLM safely (Sprint 8f v3-compiler variant) without corrupting the sanitizer.
- The 11 numbered experiments from the moat doc become runnable — because there's a reproducible profile version + compiler snapshot to point at.

---

## Refs

- `docs/rnd-2026-08/37-voiceops-moat-blueprint.md` — the full 12-week strategy
- `VOICEOPS_REAUDIT_2026-08-02.md` — the security audit that Sprint 7 addresses
- `docs/AUDIT_RESPONSE_2.md` — Sprint 7's accept/defer/disagree tracking
- `docs/ENTERPRISE_ROADMAP.md` — the 90-day commercial roadmap
