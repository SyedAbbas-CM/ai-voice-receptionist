# ElevenLabs setup

ElevenLabs is our default premium TTS. Adapter already exists at `apps/api/app/providers/tts/elevenlabs_tts.py`.

## Get a key

1. Sign up at https://elevenlabs.io (free tier ~10k credits/month, ~10 min of TTS).
2. Profile → **API Keys → Create**.

## Pick a voice

Voice library → pick a voice → copy the voice ID from the sidebar. The default in `.env.example` (`21m00Tcm4TlvDq8ikWAM`) is Rachel — a solid English clinic/business voice.

## Configure

```
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=<voice_id>
ELEVENLABS_MODEL=eleven_turbo_v2_5
TTS_PROVIDER=elevenlabs
```

## Model choice

- `eleven_turbo_v2_5` — 300ms latency, ~50% cheaper than v2, English + top languages. Default.
- `eleven_multilingual_v2` — 29 languages, higher latency, higher quality.
- `eleven_flash_v2_5` — sub-100ms latency, quality trade-off. Good for realtime.

## Cost math

- Free tier: 10k credits / month ≈ 10 minutes of speech. Enough for a few Loom demos.
- Creator plan: $22/mo → 100k credits.
- Pro plan: $99/mo → 500k credits.

Roughly 1 credit ≈ 1 character of text. Assistants speaking ~150 words per call = ~750 characters = ~750 credits per call.

## Streaming

Our adapter uses the non-streaming endpoint (returns MP3 bytes when the audio is complete). For sub-second time-to-first-audio, wire the streaming endpoint next. Not needed for the browser sim; matters for phone-call latency in phase 4/5.

## Voice cloning

The adapter is voice-cloning ready — the `voice` parameter of `synthesize()` accepts any voice ID, including cloned voices from your library. Get client consent before cloning their voice. Never clone a voice you don't have rights to.
