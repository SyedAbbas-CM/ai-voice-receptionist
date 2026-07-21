# Demo script — clinic booking

For Loom recordings and Upwork pitch videos.

## Setup (off-camera)

1. `cp .env.example .env` and fill in `GROQ_API_KEY` (free tier is enough).
2. Set `LLM_PROVIDER=groq`, `STT_PROVIDER=groq`, `TTS_PROVIDER=browser`.
3. `cd apps/api && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000`.
4. Open `http://localhost:8000/`.

## On-camera

**0:00–0:15** — Show the simulator. Point at the config bar: "this is running on Groq's free tier — Llama 3.3 70B for reasoning, Whisper Turbo for transcription, browser speech for TTS. Zero dollars."

**0:15–0:25** — Click *Start call*. The receptionist greets: *"Hi, thanks for calling Riverside Family Clinic. How can I help you today?"*

**0:25–1:10** — Voice flow 1: appointment.
- Hold *Hold to talk*: "I need an appointment for back pain tomorrow morning."
- Agent asks for name and phone.
- Voice: "John Carter, five five five oh one oh four four three two."
- Agent checks availability via tool call (visible in right panel).
- Voice: "Ten thirty works."
- Agent books — booking result appears in the *Last tool calls* panel.

**1:10–1:30** — Point at the *Extracted* panel: structured JSON with `caller_name`, `phone`, `intent: book_appointment`, `lead_score`. "This is what you'd POST to a CRM, a Google Sheet, or n8n."

**1:30–1:45** — Voice flow 2: FAQ. "Do you take Aetna insurance?" Agent answers verbatim from the business profile.

**1:45–2:00** — Voice flow 3: emergency. "I'm having chest pain." Agent immediately tells caller to call 911 and escalates. *Status* panel shows `escalated: true`.

**2:00–2:15** — Flip `LLM_PROVIDER=openai` in `.env`, restart, redo one turn. Same flow, different brain, no code changed. Same for TTS: flip to `elevenlabs`, demonstrate.

**2:15** — Close: "Same architecture handles Twilio phone numbers, Vapi, LiveKit, GoHighLevel, Google Calendar. Tell me your stack and I'll wire it in."

## Verticals to record next

- Restaurant reservation (`sample-data/restaurant/business.json` — todo)
- Real estate lead qualification (`sample-data/real-estate/business.json` — todo)
