# Vapi setup

We use Vapi's **custom-LLM** mode: Vapi handles telephony, STT (Deepgram), TTS (ElevenLabs), and the caller connection. Every LLM turn is POSTed to our `/vapi/chat/completions` endpoint. Our receptionist brain runs the logic and returns the reply in OpenAI shape.

## Why custom-LLM (not tools-only)

- We own the state machine, tool routing, extraction, escalation logic.
- Same brain works for browser sim, Twilio, LiveKit — no code duplication.
- Provider-swap for the LLM stays a one-line env change; Vapi never has to know.

## Setup

1. Sign up at https://dashboard.vapi.ai (starting credit ~$10).
2. Copy your **Private key** from the API keys tab.
3. Expose your local backend publicly:
   - Cloudflare Tunnel: `cloudflared tunnel --url http://localhost:8000`
   - or ngrok: `ngrok http 8000`
4. Fill in `.env`:
   ```
   VAPI_PRIVATE_KEY=vapi_pk_...
   VAPI_PUBLIC_URL=https://<your-tunnel>.trycloudflare.com
   VAPI_SECRET=<any-long-random-string>
   ```
5. Create the assistant:
   ```
   python scripts/create_vapi_assistant.py --create
   ```
   Copy the returned `id` — that's your assistant id.
6. In the Vapi dashboard, buy a phone number and assign it to that assistant.
7. Call the number. The brain runs from your backend.

## Update flow

When you edit `sample-data/clinic/business.json` or the system prompt:
```
python scripts/create_vapi_assistant.py --update <assistant_id>
```

## Endpoints exposed to Vapi

- `POST /vapi/chat/completions` — OpenAI-compatible custom-LLM endpoint. Vapi calls this on every caller turn.
- `POST /vapi/events` — Vapi's `serverUrl` webhook. Receives call lifecycle events. We use `end-of-call-report` to finalize the session and fire CRM sinks.

## Auth

If you set `VAPI_SECRET`, we check `Authorization: Bearer <secret>` on every incoming Vapi request. The provisioning script sends the same secret via `model.headers` (custom-LLM) and `serverUrlSecret` (events).

## Cost math

Per 3-min call, roughly:
- Vapi platform fee: ~$0.15
- Deepgram STT: bundled
- ElevenLabs TTS: bundled (or ~$0.04 if you route to your own key)
- Custom LLM (billed by you): ~$0.005 on `gpt-4o-mini`, ~free on Groq

Total: ~$0.15–0.30 per call including telephony.
