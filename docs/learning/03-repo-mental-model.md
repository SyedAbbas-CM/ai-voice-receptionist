# The repo in one page

Every file mapped to which of the five pipeline stages it belongs to. When something breaks, find the stage first, then the file.

```
voiceops-ai-agent/
├── apps/
│   ├── api/                        ← the FastAPI server that ties everything together
│   │   ├── app/
│   │   │   ├── main.py             ← startup, mounts routers, serves the simulator HTML
│   │   │   ├── core/
│   │   │   │   ├── config.py       ← every env var lives here as a typed setting
│   │   │   │   └── session_manager.py  ← per-call state, in-memory + SQLite mirror
│   │   │   ├── db/                 ← SQLite tables: sessions, transcript, bookings
│   │   │   ├── providers/          ← ADAPTERS for stages 2, 3, 5 (STT / LLM / TTS)
│   │   │   │   ├── base.py         ← the interfaces every adapter implements
│   │   │   │   ├── factory.py      ← reads env, returns the right adapter
│   │   │   │   ├── llm/            ← openai, anthropic, groq, gemini, ollama
│   │   │   │   ├── stt/            ← deepgram, openai (whisper), groq, local
│   │   │   │   ├── tts/            ← elevenlabs, openai, deepgram, cartesia,
│   │   │   │   │                     qwen3 (local), local (piper), browser
│   │   │   │   └── transport/      ← stubs for stage 1 (used by routes/)
│   │   │   └── routes/             ← HTTP + WebSocket endpoints, i.e. stage 1
│   │   │       ├── chat.py         ← /chat/start /chat/turn /chat/end (browser sim)
│   │   │       ├── voice.py        ← /voice/stt /voice/tts (browser sim helpers)
│   │   │       ├── sessions.py     ← /sessions (dashboard queries)
│   │   │       ├── vapi.py         ← /vapi/chat/completions (custom-LLM webhook)
│   │   │       ├── twilio.py       ← /twilio/voice + /twilio/stream (real phone calls)
│   │   │       ├── channels.py     ← /channels/whatsapp/*, /channels/telegram/*
│   │   │       └── elevenlabs_compat.py  ← /v1/text-to-speech/* (11L API impersonation)
│   │   └── tests/                  ← 22 tests. Run: pytest tests/ --asyncio-mode=auto
│   ├── call-simulator/             ← plain HTML/JS mic-based test UI (no build step)
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js
│   └── web-dashboard/              ← empty for now, planned Next.js dashboard
│
├── packages/                       ← Python code that isn't tied to FastAPI
│   ├── schemas/                    ← Pydantic models: CallState, Booking, Business...
│   ├── core_agent/                 ← THE BRAIN — stage 3
│   │   ├── brain.py                ← the tool-calling loop. Read this first.
│   │   ├── prompt.py               ← builds the system prompt from a BusinessProfile
│   │   └── extractor.py            ← turns transcript into structured JSON fields
│   ├── integrations/               ← stage 4 (tools) + sinks
│   │   ├── fake_calendar.py        ← JSON-file backed calendar for local demos
│   │   ├── google_calendar.py      ← real Google Calendar via service account
│   │   ├── calendar_factory.py     ← picks the calendar backend from env
│   │   ├── clinic_tools.py         ← the tool definitions the LLM can call
│   │   ├── ghl_client.py           ← GoHighLevel API v2 client
│   │   ├── google_sheets.py        ← Sheets logger
│   │   └── sinks.py                ← CRM sink layer (fires on booking/call-end)
│   └── channels/                   ← stage 1 for messaging (WhatsApp, Telegram)
│       ├── base.py                 ← Channel interface + IncomingMessage
│       ├── pipeline.py             ← STT→brain→TTS reused across channels
│       ├── whatsapp.py
│       └── telegram.py
│
├── sample-data/
│   └── clinic/business.json        ← the fake business the demo runs
│
├── scripts/
│   ├── create_vapi_assistant.py    ← provisions a Vapi assistant from business.json
│   └── test_qwen3_tts.py           ← download + synthesize smoke test
│
├── docs/
│   ├── learning/                   ← YOU ARE HERE — beginner explainers
│   ├── vapi-setup.md
│   ├── twilio-setup.md
│   ├── channels-setup.md
│   ├── qwen3-tts-setup.md
│   ├── elevenlabs-setup.md
│   ├── elevenlabs-compat.md
│   ├── ghl-setup.md
│   ├── google-setup.md
│   ├── provider-matrix.md
│   ├── demo-script.md
│   ├── pricing-notes.md
│   └── proposal-snippets.md
│
├── .env.example                    ← every setting the app knows about
├── README.md
└── data/                            ← runtime output: sqlite db, fake calendar, TTS samples
```

