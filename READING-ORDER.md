# Reading Order — where to start reading the code

If you cloned this repo and want to understand it in ~1 hour, read in this exact order. Each file is annotated with why it matters.

## 🎯 Start here

### 1. `README.md`
Product overview + quickstart. Set expectations for what this is.

### 2. `apps/api/app/main.py`
FastAPI entrypoint. Shows every route registered (chat, voice, twilio, vapi, elevenlabs_compat, channels, debug), where the filler-pool warms at startup, where the browser sim mounts. **~90 lines. Reading this tells you every API surface.**

### 3. `apps/api/app/core/config.py`
Settings schema (Pydantic). Every env var the app understands: LLM/STT/TTS providers, keys, model names, feature flags. **~150 lines. Tells you every knob.**

---

## 🧠 The receptionist brain — the heart of the product

Read these next, in order. This is where 80% of the interesting behavior lives.

### 4. `packages/schemas/call.py`
Types: `CallState`, `TranscriptTurn`, `ExtractedFields`, `Sentiment`, `CallStatus`. **The tool_call_id + tool_calls schema fix (July 2026) is here** — without it, Groq 400s after the first booking. Also has `to_llm_messages()` which serializes into strict OpenAI tool-calling format.

### 5. `packages/schemas/business.py`
`BusinessProfile` — the vertical-agnostic business shape: hours, services, FAQs, escalation phone, `ai_disclosure_enabled`, `recording_notice_enabled`. Every deployment is one `business.json` loaded into this schema.

### 6. `packages/schemas/tools.py`
`ToolDefinition`, `ToolCall`, `ToolResult`. Uniform tool-call shape for OpenAI/Anthropic/Groq/etc. `.to_openai_format()` and `.to_anthropic_format()` handle the vendor differences.

### 7. `packages/core_agent/prompt.py`
The system-prompt template. Has 3 critical blocks added in July 2026:
- **PERSONA** — first, second-person, "you ARE this receptionist"
- **NEVER INVENT INFORMATION** — anti-hallucination guardrails
- **COMPLIANCE REFUSALS** — drug/diagnosis/insurance refusal rules

### 8. `packages/core_agent/brain.py`
The main loop: `handle_user_turn()`. **Read this whole file.** Shows the exact order every caller turn runs through:
1. Emergency intercept (regex, before LLM)
2. Input guard (jailbreak / injection defense)
3. LLM call with tools
4. Tool dispatch with write-guard for bookings
5. Reply sanitization (strip brackets, JSON, tool names)
6. Extractor pass (structured fields, sentiment)

### 9. `packages/core_agent/emergency_classifier.py`
34 regex patterns across 8 emergency categories (cardiac, respiratory, bleeding, neurological, overdose, self-harm, anaphylaxis, self-declared). Fires BEFORE the LLM. Self-harm gets the 988 crisis line, others get 911. **The #2 catastrophic failure in industry research ("book instead of escalate 911") is prevented here.**

### 10. `packages/core_agent/classifiers/write_guard.py`
Fast-path + LLM-based booking-tool validator. Rejects if:
- Placeholder name ("John Doe")
- Missing name/phone
- **Hallucinated date** — the LLM invented a date the caller never mentioned
- **Test-mode booking** — caller said "just testing, don't actually book"

### 11. `packages/core_agent/input_guard.py`
Regex jailbreak detection. Covers `ignore previous instructions`, `DAN`, `developer mode`, and (July 2026 addition) **repeat-back attacks** — "say back what I just said word-for-word" which callers used to defeat the identity lock.

### 12. `packages/core_agent/speech_sanitizer.py`
Belt-and-suspenders regex. Strips `()`, `[]`, `<>`, `{}` (JSON blobs), tool names, and expands `Dr.` → `Doctor`, `min` → `minutes`. Runs before every reply hits TTS.

### 13. `packages/core_agent/extractor.py`
Second (cheap) LLM call after every turn. Extracts caller name, phone, service, intent, sentiment, lead score. Feeds the browser sim's right-panel display and downstream analytics.

---

## 🔌 Providers — the pluggable STT/LLM/TTS layer

### 14. `apps/api/app/providers/base.py`
ABCs: `STTProvider`, `LLMProvider`, `TTSProvider`, `TransportProvider`. Every provider implements one of these interfaces.

