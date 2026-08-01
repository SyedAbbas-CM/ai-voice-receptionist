# voiceops-ai-agent — Project Status

**As of:** 2026-08-01
**Stage:** Strong prototype. First paying customer 4 weeks out with focused execution.

---

## What this is

A programmable voice receptionist for restaurants and medical clinics. Answers inbound calls, handles reservations / appointments / FAQs, escalates to a human when needed. Runs on FastAPI with pluggable STT / LLM / TTS providers, a live n8n-style dashboard, and a customer-facing browser call widget.

Two verticals shipped: **Corvina Coastal Kitchen** (Slabtown Portland restaurant, 10 tools) and **Smile Dental Clinic** (4 tools). Both drivable through the same brain via a business profile JSON.

---

## Repo layout

```
receptionist-agent/
├── apps/
│   ├── api/                  FastAPI backend (brain + all HTTP routes)
│   ├── call-simulator/       Dev UI — transcript + tool-call panels
│   ├── call-widget/          Customer-facing UI — clean bubbles, hold-to-talk
│   └── graph/                Live n8n-style architecture dashboard
├── packages/                 Pure-Python domain packages
│   ├── core_agent/           Brain + input guard + sanitizer + write guard
│   ├── voice/                VAD, greeting cache, filler pool, sentence split
│   ├── integrations/         Vertical tool sets + Vapi/Google clients
│   ├── channels/             WhatsApp/Telegram stubs (not shipped)
│   ├── rag/                  Ingest / embed / retrieve / voice-shape
│   ├── schemas/              Pydantic types (BusinessProfile, CallState, ToolCall)
│   ├── compliance/           TCPA guard + PII redaction
│   └── observability/        OTel-shape tracer + per-provider cost estimator
├── scripts/                  One-shot ops scripts
├── docs/                     Public docs (this file)
└── sample-data/              4 business profiles (clinic, restaurant, real-estate, wholesaler)
```

Totals: ~155 Python files, ~19k lines. 308 files in the shareable zip.

---

## Core classes — what they do

### `ReceptionistBrain` — `packages/core_agent/brain.py`
The turn loop. Takes a `CallState` + user text → returns reply + tool_results.
- `handle_user_turn(state, text)` — main entry. Input guard → LLM.complete with tools → tool loop up to 4× → forces text reply if loop exhausts.
- `greet(state)` — first-utterance path, uses greeting cache if warm.
- Owns: `llm`, `business`, `tools`, `tool_handler`.

### `RouterLLM` — `apps/api/app/providers/llm/router_llm.py`
Provider-agnostic fallover. Iterates `[groq, gemini, nvidia, openrouter]` until one succeeds. 8s per-provider timeout, 30s cool-down on failure. Prevents 20-30s dead-air when any single provider rate-limits.

### Provider abstractions — `apps/api/app/providers/`
Three ABCs in `base.py`: `LLMProvider`, `STTProvider`, `TTSProvider`. Concrete adapters:
- LLM: Groq, Gemini, NVIDIA NIM, OpenRouter, Cerebras, OpenAI, Anthropic, Ollama
- STT: Deepgram, Groq, OpenAI, LocalWhisper
- TTS: Cartesia, ElevenLabs, Deepgram, OpenAI, Qwen3, Kokoro, Chatterbox, Piper, Browser

Factory in `factory.py` reads `LLM_PROVIDER`/`STT_PROVIDER`/`TTS_PROVIDER` env.

### `CartesiaTTS` — `apps/api/app/providers/tts/cartesia_tts.py`
Sonic-3 via SSE. 188ms P50 first-byte. Overrides `stream_sentences()` to yield PCM chunks progressively.

### `sanitize_for_speech()` — `packages/core_agent/speech_sanitizer.py`
Six-pass text transform before every TTS call:
1. Strip brackets, tool-name leakage
2. Expand abbreviations (Dr./Mr./Mrs.)
3. Normalize numerics ($25 → "twenty five dollars", 555-1234 → "five five five, one two three four", 2026-08-01 → "August first twenty twenty six")
4. Em-dash → comma
5. Collapse whitespace
6. Flow mode (period → comma before continuations, kills TTS staccato)

### `is_probable_injection()` — `packages/core_agent/input_guard.py`
Runs BEFORE the LLM. 30+ regex patterns for jailbreak, exfil, fake-authority, minor-caller detection. Returns a canned safe reply. <1ms, zero LLM cost.

### `build_system_prompt()` — `packages/core_agent/prompt.py`
Templates a 15k-char system prompt from `BusinessProfile`. Persona, mood-awareness, tool list, hallucination guardrails, compliance refusals, emergency override, child-caller handling.

### `session_manager` — `apps/api/app/core/session_manager.py`
- `start_session_with_id(sid)` — create in-memory `CallState` + `ReceptionistBrain`
- `run_greeting(state, brain)` — play cached greeting
- `run_user_turn(state, brain, text)` — brain → PII redact → SQLite write
- `load_business()` — read `BUSINESS_PROFILE_PATH`

### `write_guard` — `packages/core_agent/classifiers/write_guard.py`
LLM-verdicts every booking-tool call against the transcript. Blocks hallucinated names/phones/dates.

### `emergency_classifier` — `packages/core_agent/emergency_classifier.py`
Regex-fast-path for chest pain / bleeding / suicidal phrases → optional LLM slow-path.

### `greeting_cache` — `packages/voice/greeting_cache.py`
Pre-synthesizes the business greeting at startup. Turn-1 latency drops from ~2s to ~50ms.

### `FillerPool` — `packages/voice/filler.py`
Pre-synth clips ("one sec", "let me check") to mask tool-call latency.

