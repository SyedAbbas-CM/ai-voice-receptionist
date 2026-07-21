# Clone your own voice locally with Qwen3-TTS

10 minutes of your time. Your voice becomes the AI receptionist. 100% local, zero API cost per synthesis.

**Cost**: $0. Runs on your machine.
**Time**: ~10 min (2 min prep + 30 sec recording + retakes + wiring).
**Speed on M1 Pro**: ~40s per synthesized sentence. Not real-time — good for pre-rendered browser sim demos, not live phone calls.

For live phone calls, ElevenLabs Instant Voice Clone from the same recording is 300ms per sentence and sounds arguably better. But it costs credits. **This runbook is 100% local and free.**

---

## Step 1 — Read the script into QuickTime (5 min)

Open **QuickTime Player** → **File** → **New Audio Recording** → click red record.

Read this script naturally, in one take. Don't over-articulate. Talk like you're on a friendly phone call.

> "Hi, thanks for calling Riverside Family Clinic. My name is Alex,
> I'm the AI receptionist. I can help you book an appointment,
> answer questions about our services, or connect you with a nurse.
> What can I do for you today? Just let me know and I'll take care
> of it right away."

That's about 30 seconds when read naturally. Stop recording, save as `voice_sample.wav` in the repo's `data/` folder:

```bash
# In Finder, drag the recording into:
/Users/az/Desktop/Receptionist Agent/data/voice_sample.wav
```

**Recording tips that matter for clone quality:**

- **Quiet room** — no fans, no traffic, no keyboards clacking
- **6-12 inches from your mouth** — too close is boomy, too far is echoey
- **AirPods mic works but built-in Mac mic is often cleaner** — try both, pick the one that sounds less "phone-like"
- **Speak naturally** — don't read like an audiobook narrator; speak like you're chatting with a friend
- **Match the target usage** — you're cloning a receptionist voice, so read warmly, not dramatically

If it sounds weird when you play it back, re-record. The clone quality mirrors the reference clip quality exactly.

---

## Step 2 — Verify the recording is usable (30 sec)

```bash
cd "/Users/az/Desktop/Receptionist Agent"
afplay data/voice_sample.wav
```

Listen critically. If you hear:
- Room echo → too far from mic, re-record closer
- Popping/breathing → too close, back off 6 inches
- Fan/traffic hum → move rooms
- You sound stilted → talk more like you're on a call, less like reading

Check the file format:
```bash
file data/voice_sample.wav
```

Should say something like `WAVE audio, ..., mono, 44100 Hz` or similar. Any sample rate above 16kHz is fine — Qwen3 downsamples internally.

---

## Step 3 — Update `.env` (1 min)

Add or update these lines (I'll do this for you if you tell me your recording is ready):

```bash
TTS_PROVIDER=qwen3
QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-1.7B-Base
QWEN3_TTS_DEVICE=auto
QWEN3_TTS_REF_AUDIO=/Users/az/Desktop/Receptionist Agent/data/voice_sample.wav
QWEN3_TTS_REF_TEXT=Hi, thanks for calling Riverside Family Clinic. My name is Alex, I'm the AI receptionist. I can help you book an appointment, answer questions about our services, or connect you with a nurse. What can I do for you today? Just let me know and I'll take care of it right away.
```

**Critical**: `QWEN3_TTS_REF_TEXT` must match your recording **exactly** — same words, same order, same punctuation. If you improvised or skipped words, edit this to match what you actually said. Even one wrong word tanks the clone quality.

---

## Step 4 — Test the clone (5 min synthesis)

Run the smoke test script:

```bash
cd "/Users/az/Desktop/Receptionist Agent"
source .venv/bin/activate
python scripts/test_qwen3_tts_clone.py
```

Wait ~5 minutes on your M1 Pro (or ~1 min on Linux + 3090). The script:
1. Loads Qwen3-TTS 1.7B Base (already downloaded — ~15s from cache)
2. Reads your reference clip + text
3. Synthesizes: "Hi, thanks for calling Riverside Family Clinic. How can I help you today?"
4. Writes `data/qwen3_clone_smoke.wav`
5. Plays it

Listen. It should sound recognizably like you. If it doesn't:
- Common cause: `QWEN3_TTS_REF_TEXT` doesn't match the recording exactly. Fix and rerun.
- Another common cause: recording is too short (<10s) or too noisy. Re-record.

---

## Step 5 — Boot server on your cloned voice

```bash
cd apps/api
uvicorn app.main:app --port 8001
```

Open http://localhost:8001/ — the browser sim now uses your cloned voice for every reply.

**Expected latency on M1 Pro:**
- STT (local Whisper): ~500ms
- LLM (NVIDIA 70B): ~2s
- TTS (Qwen3 1.7B Base clone): ~40s per sentence
- **Total per turn: ~42s**

Slow, but usable for a demo Loom where you post-edit the silence out. For a live phone call, use ElevenLabs cloning instead.

---

## Step 6 — Record a Loom demo (10 min)

Same flow as the Vapi runbook:

1. Open Loom → Screen + Camera + Mic
2. Layout: browser sim on left, `/debug/traces/summary` on right
3. Read your intro on-camera (30 sec):
   > "I built an AI receptionist. This is a demo — you're going to hear
   > my own voice, cloned locally on my machine, answer as the AI
   > receptionist. No cloud voice API. This exact stack works with a
   > cloned client voice too."
4. Type or speak: "I need an appointment for back pain tomorrow at 10am"
5. Wait for AI reply — post-edit the ~40s wait out of the video
6. Show the `/debug/traces/summary` panel — highlight the 0-cost synthesis
7. End with: "Same code, ANY voice, any calendar. Send me a message."

Total Loom: 60-90 seconds after editing.

---

## For live phone calls: switch to ElevenLabs cloning

The local Qwen3 clone is too slow for real-time on your Mac. When a client wants a real phone number that rings, do this:

1. Upload the same `data/voice_sample.wav` to [elevenlabs.io/app/voice-lab](https://elevenlabs.io/app/voice-lab) → **Instant Voice Clone**
2. Give it a name → copy the resulting voice ID (`xxxxxxxxxxxxxx`)
3. In `.env`:
   ```bash
   TTS_PROVIDER=elevenlabs
   ELEVENLABS_VOICE_ID=<paste-the-cloned-voice-id>
   ELEVENLABS_MODEL=eleven_turbo_v2_5
   ```
4. Restart server. Same clone, ~300ms per sentence, real-time phone-call viable.

Free tier: 10,000 characters/month clone usage. Enough for ~20-40 demo calls.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Clone sounds robotic / not like me | REF_TEXT mismatch | Edit REF_TEXT to match recording word-for-word |
| Clone has echo / boominess | Recording had room noise | Re-record in a quieter room, closer to mic |
| Clone speaks too fast/slow | Recording speed doesn't match natural speech | Talk naturally when recording, not slowly |
| First-sentence latency 5+ minutes | Model isn't cached | First-ever run downloads 1.8GB. Subsequent runs load in ~15s. |
| `NaN in probability tensor` | MPS float16 sampling bug | Adapter forces float32 on MPS automatically; verify QWEN3_TTS_DTYPE isn't overridden |
| Silence on output | REF_TEXT is empty or doesn't parse | Check `.env` — no unescaped quotes; try shorter REF_TEXT (15-20 words) |