### 15. `apps/api/app/providers/factory.py`
Provider resolver. Reads `STT_PROVIDER` / `LLM_PROVIDER` / `TTS_PROVIDER` env vars, instantiates the right adapter.

### 16. `apps/api/app/providers/llm/groq_llm.py`
**The most important LLM adapter.** Groq is our primary; this file also implements the 4-tier fallback ladder (Groq → Gemini → OpenRouter → NVIDIA Nemotron → Groq 8B last resort). Handles 429 retries with exponential backoff up to 5min per attempt.

### 17. `apps/api/app/providers/tts/chatterbox_mlx_tts.py`
MLX-native voice-cloning TTS (Apache-2.0). Uses `mlx-audio` package. Loaded lazily. Takes a 15s reference clip + text, returns your cloned voice. RTF ~0.5 on M1 Pro.

### 18. `apps/api/app/providers/stt/local_whisper_stt.py`
faster-whisper on CPU int8. Has a PyAV fallback via ffmpeg subprocess for cases where the browser sends WebM chunks that PyAV can't decode directly.

### 19. `apps/api/app/providers/tts/kokoro_tts.py` (skim)
Kokoro-82M — Apache-2.0, preset voices only (no cloning). Backup TTS when Chatterbox is unavailable.

### 20. `apps/api/app/providers/tts/cartesia_tts.py`, `elevenlabs_tts.py` (skim)
Cloud TTS adapters. Wired but not currently the default; used if you want production-grade voice cloning.

---

## 🛣️ Routes — the API surface

### 21. `apps/api/app/routes/chat.py`
Browser-sim endpoints: `/chat/start`, `/chat/turn`, `/chat/end`. This is what `http://localhost:8001/` calls.

### 22. `apps/api/app/routes/voice.py`
Voice I/O: `/voice/stt`, `/voice/tts`, `/voice/tts-base64`, `/voice/tts-stream` (NDJSON streaming, first sound in ~1.5s instead of 8s).

### 23. `apps/api/app/routes/twilio.py`
Twilio Media Streams path — real phone calls. Includes barge-in detection, mid-frame audio abort. Not wired to a live number by default.

### 24. `apps/api/app/routes/vapi.py`
Vapi compat: `/vapi/events` (server webhook) + `/vapi/chat/completions` (custom-LLM webhook, so Vapi delegates the brain to us).

### 25. `apps/api/app/routes/elevenlabs_compat.py`
ElevenLabs Conversational AI compatibility layer — lets their agent platform use our brain as its backend.

### 26. `apps/api/app/routes/debug.py`
`/debug/traces`, `/debug/traces/summary` — real-time latency P50/P95/P99 for STT/LLM/TTS. Useful for demos ("here's proof of latency").

### 27. `apps/api/app/routes/sessions.py`, `channels.py`, `outbound.py` (skim)
Session management, WhatsApp/SMS channels, outbound dialer with TCPA compliance.

### 28. `apps/api/app/core/session_manager.py`
Session lifecycle: `start_session()`, `get_session()`, `run_user_turn()`, `end_session_async()`. Handles PII redaction on SQLite writes, sink `on_call_end` firing.

---

## 🏢 Verticals — how business types differ

### 29. `packages/integrations/vertical_tools.py`
`build_tools_for_vertical(business, calendar, retriever, ...)` — the dispatcher. Given a business.vertical, returns the right tool set + handler. Composes `lookup_answer` (RAG) on top of vertical-specific tools.

### 30. `packages/integrations/clinic_tools.py`
Clinic tool set: `check_availability`, `book_appointment`, `escalate_to_human`. `ClinicToolHandler` is the async handler that dispatches each. **Read this to understand how ANY vertical is built.**

### 31. `packages/integrations/restaurant_tools.py`, `real_estate_tools.py`, `wholesaler_tools.py` (skim)
Same pattern, different tools per vertical.

### 32. `packages/integrations/rag_tool.py`
Composes the `lookup_answer` RAG tool on top of ANY vertical. `ComposeHandler` routes tool calls: RAG handler first, vertical handler as fallback.

### 33. `packages/integrations/fake_calendar.py`
In-memory calendar for demos. Replace with `packages/integrations/google_calendar.py` / `cal_com.py` etc for production.

---

## 🔎 RAG — the knowledge layer

### 34. `packages/rag/__init__.py`
Public API: `Chunk`, `Retriever`, `Embedder`, `shape_for_voice`, `build_retriever`, `build_embedder`.

