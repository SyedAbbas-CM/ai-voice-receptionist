# Telegram voice bot — 15-minute demo runbook

Zero dollars. No credit card. Copy-paste each command in order. If a step breaks, stop and check the "if this breaks" note below the step.

**End state**: you DM your Telegram bot a voice note like *"Book me a back pain consult tomorrow at 10am, my name is John Carter, phone 555-1234"* and it voice-notes back a booking confirmation.

## Prerequisites (one-time, ~2 min)

- Telegram account
- macOS Terminal or Linux shell
- Repo already cloned to `/Users/az/Desktop/Receptionist Agent`
- Python venv already created (you have `.venv` from earlier steps)

## Step 1 — get a free LLM + STT key (~3 min)

Groq covers both. Free forever, no card required.

1. Open [console.groq.com/keys](https://console.groq.com/keys) → Sign up (Google/GitHub login is fastest).
2. Click **Create API Key** → name it `voiceops` → copy the value (starts with `gsk_`).

**Save it. Groq only shows the key once.**

## Step 2 — create your Telegram bot (~2 min)

1. Open Telegram → search **@BotFather** → start chat.
2. Send `/newbot`.
3. When asked for a name: `Riverside Clinic Test`
4. When asked for a username (must end in `bot`): `riverside_clinic_test_bot` (or any available name).
5. BotFather sends you a token like `123456789:AAxxxxxxxxxxx`. **Copy it.**

**If username is taken**: try adding numbers or use your initials. Doesn't matter — nobody sees it but you.

## Step 3 — fill in `.env` (~1 min)

```bash
cd "/Users/az/Desktop/Receptionist Agent"
cp .env.example .env    # only if you haven't already
```

Open `.env` in any editor and set exactly these lines (leave everything else default):

```
LLM_PROVIDER=groq
STT_PROVIDER=groq
TTS_PROVIDER=browser         # temp: use Qwen3 later if you want real audio

GROQ_API_KEY=gsk_YOUR_KEY_HERE

TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxx
```

**Note on TTS**: `browser` sends text back instead of audio. Telegram will only see text replies at this stage. In Step 7 we switch to Qwen3 (local audio) or Deepgram (cloud audio) for real voice notes.

## Step 4 — start the server (~30 sec)

```bash
cd "/Users/az/Desktop/Receptionist Agent"
source .venv/bin/activate
cd apps/api
uvicorn app.main:app --port 8000 --reload
```

You should see `Uvicorn running on http://0.0.0.0:8000`. Leave this terminal open.

**Verify in a second terminal:**
```bash
curl -s http://localhost:8000/health
# expected: {"ok":true,"llm":"groq","stt":"groq","tts":"browser"}
```

**If this breaks**: config typo. Check `.env` for stray characters or unescaped `#`.

## Step 5 — expose your local server to the internet (~2 min)

Telegram can't reach `localhost`. Use Cloudflare Tunnel (free, no signup).

**If you don't have cloudflared:**
```bash
brew install cloudflared     # macOS
```

**Start the tunnel** (in a third terminal):
```bash
cloudflared tunnel --url http://localhost:8000
```

Watch the output for a line like:
```
Your quick Tunnel has been created! Visit it at:
https://random-words-here.trycloudflare.com
```

**Copy that URL.** This is your `PUBLIC_URL`. It's ephemeral — will change every time you restart cloudflared.

**Verify the tunnel works:**
```bash
curl -s https://YOUR-TUNNEL-URL.trycloudflare.com/health
# expected: {"ok":true,...}
```

## Step 6 — tell Telegram where to send messages (~30 sec)

Replace both placeholders in this command with your actual values:

```bash
TG_TOKEN='123456789:AAxxxxxxxxxxx'
TUNNEL='https://random-words-here.trycloudflare.com'

curl -s "https://api.telegram.org/bot${TG_TOKEN}/setWebhook?url=${TUNNEL}/channels/telegram/webhook"
# expected: {"ok":true,"result":true,"description":"Webhook was set"}
```

**Verify it stuck:**
```bash
curl -s "https://api.telegram.org/bot${TG_TOKEN}/getWebhookInfo"
# expected: url should be your tunnel + /channels/telegram/webhook
```

## Step 7 — test it (~2 min)

Open Telegram → find your bot (search the username you set in Step 2) → hit **Start**.

**Test 1 — text:**
Send `Hi, I want to book an appointment for back pain tomorrow.`

Bot should reply within 2-4 seconds with something like:
> "Sure, I can help with that. What time works best for you tomorrow?"

Watch the uvicorn terminal — you should see request logs.

**Test 2 — voice:**
Hold the mic button → record: *"My name is John Carter and my phone number is 555-1234."*

Bot should transcribe it, keep the conversation going, and eventually call the `check_availability` and `book_appointment` tools.

**Test 3 — verify booking landed in SQLite:**
```bash
sqlite3 "/Users/az/Desktop/Receptionist Agent/data/voiceops.db" \
  "SELECT session_id, caller_name, phone, service, scheduled_for FROM bookings;"
```

You should see a row.

## Step 8 — get real voice replies (optional, ~5 min)

The bot currently replies with text only because `TTS_PROVIDER=browser`. To make it voice-note back:

**Option A: Qwen3-TTS (local, free, but slow on Mac ~40s per reply):**
```
TTS_PROVIDER=qwen3
```
Restart uvicorn. Every reply now generates a WAV locally and Telegram plays it. Slow but $0.

**Option B: Deepgram Aura (cloud, fast, uses your $200 credit):**
1. Sign up at [console.deepgram.com](https://console.deepgram.com) → create API key.
2. In `.env`:
   ```
   TTS_PROVIDER=deepgram
   DEEPGRAM_API_KEY=YOUR_KEY
   DEEPGRAM_TTS_VOICE=aura-asteria-en
   ```
3. Restart uvicorn.

Deepgram gives $200 credit on signup, no card. Enough for weeks of demos.

## Step 9 — record the Loom (~3 min)

Record ~90 seconds:
1. Show the .env file — point at `GROQ_API_KEY` and `TELEGRAM_BOT_TOKEN`. Say "no ElevenLabs, no Vapi, no OpenAI. Free tier only."
2. Show the Telegram chat.
3. Send one voice note requesting a booking.
4. Show the reply (audio + text).
5. Show the sqlite query — the booking row.
6. Say "same brain, same tools, works on WhatsApp, Twilio phone calls, and Vapi. Tell me your stack."

That's your demo. That's the pitch.

## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `Uvicorn running` but bot doesn't reply | Webhook not set / wrong URL | Re-run Step 6 |
| Webhook set but silence | Tunnel died | Check cloudflared terminal — restart if disconnected. New URL means redo Step 6 |
| Bot replies with error | Groq key wrong / missing | Test Groq directly: `curl -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models` |
| Bot transcribes voice as gibberish | Background noise + short clip | Speak clearly, 2+ seconds |
| "channel whatsapp" errors in log | Ignore — WhatsApp isn't configured, doesn't affect Telegram | — |
| SQLite query returns nothing | Booking flow didn't complete | Send a follow-up: "yes book it" |

## Cost total so far

$0. All-in.

## What you unlocked

- Working AI receptionist on Telegram
- Same brain works for WhatsApp, Twilio phone, Vapi, browser — flip env, done
- Screenshot-able booking flow for Upwork proposals
- Baseline for adding more verticals (restaurant, real estate) via `sample-data/`

## Next steps after this Loom is recorded

1. Repeat Step 1-7 with **WhatsApp Cloud API** — `docs/channels-setup.md` has that flow
2. Try the **Vapi flow** for a real phone number demo — `docs/vapi-setup.md`
3. Add a second vertical — restaurant or real-estate templates are the next PR
