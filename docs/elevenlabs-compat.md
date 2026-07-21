# ElevenLabs-compatible TTS endpoint

Our server exposes ElevenLabs' HTTP API shape at `/v1/*`. Any tool that already speaks 11L can point at our URL and get responses — backed by whatever local model you have configured (Qwen3, Kokoro, Piper) or by real ElevenLabs/OpenAI/Cartesia if you set those.

## Endpoints

| Method | Path | 11L equivalent |
|---|---|---|
| GET | `/v1/voices` | GET `https://api.elevenlabs.io/v1/voices` |
| POST | `/v1/text-to-speech/{voice_id}` | POST `https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` |
| POST | `/v1/text-to-speech/{voice_id}/stream` | POST `https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream` |

## How to point 11L SDK at us

Python `elevenlabs` SDK:
```python
from elevenlabs import ElevenLabs

client = ElevenLabs(
    api_key="whatever",           # ignored unless COMPAT_API_KEY is set
    base_url="http://localhost:8000",
)
audio = client.text_to_speech.convert(
    voice_id="Vivian",            # or an actual 11L voice_id if TTS_PROVIDER=elevenlabs
    text="Hello from the local model",
)
```

Node/Zapier/n8n 11L nodes: change the base URL in the node config (or set `ELEVENLABS_BASE_URL` if the tool honors it).

## Voice mapping

The `voice_id` in the URL is passed through to whatever TTS provider is active:

- `TTS_PROVIDER=qwen3` → voice_id is a Qwen3 speaker name (`Vivian`, `Adam`, `Rina`, `Ryan`, `Cherry`, `Ethan`).
- `TTS_PROVIDER=openai` → voice_id is an OpenAI voice (`alloy`, `echo`, `nova`, ...).
- `TTS_PROVIDER=elevenlabs` → voice_id is a real 11L voice id (e.g. `21m00Tcm4TlvDq8ikWAM`).
- `TTS_PROVIDER=deepgram` → voice_id is a Deepgram voice (e.g. `aura-asteria-en`).

`GET /v1/voices` returns a curated list for the current provider so 11L SDK "list voices" calls work in every mode.

## Auth

- If `COMPAT_API_KEY` is unset (default): open, no auth. Fine for local dev.
- If set: clients must send `xi-api-key: <that value>` header — same header 11L uses.

## What's NOT implemented (yet)

- Server-Sent Events for the streaming endpoint (we buffer + chunk instead — good enough for most SDK consumers, but not truly progressive)
- `/v1/voices/add` (voice cloning creation) — cloning is done via env config for Qwen3 Base models today
- History, usage, subscriptions, dubbing endpoints — not needed for a receptionist

Open an issue or ask if a specific tool needs one of these.