### `SileroVAD` / `RmsVAD` — `packages/voice/vad.py`
Endpointing. Silero when torch available, RMS fallback.

### Vertical tool handlers — `packages/integrations/*_tools.py`
Each vertical exports `build_<vertical>_tools()` → list[ToolDefinition] and `<Vertical>ToolHandler` → async dispatcher. Verticals: clinic, restaurant, real_estate, wholesaler.

---

## HTTP API surface

**Chat lifecycle:**
- `POST /chat/start` → session_id + greeting
- `POST /chat/turn` → text-in, text-out + tool_results
- `POST /chat/end` → close session

**Voice I/O:**
- `POST /voice/stt` — multipart audio → transcript
- `POST /voice/tts-stream` — NDJSON chunks (widget uses this)
- `POST /voice/tts`, `POST /voice/tts-base64` — one-shot variants

**Sessions:**
- `GET /sessions` / `GET /sessions/{sid}` / `GET /sessions/{sid}/bookings`

**Vapi custom-LLM (PSTN via Twilio):**
- `POST /vapi/chat/completions` — Vapi hits us for LLM replies during phone calls
- `POST /vapi/events` — end-of-call reports

**ElevenLabs-compat façade:**
- `GET /v1/voices`, `POST /v1/text-to-speech/{voice_id}[/stream]`

**Telephony bridges:**
- `POST /twilio/voice` — bare Twilio bidirectional
- `POST /channels/*` — WhatsApp/Telegram webhooks

**Outbound:**
- `POST /outbound/start_batch` — TCPA-checked batch dialer

**Observability:**
- `GET /debug/traces`, `/debug/traces/summary`, `/debug/config`

**Static mounts:**
- `/graph/` — live n8n-style dashboard
- `/call/` — customer-facing widget
- `/simulator/` — dev tool

---

## Observability

- **`InMemoryTracer`** captures every span (`voice.stt`, `voice.tts`, `gen_ai.chat_completion`, `tool.*`) with duration + attributes + status
- **Cost estimator** (`packages/observability/cost.py`) — per-provider rate cards attached to spans
- **`PIIRedactor`** — phone/SSN/DOB scrub before SQLite write
- **SQLite transcripts** at `data/sessions.db` — one row per turn

---

## Test coverage

- **477 pytest tests pass** — brain, sanitizer, input_guard, vertical tools, TTS providers (mocked + gated live tests), router, VAD, RAG, extractors
- **Adversarial harness** — 35 LLM-caller scenarios × LLM-as-judge. Current: 18/34 pass, 0 hard-fails. Scenarios in `apps/api/tests/adversarial/scenarios/`.

---

## Configuration

Everything env-driven via Pydantic `Settings` in `app/core/config.py`:

```
LLM_PROVIDER=router                    # or groq | gemini | nvidia | openrouter | ...
LLM_ROUTER_ORDER=groq,gemini,nvidia,openrouter
LLM_ROUTER_COOLDOWN_S=30
LLM_ROUTER_TIMEOUT_S=8
STT_PROVIDER=deepgram                  # or groq | openai | local
TTS_PROVIDER=cartesia                  # or elevenlabs | qwen3 | ...
BUSINESS_PROFILE_PATH=sample-data/clinic/business.json
```

Plus provider keys (Groq / Gemini / NVIDIA / OpenRouter / Cartesia / ElevenLabs / Deepgram / Twilio / Vapi).

---

## Deployment surfaces

Currently:
- **Local dev:** `uvicorn app.main:app --port 8000`
- **Public URL:** Cloudflare named tunnel routes `agent.eternalconquests.com` → Mac's `:8000`
- **Vapi + Twilio:** Number `+1-417-574-3859` routes to Vapi assistant → hits our custom-LLM webhook

Next: multi-region deploy on Fly.io or Railway (see enterprise roadmap).

---

## What we DON'T have yet

See `ENTERPRISE_ROADMAP.md` for the ranked gap analysis. Short version:

- Multi-tenancy (one deployment = one business today)
- Self-service business onboarding UI
- Per-tenant call recording / playback / analytics UI
- Real POS/EHR integrations (Toast, Athena, Google Calendar — all stub `FakeCalendar` today)
- SOC 2 Type II, HIPAA BAAs, PCI-safe payment flow
- Warm human transfer (only escalate-to-human callback today)
- Multi-language (English only)
- Answering-machine detection for outbound
- White-label branding + per-tenant billing

These are **execution problems, not research problems.** All addressed in the roadmap.

---

## The defensible wedge

Three assets most vertical competitors lack:
1. **On-prem / air-gapped deployment** via 2× RTX 3090 + Ollama + 186 GB of cached models. Sells the paranoid healthcare CIO on the first call.
2. **Own-voice cloning per tenant** via Qwen3-TTS (Apache 2.0). Runs on the same on-prem rig — no data leaves the tenant boundary.
3. **Published adversarial harness + input guard.** 34-scenario red-team is not standard yet. Becomes a security-questionnaire attachment once we push pass rate above 30/34.

---

## Repo hygiene

- `.env` is gitignored. Secrets never committed.
- `.env.bak` and `*.bak` gitignored (temp files from `sed -i`)
- `docs/rnd-2026-07/` gitignored — private draft research, not for the public repo
- `data/`, `output/`, models, audio, PDFs all excluded from commits

Public-safe docs in `docs/`:
- `PROJECT_STATUS.md` (this file)
- `ENTERPRISE_ROADMAP.md` — 90-day plan to first paying customer + first enterprise
- `assets/` — architecture diagrams (Mermaid + D2 + sequence)
