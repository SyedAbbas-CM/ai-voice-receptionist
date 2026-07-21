# voiceops-ai-agent

AI receptionist starter kit for local businesses. Inbound calls, appointment booking, lead qualification, FAQs, SMS/email follow-up, CRM logging.

Provider-swappable: every cloud piece (STT, LLM, TTS, transport) can be replaced by a local/free equivalent during testing via `.env`.

## Modes

- **Demo/free**: browser mic, local STT/TTS/LLM, fake calendar
- **Production**: Twilio/LiveKit/Vapi + OpenAI/Claude/Gemini + ElevenLabs/Deepgram/Cartesia + Google Calendar/Sheets/CRM

## Quick start

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Then open `apps/call-simulator/index.html` in a browser.

## Architecture

```
Phone/browser -> transport -> VAD -> STT -> LLM -> tools -> TTS -> caller
                                            |
                                            v
                                       logs/dashboard
```

See `docs/architecture.md` for details.

## Current phase

Phases 1-3 + channels + compat layer (of 5):

- [x] Tool-calling receptionist brain (text-first)
- [x] Browser mic call simulator
- [x] Provider adapter interfaces (STT/LLM/TTS/transport)
- [x] SQLite session/transcript/booking storage
- [x] Clinic vertical sample
- [x] Vapi custom-LLM webhook + assistant provisioning script (works for Vapi + Retell + Bland — same OpenAI-compat shape)
- [x] GoHighLevel client (contacts, notes, opportunities, calendar)
- [x] Google Calendar + Sheets adapters
- [x] Pluggable CRM sink layer (`none | ghl | sheets | ghl+sheets`)
- [x] ElevenLabs TTS (real)
- [x] Qwen3-TTS local adapter (Apache 2.0, preset + voice cloning)
- [x] ElevenLabs-compatible endpoints (`/v1/text-to-speech/*`, `/v1/voices`) — 11L SDKs point at us
- [x] WhatsApp Business Cloud API channel (text + voice notes)
- [x] Telegram bot channel (text + voice notes)
- [x] Shared voice-message pipeline (STT → brain → TTS → channel)
- [ ] Twilio + OpenAI Realtime branch (phase 4)
- [ ] LiveKit/Pipecat self-hosted branch (phase 5)
- [ ] Cross-channel identity linking (phone unification)

## New to voice AI?

Start with [`docs/learning/00-start-here.md`](docs/learning/00-start-here.md). Four short files: what a voice agent is, glossary, how this repo maps to the concepts, and a curated reading list.

## First demos

- **Inbound (15 min, $0)**: [`docs/runbooks/telegram-first-demo.md`](docs/runbooks/telegram-first-demo.md) — Telegram voice-note receptionist on Groq free tier.
- **Outbound cold-caller (Vapi + Google Sheets)**: [`docs/runbooks/subtodealz-outbound-demo.md`](docs/runbooks/subtodealz-outbound-demo.md) — the SubtoDealz-style real-estate wholesaler dialer. Replaces an entire n8n workflow with one FastAPI endpoint.
- **Outbound cold-caller on a local voice model (Qwen3-TTS)**: [`docs/runbooks/local-voice-outbound.md`](docs/runbooks/local-voice-outbound.md) — flip `transport: "local"` and the same endpoint runs the conversation on-device with cloned voice. No Vapi account, no phone number, no per-minute charges.

## Products / verticals shipped

- **Clinic** — inbound appointment booking (`sample-data/clinic/business.json`)
- **Restaurant** — inbound reservations with party size + dietary notes (`sample-data/restaurant/business.json`)
- **Real estate** — inbound lead qualification + viewing bookings + lead scoring (`sample-data/real-estate/business.json`)
- **Wholesaler outbound** — outbound seller-financing pitch to landlords, GPT-4.1 disposition, sheet writeback (`sample-data/subtodealz/business.json`) — **Product A** in the current build

Change `BUSINESS_PROFILE_PATH` in `.env` to switch the receptionist's vertical. The brain automatically loads the right tools.

## Archived n8n workflows (reference)

Four original n8n workflows are preserved in [`workflows/n8n/`](workflows/n8n/) with all hardcoded IDs scrubbed:

- `subtodealz-outbound.json` — the original SubtoDealz outbound dialer (being ported to Product A)
- `ironclad-post-call-router.json` — IronClad Family multi-channel post-call CRM fan-out (patterns being stolen for Product B)
- `vivarays-notion-ingestion.json` + `vivarays-content-generator.json` — VivaRays coaching RAG + brand-voice content engine (patterns being stolen for Product C)

See [`workflows/n8n/README.md`](workflows/n8n/README.md) for what each does + the mapping from n8n nodes → Python modules.

## Integration guides

- [Vapi setup](docs/vapi-setup.md) — telephony via Vapi custom-LLM
- [Twilio setup](docs/twilio-setup.md) — real phone calls direct via Media Streams (no Vapi)
- [GoHighLevel setup](docs/ghl-setup.md) — CRM sink + calendar
- [Google setup](docs/google-setup.md) — Calendar backend + Sheets logging
- [ElevenLabs setup](docs/elevenlabs-setup.md) — premium TTS
- [Qwen3-TTS setup](docs/qwen3-tts-setup.md) — local open-weights TTS with voice cloning
- [ElevenLabs-compatible API](docs/elevenlabs-compat.md) — point any 11L SDK at our server
- [Channels: WhatsApp + Telegram](docs/channels-setup.md) — voice-note conversations
- [Provider matrix](docs/provider-matrix.md) — pick a stack per client
- [Demo script](docs/demo-script.md) — the Loom recording playbook

## Verticals

- Clinic: appointment booking with calendar check
- Restaurant: reservations (template scaffolded)
- Real estate: lead qualification (template scaffolded)