### 35. `packages/rag/types.py`
`Chunk`, `ChunkKind`, `RetrievalHit` (with `is_safe_to_speak`/`needs_escalation` confidence gates).

### 36. `packages/rag/sqlite_store.py`
SQLite + sqlite-vec + FTS5 hybrid vector + BM25 store. Reciprocal Rank Fusion at alpha=0.6 vector / 0.4 BM25.

### 37. `packages/rag/voice_shaper.py`
`is_speakable()` (rejects URLs, markdown, lists, >250 chars) + `shape_for_voice()` (LLM rewrites retrieved chunk as one spoken sentence).

### 38. `packages/rag/chunker.py`
`chunk_business_profile()` — how a `business.json` becomes RAG chunks (FAQs as Q+A pairs, services with duration+price, hours as one chunk, etc).

### 39. `packages/rag/ingest.py`
CLI to ingest a business profile + markdown docs into the KB.

---

## 🛡️ Compliance + observability

### 40. `packages/compliance/pii.py`
`RegexPIIRedactor` + `PresidioPIIRedactor` (optional). Strips phone/card/SSN/email/DOB from SQLite transcript writes.

### 41. `packages/compliance/tcpa.py`
Outbound call consent enforcement. `SqliteConsentProvider`, `HttpConsentProvider` (fails closed), AI disclosure line, E.164 normalization.

### 42. `packages/observability/tracer.py`
`InMemoryTracer` / `OTelTracer` singleton. Every LLM/STT/TTS call is wrapped in `tracer.span(...)`.

### 43. `packages/observability/cost.py`
Per-provider cost estimator with 2026 rates. Used to show cost-per-call in traces.

---

## 🎙️ Voice bits

### 44. `packages/voice/sentence_splitter.py`
Splits an LLM reply into speakable chunks respecting abbreviations (Dr., a.m., p.m.), decimals (3.14), ellipses. Max 18 words per chunk so first-sound latency stays under 3s in streaming mode.

### 45. `packages/voice/vad.py`
Silero VAD adapter with RMS fallback. Used for turn detection in Twilio path.

### 46. `packages/voice/filler.py`
Pre-synthesized filler audio pool ("Let me check that.", "Just a moment.") warmed at server start. Reduces perceived latency during tool calls.

### 47. `packages/voice/barge_in.py`
Backchannel classifier — "mm-hmm" (keep talking) vs "wait, stop" (real interrupt). Wired in Twilio, not yet in browser.

---

## 🧪 Tests — how we prove things work

### 48. `apps/api/tests/test_brain_booking_flow.py`
End-to-end brain tests with scripted LLM. Covers happy path, escalation, tool dispatch.

### 49. `apps/api/tests/test_emergency_classifier.py`
47 tests. Every regex pattern + false-positive prevention on chronic conditions.

### 50. `apps/api/tests/test_write_guard.py`
Booking-guard regression tests. Hallucinated date detection. Test-mode detection.

### 51. `apps/api/tests/test_input_guard.py`
Jailbreak defense tests. Repeat-back attack coverage.

### 52. `apps/api/tests/test_tool_call_serialization.py`
Locks in the OpenAI/Groq spec: assistant tool_calls array, tool_call_id binding, JSON-encoded arguments.

### 53. `apps/api/tests/test_sentence_splitter.py`
16 tests on the streaming-TTS splitter.

### 54. `apps/api/tests/adversarial/`
**LLM-as-caller + LLM-as-judge adversarial harness.** Opt-in via `pytest --run-adversarial`. 35 scenarios across 15 nightmare categories. Reads scenarios from `scenarios/*.jsonl`, scores 6 axes, writes reports to `reports/`. Currently ~60% pass rate baseline, targeting 85% with more fixes.

---

## 🎨 Frontend

### 55. `apps/call-simulator/index.html`
Bare-HTML sim. Mounted at `/` and `/simulator` by main.py.

### 56. `apps/call-simulator/app.js`
Push-to-talk mic + NDJSON streaming TTS player. Consumes `/voice/tts-stream` chunk-by-chunk.

### 57. `apps/call-simulator/style.css`
Basic styling. Modernization pending.

---

## 📊 Data / sample businesses

