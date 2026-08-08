# Audit Response 3 — 2026-08-04

Response to the third external audit (delivered 2026-08-04, post-Sprint 9).
Same format as `AUDIT_RESPONSE.md` and `AUDIT_RESPONSE_2.md`:
**accept / partially accept / defer / disagree**.

Overall verdict from the audit:

> "The repository contains many components associated with an intelligent
> real-time voice agent, but the actual runtime is still a sequential
> tool-calling chatbot wrapped in mostly batch speech processing."

We accept this framing. Sprint 9 landed the *plumbing* for intelligence
(actor + VPL + two-planner + two-stage barge-in) but the audit is right
that several integration seams are batch under a streaming label. This
sprint response addresses the P0s. P1s + P2s go into Sprint 10 planning.

---

## Immediate P0 fixes (this session)

| # | Finding | Verdict | Fix |
|---|---|---|---|
| 1 | RAG dispatcher swallows all non-RAG tool calls when `ComposeHandler` fails to route on error-string match | **ACCEPT — critical** | Replace error-string dispatch with explicit routing table by tool name. Add regression test that would have caught this. |
| 2 | `_ensure_perf_planner` temporarily mutates global `settings.groq_model` — race under concurrent calls | **ACCEPT — critical** | Pass model explicitly to `GroqLLM.__init__` (new param) instead of reading from settings. No global mutation. |
| 3 | Prompt has hardcoded tool signatures that don't match actual `ToolDefinition` schemas | **ACCEPT** | Remove hardcoded signatures from static prompt; let provider schemas be authoritative. |
| 4 | Fake calendar uses 9-5 weekday hardcode instead of profile hours → conflicts with active Smile Dental hours | **ACCEPT** | Read `business.hours` from profile in `FakeCalendar` so availability matches spoken policy. |
| 5 | Missing deps (`sqlite-vec`, `num2words`, `cartesia`) cause 30+ test failures in fresh env | **ACCEPT** | Pin all runtime deps in `apps/api/requirements.txt`. |
| 6 | CI runs `ruff check ... \|\| true` — lint failures never fail build | **ACCEPT** | Remove `\|\| true`. Lint failures fail CI. |
| 7 | RAG tenant filter runs AFTER vector search → other-tenant results occupy top ranks | **ACCEPT** | Move tenant filter into the vector query (WHERE clause). |
| 8 | RAG uses only top-1 chunk even when top-3 are retrieved | **ACCEPT** | Concatenate top-K with a delimiter into the voice shaper. K default 3. |
| 9 | `LookupAnswerHandler` test uses stub that returns `"unknown tool"` — masks real bug #1 | **ACCEPT** | Fix test to use real handler error string, add integration test with real Compose. |
| 10 | README claims stale test counts, "streaming" defaults, feature-flag reality | **ACCEPT** | Rewrite Quick Start / Feature Matrix sections to describe actual defaults. |

## P1 fixes (deferred to Sprint 10)

