# Twilio real phone calls

Buy a phone number, receive real calls, run our brain. No Vapi in between.

## How it works

1. Someone dials your Twilio number.
2. Twilio POSTs to `/twilio/voice`. We return TwiML that says "open a Media Stream to `wss://…/twilio/stream`".
3. Twilio opens a WebSocket and streams caller audio as base64 µ-law 8kHz frames.
4. We buffer audio, detect end-of-turn via silence, transcribe via STT.
5. Brain runs, produces reply text.
6. We synthesize with TTS (WAV), downsample to 8kHz µ-law, send back over the same WebSocket as media frames.
7. Caller hears the reply. Loop.

## Setup

1. Sign up at [twilio.com](https://www.twilio.com). Trial account gives you $15+ in credit and 75 free voice minutes.
2. **Phone Numbers → Buy a number** (~$1.15/mo).
3. Expose your local server publicly:
   ```
   cloudflared tunnel --url http://localhost:8000
   ```
4. Configure the number:
   - **Voice Configuration → A call comes in → Webhook**
   - URL: `https://<your-tunnel>/twilio/voice`
   - HTTP: `POST`
5. `.env`:
   ```
   TWILIO_ACCOUNT_SID=AC...        # optional
   TWILIO_AUTH_TOKEN=...           # optional
   TWILIO_PUBLIC_URL=https://<your-tunnel>
   TTS_PROVIDER=qwen3              # MUST emit WAV — see below
   STT_PROVIDER=groq               # or deepgram
   ```
6. `uvicorn app.main:app --port 8000`, then call your Twilio number.

## Provider constraints

- **TTS**: the Twilio route needs WAV output because Python's stdlib `audioop` doesn't decode MP3. Use `qwen3` (local WAV) or `local` (Piper WAV). ElevenLabs and OpenAI TTS return MP3 by default — those need an `ffmpeg`/`pydub` conversion step we haven't added yet. Tell me if you want it and it's a 30-line addition.
- **STT**: any provider works. Groq Whisper Turbo is the fastest (~200ms per utterance).
- **LLM**: any provider works. On a phone call, the caller notices latency more than model IQ — Groq Llama 3.3 70B is a great default.

## Turn detection (the naive version)

Today we use RMS energy + silence timeout. Config in `apps/api/app/routes/twilio.py`:

- `SILENCE_THRESHOLD_RMS = 500` — below this = silence
- `SILENCE_HANG_MS = 700` — how much silence ends a turn
- `MAX_UTTERANCE_MS = 12000` — hard cap so we never buffer forever

If callers complain about the agent cutting them off, raise `SILENCE_HANG_MS` to 900-1200. If it feels slow to respond, lower it.

Phase 5 will replace this with Silero VAD (a tiny ML model that's ~10× more accurate at end-of-turn detection). This works fine for MVP demos.

## Barge-in

Not implemented yet. If the caller talks while the agent is speaking, we drop their audio. Phase 5 adds proper barge-in with a "stop speaking mid-frame" hook.

## Cost math

Per 3-minute call:
- Twilio inbound US voice: $0.0085/min → ~$0.026
- STT (Groq Whisper Turbo): free tier or ~$0.01 at scale
- LLM (Groq 70B): free tier or ~$0.005
- TTS (Qwen3 local): $0 electricity
- **Total: ~$0.03 per call.** vs ~$0.15-0.30 through Vapi.

## Troubleshooting

- **"This number is not configured"** → you didn't set `TWILIO_PUBLIC_URL`.
- **WebSocket closes immediately** → your tunnel isn't serving `wss://`. Check that the URL you set as `TWILIO_PUBLIC_URL` actually returns 200 on `/twilio/voice`.
- **Caller hears silence** → your TTS provider isn't emitting WAV. Set `TTS_PROVIDER=qwen3` or `local`.
- **Caller hears garbled audio** → sample rate mismatch. The µ-law conversion assumes your TTS is 24kHz or 44.1kHz mono. If you're using an exotic TTS, check `_tts_bytes_to_mulaw` in `routes/twilio.py`.
- **Agent doesn't respond** → the silence detector didn't fire. Speak louder or raise `SILENCE_THRESHOLD_RMS`. Check server logs for `twilio ... heard: <transcript>`.
