# LiveKit Island — Operator Quickstart

**STATUS:** scaffold only, deferred. Do NOT try to run this yet — files are stubs.

Read `docs/LIVEKIT-INTEGRATION-PLAN.md` for the full architecture + un-defer checklist.

## When we un-defer

**Number in play:** +12402127040 (US Maryland, 240 area code) — LiveKit-native
**LiveKit project:** `p_1skl1tri3bo`
**LiveKit URL:** `wss://voice-agent-shfi5ymq.livekit.cloud`
**SIP URI:** `sip:1skl1tri3bo.sip.livekit.cloud`

## Config you (operator) do in the LiveKit dashboard

1. Go to https://cloud.livekit.io/projects/p_1skl1tri3bo/telephony
2. Confirm +12402127040 is listed under **Phone numbers**
3. Under **Dispatch rules** → New:
   - Name: `receptionist-agent-dispatch`
   - Type: individual — each caller gets their own room
   - Room prefix: `call-`
   - Agent name: `receptionist` (must match `agent_name` in `worker.py`)
4. Save

## Running the worker (after un-defer)

```bash
# In .env
TELEPHONY_PROVIDER=both   # runs both Twilio uvicorn AND LiveKit worker sidecar

# Sidecar launcher
./scripts/run_livekit_worker.sh &
```

Then dial +14175743859 (Twilio path) or +12402127040 (LiveKit path). Both
route to the same brain — real A/B comparison, same agent behavior, only
transport differs.

## Files in this island

- `worker.py` — LiveKit Agents SDK entrypoint (spawns per-call jobs)
- `session_adapter.py` — binds a LiveKit room job to our CallActor + brain
- `audio_bridge.py` — inbound: LK PCM 48k → μ-law 8k → StreamingSTTBridge
- `tts_bridge.py` — outbound: EL μ-law 8k → LK PCM 48k → room audio_out

## What LiveKit gives us that Twilio doesn't

- Different PSTN carrier (may have less Karachi-side jitter, may not — first test tells)
- WebRTC transport (native RTP + jitter buffer inside LiveKit edge)
- Built-in SIP + WebRTC bridge

## What LiveKit does NOT change

- OpenAI first-token latency (same provider, same region)
- Deepgram STT latency (same provider, same region)
- ElevenLabs TTS latency (same provider, same region)
- Karachi mobile carrier uplink jitter (physical, out of any software's control)

Realistic per-turn win: 0-500ms. Bigger wins need US-East server.

## Why keep-brain path (not full drop-in)

Full drop-in via `VoicePipelineAgent` would lose:
- response cache, conv-control fastpath, prompt cache prewarm
- all humanness prompt rules (Ships 6+7 tonight)
- farewell hangup, idle ladder, race fixes, cache-poisoning fixes
- zombie-speaking watchdog, TTS stream watchdog
- SmartTurn μ-law decode fix, sentence buffer, filler policy

Keep-brain path uses LiveKit as **transport only**. Same brain, same fixes,
just a different way for audio to arrive and leave. This is what we want.