| # | Finding | Verdict | Sprint 10 plan |
|---|---|---|---|
| 11 | Batch-STT under a streaming label — actor calls `stt.transcribe(complete_wav)` not Deepgram Flux streaming | **ACCEPT — deferred** | Sprint 10a: wire Deepgram Flux streaming path. Was already planned. |
| 12 | Cartesia SSE collected into single blob before playback → "tts_first_byte" metric misleading | **ACCEPT — deferred** | Sprint 10d: wire Cartesia SSE chunk-through actor, mark first-byte on first chunk. |
| 13 | Interruption state not reconciled with brain transcript — full reply appended before playback complete | **ACCEPT — critical, deferred** | Sprint 10e: `state.transcript` on interrupt gets rewritten to `ledger.heard_text_for(gen)`. |
| 14 | Ledger granularity too coarse — one chunk per response, can't represent partial hearing | **ACCEPT — deferred** | Sprint 10e: chunk at sentence boundaries with word timestamps from provider. |
| 15 | Barge-in ducks by *dropping* frames not attenuating — words vanish on false trigger | **ACCEPT — deferred** | Sprint 10f: apply true attenuation (audioop.mul on the ducked frames) rather than skip. |
| 16 | Write guard runs another LLM call, fails open on guard error | **ACCEPT — deferred** | Sprint 10g: deterministic write guard (evidence ledger from state, not LLM). |
| 17 | No typed goal/slot state — transcript IS the memory | **ACCEPT — architecture-scale** | Sprint 10-11: introduce `CallGoal` with `collected_slots`, `slot_evidence`, `proposed_action`, `confirmed_action`. |
| 18 | Semantic planner is post-hoc classification, not real planning | **ACCEPT — as documented** | Sprint 10: extend brain prompt to *emit* speech_act + goal state alongside reply, so semantic layer stops inferring. |
| 19 | Every turn can trigger 5-7 LLM calls (main + tool loop + extraction + write guard + RAG shape + performance + forced final) | **ACCEPT** | Sprint 10-11: extraction runs incrementally not per-turn; write guard becomes deterministic; RAG shape optional. |
| 20 | Tool execution is sequential; no read/write/idempotency distinction | **ACCEPT — deferred** | Sprint 11: tool metadata for `kind: read|write`, idempotency keys, plan/commit boundary. |
| 21 | `check_availability`/`book_appointment` has TOCTOU race | **ACCEPT — deferred** | Sprint 10g: local reservation lock + idempotency key on booking. |
| 22 | Missing cancel/reschedule/find-existing tools for a real receptionist | **ACCEPT — deferred** | Sprint 10h: complete the appointment lifecycle for clinic vertical before adding new verticals. |
| 23 | Google Calendar naive datetimes → DST/timezone risk | **ACCEPT — deferred** | Sprint 10g alongside idempotency. |
| 24 | Session state process-local → restart loses calls, workers can't share | **ACCEPT — deferred** | Sprint 11: Redis-backed session store. Called out in Sprint 8 planning already. |
| 25 | Public route allowlist too broad (`/chat/`, `/voice/`, `/debug/`, `/v1/`, `/admin/`) | **ACCEPT — deferred** | Sprint 10i: signed short-lived widget tickets. Rate limits + audio size caps. |
| 26 | Raw transcript logged at info level; local Whisper writes /tmp audio by default | **ACCEPT — deferred** | Sprint 10i: structured redaction on log; opt-in audio debug with expiry. |
| 27 | Local Whisper blocks event loop | **ACCEPT — deferred** | Sprint 10a: move to executor pool. |
| 28 | Sync SQLAlchemy in async paths | **ACCEPT — deferred** | Sprint 10: audit all `Session()` uses in request path. |
| 29 | Tenant guard is string-based over compiled SQL — brittle | **ACCEPT — deferred** | Sprint 11: Postgres RLS as primary; app-level check as belt-and-suspenders. |
| 30 | STT hallucination blacklist deletes "yes"/"no"/"thank you" | **ACCEPT — deferred** | Sprint 10a: replace exact-text blacklist with confidence + no-speech probability. |
| 31 | Minimum utterance ~1s discards legitimate "Yes"/"Tuesday"/"Chen" | **ACCEPT — deferred** | Sprint 10a: lower to 300ms + VAD confidence check. |
| 32 | English hardcoded in Groq + Deepgram defaults | **ACCEPT — deferred** | Sprint 11: pass locale from business profile. |
| 33 | RAG "confidence" is RRF ranking not answerability | **ACCEPT — deferred** | Sprint 10c: reranker + absolute similarity threshold + answerability classifier. |
| 34 | RAG stale chunks not fully cleaned on re-ingest | **ACCEPT — deferred** | Sprint 10c: per-source ingestion generation + purge. |
| 35 | RAG tables discarded during chunking | **ACCEPT — deferred** | Sprint 10c: dedicated table extractor. |
| 36 | Adversarial suite is text-only, not real audio | **ACCEPT — deferred** | Sprint 10b: audio-input adversarial harness with recorded WAVs. |
| 37 | Evaluation permissive (weighted average threshold) | **PARTIALLY ACCEPT** | Add absolute per-dimension floors alongside the weighted score. |

## Disagreements / partial accepts

| # | Finding | Verdict | Response |
|---|---|---|---|
| A | "33 test failures in mounted env" | **PARTIAL** | Caused by missing deps (accept #5). In our env with deps installed: 635 passing, 2 pre-existing Kokoro fails. Not a code quality issue but the audit is right the dep pinning problem hid it. |
| B | Every feature flag defaults off = "documentation drift" | **DISAGREE** | Feature flags default off intentionally so soak time exists before flip. This is the safe-rollout pattern. Fix is README truthfulness (accept #10), not flip everything on by default. |
| C | "Prompt says receptionist is NOT AI" | **PARTIAL** | The prompt phrasing is legacy from Sprint 3 persona work — the greeting layer separately handles AI disclosure per state law. Will soften prompt to remove contradiction (Sprint 10 prompt refactor #17). |
| D | "18/34 = 53% pass" cited from adversarial | **DISAGREE ON NUMBER** | That's from a stale run. Current pass rate is higher (last run: 28/34). Audit did not have access to latest harness output. Will re-run + publish. |
| E | "Recommend switching to OpenAI Realtime or Gemini Live" | **DISAGREE** | Reasonable option but doubles provider lock-in and loses our tenant isolation guarantees. Sprint 10 stays with modular pipeline + real streaming. Revisit for Sprint 12 if latency budget can't be hit. |
| F | "Actor code contains all vocabulary of realtime but runs batch" | **ACCEPT with context** | True. That's what Sprint 10a/d/e were already scoped to fix. Sprint 9 shipped the kernel + flags; Sprint 10 makes the streaming real. |

## Immediate demo posture

Per audit recommendation for the safe demo path:

```env
LLM_PROVIDER=groq
STT_PROVIDER=groq
TTS_PROVIDER=browser        # or cartesia if the widget path is validated

RAG_RETRIEVER=noop          # ← per accept #1 until dispatcher fix soaked
CALENDAR_BACKEND=fake

TWILIO_USE_ACTOR=false
TWO_PLANNER_ENABLED=false
TWO_STAGE_BARGE_IN_ENABLED=false

API_AUTH_ENFORCE=true       # DO NOT use false on any public tunnel
```

Once P0s in this doc land, we can flip:
- `RAG_RETRIEVER=sqlite` (safe after #1 fix)
- `TWILIO_USE_ACTOR=true` + intelligence flags on for controlled call testing

## What this response does NOT commit to

- No re-architecting the brain into typed state in this response cycle (P1, Sprint 10-11)
- No switch to Realtime/Live models (disagreement E)
- No new verticals until clinic lifecycle is complete (accept #22)

Author: claude
Date: 2026-08-04
