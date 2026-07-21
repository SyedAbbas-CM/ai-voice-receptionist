# Run right now — no Vapi, no Twilio, no Google Sheet

You have LLM keys (Groq / Cerebras / OpenRouter / Mistral / LLM7 / GOOGLE_API_KEY) and Qwen3-TTS downloaded. That is enough to demo Product A end-to-end **on your machine, in five minutes, spending nothing**.

What this runbook produces:
- Cloned voice speaking real sentences (WAV files you can play)
- Full "call" transcripts (JSONL)
- The lead-classifier + rent-extractor running against the transcript
- All the same infrastructure that will drive a real Vapi phone call the day you point at Vapi

## Step 1 — pick your LLM

You have several. Cerebras is fastest, Groq is well-tested here, OpenRouter is the most flexible.

Pick ONE of these blocks to set in `.env`:

```bash
# Option A: Cerebras (2000 tok/s, free ~1M tok/day)
LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=csk_...            # yours
CEREBRAS_MODEL=llama-3.3-70b

# Option B: Groq (already tested, free tier)
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile

# Option C: OpenRouter (any model, pay-per-token)
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct
```

## Step 2 — voice + demo mode

Add these lines to `.env` regardless of which LLM you picked:

```bash
# Local Qwen3-TTS with voice cloning (uses Qwen's public demo voice by default)
TTS_PROVIDER=qwen3
QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-1.7B-Base
QWEN3_TTS_DEVICE=auto

# Wholesaler vertical + local emulator mode
BUSINESS_PROFILE_PATH=./sample-data/subtodealz/business.json
OUTBOUND_TRANSPORT=local

# STT provider is set to `groq` in .env.example; that's fine (STT isn't
# used by the local emulator — the caller's turns are scripted). Leave it.
```

**No Vapi env vars needed.** No Twilio. No Google service account. No sheet.

## Step 3 — boot

```bash
cd "/Users/az/Desktop/Receptionist Agent"
source .venv/bin/activate
cd apps/api
uvicorn app.main:app --port 8000 --reload
```

Verify:
```bash
curl -s http://localhost:8000/health
# → {"ok":true,"llm":"cerebras","stt":"groq","tts":"qwen3"}
```

## Step 4 — fire a call

**One-shot demo (no Google Sheet):**
```bash
curl -s -X POST http://localhost:8000/outbound/start_batch \
  -H 'Content-Type: application/json' \
  -d '{
    "transport": "local",
    "business_id": "demo-subtodealz-001",
    "max_calls_per_batch": 1,
    "sheet_id": "SKIP"
  }' 2>&1 | head -30
```

This will error because `sheet_id: "SKIP"` isn't a real sheet. **That's fine** — the error is in the sheet read step. The emulator runs downstream.

**Proper zero-external demo:** call the emulator directly through a small Python script:

```bash
cd "/Users/az/Desktop/Receptionist Agent"
source .venv/bin/activate
python -c "
import asyncio, json, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'apps/api')
from packages.integrations.local_voice_orchestrator import LocalVoiceOrchestrator
from packages.schemas import BusinessProfile

biz = BusinessProfile(**json.load(open('sample-data/subtodealz/business.json')))
orch = LocalVoiceOrchestrator(business=biz)

async def go():
    r = await orch.dispatch_call(
        assistant_id='local',
        phone_number_id='local',
        customer_number='+15551234567',
        variable_values={'lead_name': 'Bob', 'property_address': '123 Elm St', 'rent_amount': '1500'},
    )
    print('call started:', r.id)
    # Give it a minute to run — Qwen3 on Mac is slow
    for i in range(120):
        await asyncio.sleep(1)
        from pathlib import Path
        transcript = Path('data') / 'local_calls' / r.id / 'transcript.jsonl'
        if transcript.exists() and b'capture_disposition' in transcript.read_bytes():
            print('call finished after', i, 'seconds')
            break
    print('output at data/local_calls/' + r.id)

asyncio.run(go())
"
```

## Step 5 — hear it

```bash
# Play the greeting
afplay data/local_calls/local_*/turn_000_assistant.wav

# Or open the whole folder in Finder
open data/local_calls/local_*/

# Or concatenate all assistant turns into one WAV
ls data/local_calls/local_*/turn_*_assistant.wav | \
  awk '{print "file \x27" $0 "\x27"}' > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy full_call.wav
open full_call.wav
```

## What just happened

1. The emulator built the wholesaler "Alex from SubtoDealz" system prompt with your lead's variables substituted (`{{lead_name}}` → Bob, etc).
2. The brain (Cerebras / Groq / OpenRouter LLM) generated the greeting.
3. Qwen3-TTS 1.7B Base cloned Qwen's demo voice reading the greeting → saved to `turn_000_assistant.wav`.
4. A scripted "caller" said their first line (from `DEFAULT_CALLER_SCRIPT` in `local_voice_orchestrator.py`).
5. The brain replied. Loop repeats up to 4 times.
6. When the brain calls the `capture_disposition` tool ("this lead is CALLBACK_REQUESTED"), the call ends.
7. An `end-of-call-report` event POSTs to `/vapi/events` — the same handler that would run on a real Vapi call.
8. If a Google Sheet was configured, the disposition writes back. Since it isn't, it logs and moves on.

## Time budget on your Mac

- **First run: ~10 min** because the Qwen3 model has to load once (~15s from cache, ~600s cold on first-ever run).
- **Per synthesized sentence: ~40s** on M1 MPS float32. A 5-turn "call" takes ~4 minutes to fully render.
- **On a 3090: ~1-2s per sentence** — a 5-turn call finishes in ~15 seconds.

## Cloning your own voice (2 min)

1. Record any WAV of you speaking (3-30 seconds). QuickTime → File → New Audio Recording → export as WAV.
2. Transcribe it exactly.
3. Set in `.env`:
   ```
   QWEN3_TTS_REF_AUDIO=/absolute/path/to/your.wav
   QWEN3_TTS_REF_TEXT=The exact transcript of what you said.
   ```
4. Restart uvicorn.

Now every call uses your voice.

## Adding a Google Sheet (10 min, optional)

If you want the disposition to actually write back to a spreadsheet:

1. Create a service account (console.cloud.google.com → IAM → Service Accounts → Create → key JSON)
2. Enable Google Sheets API in the same project
3. Share your Sheet with the service account email as Editor
4. Set in `.env`:
   ```
   GOOGLE_SERVICE_ACCOUNT_JSON=/absolute/path/to/sa.json
   GOOGLE_SHEET_ID=<from sheet URL>
   ```
5. Sheet needs these columns in row 1:
   `Name | Phone | Property address | Rent Amount | Total Calls | Status | Last Called | Notes`

Then run `/outbound/start_batch` without `sheet_id: "SKIP"`.

## Ready for real phone calls?

When you have a Vapi account and want to dial real numbers, switch two lines in `.env`:

```bash
OUTBOUND_TRANSPORT=vapi
VAPI_PRIVATE_KEY=...
VAPI_ASSISTANT_ID=...
VAPI_PHONE_NUMBER_ID=...
```

Same endpoint, same body, same disposition logic. See `docs/runbooks/subtodealz-outbound-demo.md`.
