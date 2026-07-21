# Product A on a local voice model — no Vapi, no phone bill

This runbook shows how to run the SubtoDealz outbound dialer using a **fully local voice stack** — Qwen3-TTS voice cloning for the assistant's voice, Groq (free) or your local LLM for the brain. No Vapi account, no Twilio number, no phone bill.

**What you get out of this demo**: the same `/outbound/start_batch` endpoint, same disposition logic, same Google Sheet writeback. But instead of a real phone call, the emulator runs a scripted conversation on your machine and saves the full audio + transcript. Perfect for Upwork demos where you want to show a client "here's your voice, here's your script, here's the sheet updating" without paying per-minute.

**Time to first working call**: ~10 minutes if Qwen3-TTS is already downloaded, ~20 if not.

## The one env change

```bash
# .env
OUTBOUND_TRANSPORT=local            # was 'vapi'

LLM_PROVIDER=groq                   # any LLM works
GROQ_API_KEY=gsk_...

TTS_PROVIDER=qwen3
QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-1.7B-Base
QWEN3_TTS_DEVICE=auto               # picks cuda / mps / cpu

# Optional — clone a specific voice. Default: Qwen's public sample clip.
# QWEN3_TTS_REF_AUDIO=/path/to/my_voice_sample.wav
# QWEN3_TTS_REF_TEXT=Hi, this is a sample of the voice I want to clone.

BUSINESS_PROFILE_PATH=./sample-data/subtodealz/business.json
```

That's it. The transport switch is `local`, the TTS is Qwen3, and the LLM is whatever you already had configured.

The Google Sheet writeback still works — you can either point at a real Google Sheet (same as the Vapi path) or leave `GOOGLE_SERVICE_ACCOUNT_JSON` unset, in which case the disposition handler logs the classification and skips the write.

## Two-minute setup

```bash
# 1. Ensure Qwen3-TTS is installed (~4-8 GB download first time)
source .venv/bin/activate
pip install qwen-tts torch soundfile
python scripts/test_qwen3_tts_clone.py    # smoke test: clone-synthesize one WAV

# 2. Configure .env (see above)

# 3. Boot the server
cd apps/api
uvicorn app.main:app --reload --port 8000
```

## Run it

```bash
# Preview which leads would be dialed (no calls, no synthesis)
curl -s -X POST http://localhost:8000/outbound/dry_run \
  -H 'Content-Type: application/json' \
  -d '{
    "transport": "local",
    "business_id": "demo-subtodealz-001",
    "max_calls_per_batch": 1
  }' | python -m json.tool

# Actually run the emulated call
curl -s -X POST http://localhost:8000/outbound/start_batch \
  -H 'Content-Type: application/json' \
  -d '{
    "transport": "local",
    "business_id": "demo-subtodealz-001",
    "max_calls_per_batch": 1
  }' | python -m json.tool
```

The response returns immediately with a `local_<hex>` call ID. The call runs in the background — LLM decides what to say, Qwen3-TTS synthesizes each line, files are saved to disk. When the brain calls `capture_disposition`, the "call" ends and an event is POSTed to `/vapi/events` — the same handler that runs classification + Sheet writeback for the Vapi path.

## What's saved

```
data/local_calls/<call_id>/
├── transcript.jsonl           # one JSON line per turn (role, text, timestamp)
├── turn_000_assistant.wav     # greeting audio
├── turn_001_assistant.wav     # reply to caller line 1
├── turn_002_assistant.wav     # reply to caller line 2
└── ...
```

Play any WAV to hear the cloned voice. Concatenate them for the Loom demo:

```bash
# Example: stitch the assistant turns together for a demo reel
cd data/local_calls/local_xxxx
ffmpeg -f concat -safe 0 \
  -i <(for f in turn_*_assistant.wav; do echo "file '$PWD/$f'"; done) \
  -c copy full_call.wav
```

## Same interface as Vapi

The switch is one field in the request body. This works today:

| Body field | Vapi path | Local path |
|---|---|---|
| `transport` | `"vapi"` (default) | `"local"` |
| `business_id` | same | same |
| `sheet_id` | same | same |
| `assistant_id` | required (env or body) | ignored |
| `phone_number_id` | required (env or body) | ignored |
| all dialer policy fields | same | same |
| `max_calls_per_batch` | same | same |

The `DispatchOutcome` returned in `dispatched[*]` also matches — every consumer of the API (dashboard, tests, curl scripts) is transport-agnostic.

## What the local path is NOT

Read this so nobody's surprised:

- **Not a real phone call.** The emulator scripts the caller's responses. It's a demo of the voice + brain + classification pipeline, not a live conversation with a stranger. A real self-hosted phone path needs Twilio Media Streams — see `docs/twilio-setup.md`.
- **Not fast on Mac.** Qwen3-TTS on Apple Silicon MPS runs at ~40s/sentence for the 1.7B model. Good for demos, unusable for real-time. On a 3090, it drops to ~1-2s.
- **Not free.** Just cheap. Groq LLM is free-tier, Qwen3-TTS is your electricity. If a client wants unlimited scale, Groq's free tier will rate-limit — swap to their paid tier or OpenAI.

## When to use which transport

| Situation | Transport |
|---|---|
| Upwork Loom demo where you want the client's own voice | `local` |
| Real dial to a real phone | `vapi` |
| Client wants privacy, no data to third parties | `local` (or Vapi + on-prem LLM) |
| High-volume production, >50k min/mo | Self-hosted LiveKit + Twilio SIP (Product A2, coming later) |
| Fast iteration with zero external accounts | `local` |

## Cloning a specific voice

1. Record a 3-30 second sample of the voice you want to clone. WAV is safest.
2. Transcribe it exactly (what the speaker says, verbatim). 3-30s → 5-40 words.
3. Set both env vars:
   ```
   QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-1.7B-Base
   QWEN3_TTS_REF_AUDIO=/absolute/path/to/sample.wav
   QWEN3_TTS_REF_TEXT=The exact transcript of that sample.
   ```
4. Restart uvicorn. All calls now use the cloned voice.

**Legal:** never clone a voice you don't have written permission for. For client demos, use your own voice or the public Qwen demo sample.

## Comparing on the same call

Want to compare Vapi vs local side by side? Run the same batch twice with just `transport` changed. Both write to the same sheet — you'll see the two rows and can compare voice quality, latency, and classification agreement.
