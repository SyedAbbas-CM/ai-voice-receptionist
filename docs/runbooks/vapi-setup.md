# Vapi setup — get a real phone number ringing in 45 minutes

You have no phone number today. This runbook takes you from "no Vapi account" to "your own cellphone rings and hears the AI receptionist" so you can record a Loom demo.

**Cost**: $0 today. Vapi gives $10 signup credit which covers your first ~100 minutes of testing plus one US number for ~1 month.

**End state**: your cellphone rings from a US number you bought, the AI says "Hi, thanks for calling [Business]. How can I help you today?", you say something back, it books an appointment, you hang up.

---

## Prereqs (already true if you followed earlier runbooks)

- Repo cloned at `/Users/az/Desktop/Receptionist Agent`
- `.venv` created with `pip install -r apps/api/requirements.txt`
- 278 tests pass: `cd apps/api && python -m pytest tests/ -q`
- Your `.env` already has `NVIDIA_API_KEY`, `GROQ_API_KEY`, `ELEVENLABS_API_KEY` set
- Your cellphone number handy for the test call

---

## Step 1 — Sign up Vapi (5 min, $0)

1. Open [dashboard.vapi.ai](https://dashboard.vapi.ai) → **Sign up**
2. Use Google login if you have one — fastest
3. On the dashboard home you should see **"$10 credit added"** at the top

If you see a "verify email" popup, do it. Vapi won't let you buy a number until email is verified.

---

## Step 2 — Grab your API key (2 min)

1. Left sidebar → **API Keys**
2. There will be a "Private" and a "Public" key
3. **Copy the Private key**. Looks like `vapi_pk_...` or a UUID like `c42e779f-...`

**Save it somewhere safe** — you won't see it again after leaving the page.

---

## Step 3 — Buy a phone number (5 min, uses ~$1.15 of your credit)

1. Left sidebar → **Phone Numbers** → **Buy Number**
2. Country: **United States**. Any area code — doesn't matter for a demo.
3. Confirm purchase. Charges ~$1.15/mo from your $10 credit.
4. Once bought, click the number to see its detail page
5. **Copy the `phoneNumberId`** — it's a UUID at the top of the detail page. Save it.

**Do NOT set a Voice Configuration webhook yet** — we'll do that in Step 7.

---

## Step 4 — Create the AI assistant (10 min)

1. Left sidebar → **Assistants** → **Create Assistant**
2. Name it "SubtoDealz Test" or "Riverside Clinic Test" — anything memorable
3. **Model tab**:
   - **Provider**: OpenAI (Vapi uses this to route the LLM call, but we'll override with our custom-LLM webhook in a sec)
   - **Model**: `gpt-4o-mini` (any model works; this is a placeholder)
   - **First message**: Leave the default for now
   - **System prompt**: leave empty (custom-LLM webhook provides its own)
4. **Transcriber tab**:
   - **Provider**: `deepgram`
   - **Model**: `nova-3`
   - **Language**: `en-US`
5. **Voice tab**:
   - **Provider**: `11labs`
   - **Voice ID**: `21m00Tcm4TlvDq8ikWAM` (Rachel — the default receptionist voice)
   - **Model**: `eleven_turbo_v2_5`
   - Uses Vapi's own ElevenLabs credit — don't need to add your key here
6. **Advanced tab** → scroll to **Server URL** → **leave empty for now** (Step 7)
7. Click **Create**
8. On the assistant detail page, **copy the assistant ID** (UUID at the top). Save it.

---

## Step 5 — Update your `.env` (2 min)

Open `.env` in the repo root and add or update these lines:

```bash
LLM_PROVIDER=nvidia
NVIDIA_MODEL=meta/llama-3.1-70b-instruct
STT_PROVIDER=local
TTS_PROVIDER=qwen3
OUTBOUND_TRANSPORT=vapi
BUSINESS_PROFILE_PATH=/Users/az/Desktop/Receptionist Agent/sample-data/clinic/business.json

VAPI_PRIVATE_KEY=<paste from Step 2>
VAPI_ASSISTANT_ID=<paste from Step 4>
VAPI_PHONE_NUMBER_ID=<paste from Step 3>
VAPI_SECRET=my-random-webhook-secret-1234abcd
```

- `LLM_PROVIDER=nvidia` — because we've tested Llama 3.1 70B works with tool-calling on your key
- `BUSINESS_PROFILE_PATH` — pointing at clinic for the Loom because it's the most universal demo (any business gets it in 2 seconds)
- `VAPI_SECRET` — invent any random string. Vapi will send this back in every webhook so we know it's really Vapi calling.

---

## Step 6 — Expose your local server publicly (5 min, $0)

Vapi needs to reach your laptop over the internet. Two options:

**Option A: Cloudflare Tunnel** (recommended — free, no signup, no rate limits)

```bash
# Install if you don't have it
brew install cloudflared

# Start a tunnel (leave this terminal open forever during the demo)
cloudflared tunnel --url http://localhost:8001
```

Watch the output. After 10-20 seconds you'll see:

```
Your quick Tunnel has been created! Visit it at:
https://random-words-here.trycloudflare.com
```

**Copy that URL.** That's your public backend URL.

**Option B: ngrok** (if you already have it)

```bash
ngrok http 8001
```

Same idea — copy the `https://xxxxx.ngrok.io` URL.

---

## Step 7 — Point Vapi at your tunnel (3 min)

Back in the Vapi dashboard:

1. Left sidebar → **Assistants** → click your assistant
2. **Advanced tab** → **Server URL**:
   ```
   https://your-tunnel-url.trycloudflare.com/vapi/events
   ```
3. **Server URL Secret** — paste the same `VAPI_SECRET` string you put in `.env`
4. Click **Save**

Then for the **custom LLM** (so our brain answers instead of OpenAI):

5. Back to the assistant → **Model tab**
6. Scroll down to **Provider** → change from OpenAI to **Custom LLM**
7. **Custom LLM URL**:
   ```
   https://your-tunnel-url.trycloudflare.com/vapi/chat/completions
   ```
8. **Authorization header**: `Bearer <YOUR_VAPI_SECRET>` (same secret)
9. Click **Save**

---

## Step 8 — Boot your server (2 min)

New terminal window:

```bash
cd "/Users/az/Desktop/Receptionist Agent"
source .venv/bin/activate
cd apps/api
uvicorn app.main:app --port 8001 --reload
```

Wait for `Uvicorn running on http://0.0.0.0:8001`. Verify from another terminal:

```bash
curl http://localhost:8001/health
```

Expected: `{"ok":true,"llm":"nvidia","stt":"local","tts":"qwen3"}`

Also verify the tunnel works — hit the tunnel URL directly:

```bash
curl https://your-tunnel-url.trycloudflare.com/health
```

If both return the health JSON, you're good.

---

## Step 9 — Ring your own phone (2 min)

**Option A: Inbound test** (recommended for the Loom)

1. Take out your cellphone
2. Dial the Vapi number you bought in Step 3
3. Wait 2-3 rings — you should hear "Hi, thanks for calling Riverside Family Clinic. How can I help you today?"
4. Say: "I'd like to book an appointment for back pain tomorrow at 10am. My name is [YOUR_NAME], my number is 555-1234."
5. The AI should confirm, check availability, book it, and say goodbye

**Option B: Outbound test** (uses the /outbound endpoint)

From another terminal while server is running:

```bash
curl -X POST http://localhost:8001/outbound/start_batch \
  -H "Content-Type: application/json" \
  -d '{
    "transport": "vapi",
    "leads": [{"phone": "+1YOURCELLPHONE", "name": "Test", "property_address": "123 Test St", "rent_amount": "1500"}]
  }'
```

Your cellphone rings from the Vapi number within 5-10 seconds.

---

## Step 10 — Record the Loom (10 min)

1. Open [loom.com](https://loom.com) → **New Recording**
2. Screen + camera + mic. Set to screen-only for the first take.
3. Layout your screen:
   - Left half: **the sample-data/clinic/business.json** file in your editor (shows the config)
   - Right half: your cellphone (angled so the camera can see the screen)
4. **Start recording**. Read this ~30-word intro on-camera:

   > "This is an AI receptionist for a clinic. It answers real calls,
   > checks the calendar, books appointments. Same code works for
   > outbound cold calling too. Here's it live:"

5. Show your cellphone screen dialing the Vapi number
6. Turn on speaker
7. Let the call play out — 30-45 seconds is plenty
8. Show the SQLite booking that got written:
   ```bash
   sqlite3 apps/api/data/voiceops.db "SELECT * FROM bookings ORDER BY id DESC LIMIT 1;"
   ```
9. End the Loom with:
   > "3-5 days to your specific calendar and CRM. Send me a message
   > and I'll get you a quote."

10. Stop recording. Copy the Loom URL.

**Total Loom length: 60-90 seconds max.** Any longer and clients tune out.

---

## Step 11 — Post the Loom to Upwork (20 min)

1. Open [upwork.com](https://www.upwork.com/nx/jobs/search/) → search:
   - "AI receptionist"
   - "AI phone answering"
   - "Vapi assistant"
   - "AI cold caller"
2. Filter to **Posted last 24 hours** and budget **$500+**
3. Open `docs/proposal-snippets.md` and pick Template 1, 2, or 3 based on the job
4. Paste, replace `[LOOM_URL]` with your Loom, replace `[YOUR_NAME]`, replace `[CLIENT_NAME]` with the client's first name
5. Send to 5 jobs today

Expected reply rate: **10-30%** if the Loom is good. Below 10% means your Loom is showing too much code or explaining too much. Rewatch it and tighten.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Vapi assistant rings but hangs up immediately | Server URL not set or tunnel down | Verify tunnel URL still active. Cloudflared tunnels expire when the terminal closes. |
| Call answers but AI is silent | Model tab still on OpenAI, not Custom LLM | Step 7.6 — switch provider to Custom LLM. |
| AI answers but wrong voice / wrong business | `BUSINESS_PROFILE_PATH` in `.env` wrong | Point at the clinic JSON. Restart server. |
| Call cost too much | `TTS_PROVIDER=qwen3` slow → Vapi times out | For real phone calls, switch to `TTS_PROVIDER=elevenlabs`. Qwen3 on M1 Pro can't keep up with a live phone call. |
| Webhook returns 401 | `VAPI_SECRET` mismatch between `.env` and dashboard | Rewrite both to the same value. |
| Sheet writeback doesn't happen | `CRM_SINK=` unset or Google service account missing | For the Loom, use SQLite (already working). Sheets is Product B territory. |

---

## What breaks if you don't want to spend the $10

You can't. There's no Vapi free tier that gives you a real phone number. Your options if you refuse to spend anything:

1. **Telegram voice-note demo** (0 dollars, no phone) — follow `docs/runbooks/telegram-first-demo.md` instead. Less impressive on Upwork but real users.
2. **Browser sim demo** — no phone, just mic-in-browser. Cheapest to record but every voice-agent freelancer on Upwork already has one. Lowest signal.

**Honest take**: $10 for a real phone number that rings is the single highest-ROI dollar you'll spend on this repo. Do it.

---

## What's next after the first Upwork bite

When a client replies to your Loom:

1. **Don't rebuild anything.** Ask them 2 questions: what calendar / CRM they use, and what's the hardest 2 minutes of their day.
2. **Quote from the proposal template.** $2,000-3,500 for inbound receptionist. $3,000-5,000 for outbound dialer. Don't discount on the first client — they're your reference customer, they're paying for your reputation to exist.
3. **Timeline**: 5 business days to a working demo on their number. Don't quote 3 days unless you know their calendar API cold.
4. **Contract**: 50% upfront, 50% on delivery. Upwork Escrow handles this.

Then just swap the `BUSINESS_PROFILE_PATH` JSON for theirs, adjust the CRM sink, and ship it.