## The three files you must understand

1. **`packages/core_agent/brain.py`** — the whole product in ~150 lines.
   - `handle_user_turn` is the loop: LLM call → tool calls → LLM call → reply.
   - `MAX_TOOL_ITERATIONS = 4` — the safety net so an LLM stuck in a tool loop can't burn tokens forever.

2. **`apps/api/app/providers/factory.py`** — how "flip an env var, get a different backend" works.
   - Every `TTS_PROVIDER`, `STT_PROVIDER`, `LLM_PROVIDER` value maps to one class.
   - Add a new provider by writing a class, importing it here, and adding an `if` branch.

3. **`apps/api/app/core/session_manager.py`** — where state lives.
   - In-memory `_states` dict keyed by session_id. Mirrored to SQLite on every turn.
   - `start_session_with_id` — used when the channel decides the id (WhatsApp phone, Twilio call SID).
   - `end_session_async` — fires the CRM sink (`on_call_end`).

## The one flow to trace end-to-end

Open all these files side by side and follow one turn through them:

1. Browser sim → `apps/call-simulator/app.js` records mic, POSTs blob to `/voice/stt`.
2. `apps/api/app/routes/voice.py::speech_to_text` → hands to `get_stt().transcribe`.
3. `apps/api/app/providers/stt/groq_stt.py::transcribe` → HTTP to Groq, returns string.
4. Browser sim POSTs transcript to `/chat/turn`.
5. `apps/api/app/routes/chat.py::caller_turn` → `session_manager.run_user_turn`.
6. `session_manager.py::run_user_turn` → `brain.handle_user_turn`.
7. `brain.py::handle_user_turn` → LLM call → maybe tool call → maybe another LLM call → reply text.
8. Back up the stack. Browser sim POSTs reply text to `/voice/tts-base64`.
9. `voice.py::text_to_speech_b64` → `get_tts().synthesize` → returns audio.
10. Browser sim plays the audio.

Every other transport (Vapi, Twilio, WhatsApp, Telegram) is a variation on this same flow with different first/last steps.

## Where to add things

- **New voice** → new file in `providers/tts/`, register in `factory.py`.
- **New LLM** → new file in `providers/llm/`, register in `factory.py`.
- **New CRM** → new sink class in `integrations/sinks.py`, add to `build_sink_from_env`.
- **New channel** (Discord, Slack) → new file in `packages/channels/`, new route file in `apps/api/app/routes/`.
- **New vertical** (dentist, gym) → new JSON in `sample-data/`, new tool file next to `clinic_tools.py` if it needs different tools.

## What's *not* in this repo (yet)

- **Real dashboard** — the browser sim doubles as a debug UI. A proper Next.js session-browser is phase 6.
- **Streaming STT** — every provider is turn-based today. Streaming is what cuts perceived latency from ~1500ms to ~500ms.
- **Cross-channel identity** — a caller who texts on WhatsApp and calls on Twilio is two sessions. See `docs/channels-setup.md` bottom for the plan.
- **Multi-tenant** — one business per instance right now. Multi-tenant is a per-request business-profile lookup + a `business_id` column everywhere.