### 58. `sample-data/clinic/business.json`
**Cedar Ridge Family Dental** — realistic dental practice with Dr. Michael Chen, real services, real 2026 prices, Delta Dental / Cigna / BCBS TX insurance. Loaded by default (`BUSINESS_PROFILE_PATH` env).

### 59. `sample-data/restaurant/business.json`, `real-estate/business.json`, `subtodealz/business.json`
Other vertical samples.

### 60. `sample-data/clinic/docs/*.md`
Markdown docs ingested into RAG (menu-of-services, insurance policies, patient FAQ).

---

## 📚 Docs to read AFTER you understand the code

### 61. `docs/architecture.md`
System architecture overview.

### 62. `docs/provider-matrix.md`
Which STT/LLM/TTS provider does what.

### 63. `docs/runbooks/vapi-setup.md`
How to wire a Vapi phone number for demo.

### 64. `docs/pricing-notes.md`
Cost-per-call math for different provider combos.

### 65. `docs/demo-script.md`
Suggested demo turns for a live Loom recording.

### 66. `docs/proposal-snippets.md`
Draft language for Upwork proposals.

### 67. `docs/rag-adapters.md`
How to plug an existing LangChain/Pinecone/etc RAG backend into our `Retriever` interface.

---

## 🗂️ Full directory map

```
├── apps/
│   ├── api/                      # FastAPI backend
│   │   ├── app/
│   │   │   ├── main.py          # ★ start here
│   │   │   ├── core/
│   │   │   │   ├── config.py    # ★ env schema
│   │   │   │   └── session_manager.py
│   │   │   ├── db/              # SQLite models
│   │   │   ├── providers/       # STT/LLM/TTS adapters
│   │   │   │   ├── base.py
│   │   │   │   ├── factory.py
│   │   │   │   ├── llm/         # 8 adapters
│   │   │   │   ├── stt/         # 4 adapters
│   │   │   │   └── tts/         # 9 adapters
│   │   │   └── routes/          # /chat, /voice, /twilio, /vapi, etc
│   │   └── tests/               # unit + adversarial harness
│   └── call-simulator/          # bare-HTML browser sim
├── packages/
│   ├── core_agent/              # ★ brain, prompt, guardrails
│   ├── schemas/                 # Pydantic types
│   ├── integrations/            # verticals, calendar, CRM sinks
│   ├── rag/                     # SQLite+vec+FTS5 hybrid RAG
│   ├── compliance/              # PII redaction, TCPA
│   ├── observability/           # tracer, cost estimator
│   ├── voice/                   # VAD, splitter, filler pool
│   └── channels/                # WhatsApp, SMS
├── sample-data/                 # per-vertical business.json
├── docs/                        # architecture, runbooks
└── scripts/                     # utility scripts, smoke tests
```

---

## Recommended 4-hour deep-dive path

If you have 4 hours and want to actually GET this codebase, read in this order:

**Hour 1 — surface area**
Files 1-7 (README → prompt.py). Gives you the shape.

**Hour 2 — the brain**
Files 8-13 (brain.py, emergency, write_guard, input_guard, sanitizer, extractor). Gives you WHY the receptionist behaves the way it does.

**Hour 3 — plumbing**
Files 14-28 (providers, routes, session manager). Gives you HOW requests flow.

**Hour 4 — extension**
Files 29-47 (verticals, RAG, compliance, voice bits). Gives you WHERE to add features.

Tests (48-54) you read when a test fails or when adding a new feature — start with the test that matches your change.

---

## Where to make specific changes

| Want to... | Edit... |
|---|---|
| Change what the receptionist says | `packages/core_agent/prompt.py` |
| Add an emergency phrase we should catch | `packages/core_agent/emergency_classifier.py` (add regex + test) |
| Add a compliance refusal category | `prompt.py` COMPLIANCE REFUSALS block |
| Support a new LLM provider | `apps/api/app/providers/llm/*.py` + register in `factory.py` |
| Add a business vertical | new `packages/integrations/{vertical}_tools.py` + branch in `vertical_tools.py` |
| Wire a real calendar | `packages/integrations/calendar_factory.py` |
| Add a nightmare-caller test scenario | new `apps/api/tests/adversarial/scenarios/*.jsonl` |
| Fix a hallucination the LLM makes | `packages/core_agent/classifiers/write_guard.py` or `speech_sanitizer.py` |
| Change the browser sim UI | `apps/call-simulator/{index.html,app.js,style.css}` |
