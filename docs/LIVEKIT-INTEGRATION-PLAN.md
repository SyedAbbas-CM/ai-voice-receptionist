# LiveKit Integration Plan

**Status:** SCAFFOLD ONLY — deferred until Twilio-side speedups verify
**Owner:** networking chat (this session's ownership per audit split)
**Created:** 2026-08-22
**Number:** +12402127040 (LiveKit-native, US-Maryland 240 area code)

---

## Intent

Wire LiveKit as a **runtime-switchable alternative** to Twilio. Both providers coexist. User dials +14175743859 → Twilio path (current). User dials +12402127040 → LiveKit path. Real A/B on carrier + transport, same brain.

**Not** replacing Twilio. **Not** adopting LiveKit's opinionated `VoicePipelineAgent` (that would throw away the fixes we shipped this week). LiveKit is transport-only; our brain, cache, gate, farewell, watchdogs, prompt, TTS all stay identical.

---

## Why LiveKit at all

**User's goal:** "see how different LiveKit is." A comparison test.

**What LiveKit potentially wins vs Twilio:**
- Different PSTN carrier ingress (some regions have less jitter than Twilio)
- WebRTC transport (may be lower latency than Twilio Media Streams' custom WSS in some paths)
- Built-in `RoomIO` semantics + turn-taking primitives (which we don't need — we have our own)

**What LiveKit will NOT change:**
- OpenAI first-token latency (same provider, same region)
- Deepgram STT latency (same provider, same region)
- ElevenLabs TTS latency (same provider, same region)
- Karachi's uplink jitter (physical, out of any software's control)

**Realistic expectation:** LiveKit vs Twilio comparison might show 0-500ms per-turn difference. Bigger wins would only come from US-East server (Lever H).

---

## Ownership split during build

- **This chat (networking):** all code, dependencies, dashboard-instruction docs
- **User:** ~5 min in LiveKit dashboard (dispatch rule + number verification)
- **voice-agent chat:** untouched — LiveKit island is isolated

---

## Architecture: keep-brain path

Everything downstream of "raw caller audio arrives" stays unchanged:

```
+12402127040 caller
  ↓
LiveKit SIP ingress
  ↓
LiveKit dispatch rule → creates a Room
  ↓
Our LiveKit Worker (sidecar Python process, separate from uvicorn)
  ↓
LiveKitCallSession (parallel to TwilioActorSession)
  ↓
audio_bridge.py: LiveKit PCM 48k → μ-law 8k → StreamingSTTBridge.feed()
  ↓
[EVERYTHING BELOW IS IDENTICAL TO TWILIO PATH]
  StreamingSTTBridge → Deepgram Flux → TurnManager
  → CallActor (same class, same bump_turn, same gen control)
  → SessionManager → ReceptionistBrain → OpenAI → SpeechCommitGate
  → SentenceBuffer → _stream_tts_incremental → ElevenLabs
  ↓
tts_bridge.py: μ-law 8k from EL → PCM 48k → LiveKit room audio_out
  ↓
LiveKit egress
  ↓
Caller hears audio
```

**Contrast the WRONG "drop-in" path we're rejecting:**
```
LiveKit → VoicePipelineAgent(stt=Deepgram, llm=OpenAI, tts=EL) → caller
```
That path loses: response cache, conv-control fastpath, prompt cache prewarm, all humanness rules, farewell hangup, idle ladder, race fixes, cache-poisoning fixes, zombie-speaking watchdog, TTS stream watchdog, SmartTurn μ-law decode fix, sentence buffer, filler policy, everything.

---

## Directory layout (scaffolding created; runtime code stubs only)

```
apps/api/app/telephony/livekit/
├── __init__.py                        ← empty module marker
├── README.md                          ← operator-facing quickstart
├── worker.py                          ← Agent worker entrypoint (STUB)
├── audio_bridge.py                    ← LK PCM 48k → μ-law 8k → bridge (STUB)
├── tts_bridge.py                      ← EL μ-law 8k → PCM 48k → LK publish (STUB)
└── session_adapter.py                 ← binds LK room job → CallActor-shaped session (STUB)

scripts/
├── run_livekit_worker.sh              ← sidecar launcher (NOT YET WRITTEN)
└── livekit_dispatch_rule.json         ← config for user to paste in LK dashboard (NOT YET WRITTEN)
```

Nothing under `apps/api/app/telephony/livekit/` is wired into runtime. Env flag doesn't exist yet either. This is deliberate: safe to commit + resume, zero risk to current Twilio calls.

---

## Runtime switch semantics (planned, not yet built)

Add to `.env`:
```bash
TELEPHONY_PROVIDER=twilio   # default; only Twilio processes calls
# TELEPHONY_PROVIDER=livekit # only LiveKit worker runs
# TELEPHONY_PROVIDER=both    # both run; A/B by dialing different numbers
```

Sidecar launch (via `scripts/run_livekit_worker.sh`):
- If `TELEPHONY_PROVIDER in ("livekit", "both")` → spawn LK worker as subprocess
- If `TELEPHONY_PROVIDER == "twilio"` → don't spawn LK worker (default)
- uvicorn (Twilio handler + /health + /metrics) always runs

Recommended default once wired: **`both`**. Dial either number for live A/B without a bounce.

---

## Audio format conversion (both directions)

**Inbound (caller mic → our brain):**
- LiveKit gives PCM 48kHz mono, 20ms frames (960 samples int16)
- We need μ-law 8kHz for `StreamingSTTBridge.feed()` (which streams to Deepgram)
- Path: `audioop.ratecv(pcm_48k, 2, 1, 48000, 8000, state)` → `audioop.lin2ulaw(pcm_8k, 2)` → `bridge.feed(mulaw_frame)`
- Latency cost: sub-millisecond per frame, `state` persisted across frames to avoid click artifacts (same pattern as NET-02 SmartTurn fix)

**Alternative to consider before shipping:** Deepgram Flux supports linear16@48k natively — could skip the μ-law step and feed 48k directly. Bench-decide during actual implementation.

**Outbound (EL TTS → caller):**
- ElevenLabs gives μ-law 8kHz (Twilio-shape, current setup)
- LiveKit expects PCM 48kHz
- Path: `audioop.ulaw2lin(mulaw, 2)` → `audioop.ratecv(pcm_8k, 2, 1, 8000, 48000, state)` → `rtc.AudioSource.capture_frame(...)`
- Same latency cost, same state-persistence rule

**Alternative:** ask ElevenLabs for PCM 48k output directly (their API supports `pcm_44100` and `pcm_24000`; not native 48k, would still need one resample). μ-law route is likely simpler.

---

## LiveKit dashboard config the user owns (~5 min)

1. Go to https://cloud.livekit.io/projects/p_1skl1tri3bo/telephony
2. Confirm +12402127040 is listed under "Phone numbers"
3. Under "Dispatch rules" → New:
   - Name: `receptionist-agent-dispatch`
   - Type: individual — each caller gets their own room
   - Room prefix: `call-`
   - Agent name: `receptionist` (must match `agent_name` in `worker.py`)
4. Save

That's the entire user-side task. Nothing more.

Alternative via `lk` CLI (if user prefers):
```bash
lk sip dispatch-rule create scripts/livekit_dispatch_rule.json
```
(That JSON file will be written when we un-defer this work.)

---

## Dependencies

```bash
pip install livekit-agents  # Agent worker runtime, room subscription, dispatch
# livekit (core client SDK) is a transitive dep, don't install separately
```

**NOT installing:**
- `livekit-plugins-deepgram` — we use our own Deepgram Flux integration
- `livekit-plugins-openai` — we use our own router LLM stack
- `livekit-plugins-elevenlabs` — we use our own EL TTS + cache + gate
- `livekit-plugins-silero` — we use SmartTurn + DG Flux native EoT

Total added: ~50MB in venv. Much less than the ~200MB estimate that assumed pipeline plugins.

---

## Risks + open questions

**Risk 1 — LiveKit worker event-loop contention.** LiveKit Agents SDK has its own asyncio task tree. Our brain also lives in asyncio. Running both in one process may cause loop starvation on hot paths. **Mitigation:** run LiveKit worker as separate subprocess. Communication via IPC (unix socket or shared state). Adds complexity but preserves runtime isolation.

**Risk 2 — First-call cold start on LiveKit is slower.** JIT + dispatch route + room creation adds ~500-1500ms to first call after worker boot. Warmup call at worker startup can hide this (parallel to our OpenAI/EL warmup pattern).

**Risk 3 — LiveKit's PSTN provider may be worse than Twilio.** We assume LiveKit's carrier is comparable. Reality could be worse from Karachi. First test call tells us.

**Risk 4 — SIP inbound trunk auth mismatch.** LiveKit's SIP endpoint needs the trunk config to match the number. If +12402127040 wasn't provisioned through LiveKit's own SIP service (was imported), auth may fail on first call. **Mitigation:** user verifies dispatch rule is bound to the right trunk during step 3 above.

**Open question 1 — Does LiveKit's built-in barge detection conflict with ours?** LiveKit publishes VAD events. Our brain uses SmartTurn + DG Flux EoT. If LK's VAD fires first, we may get barge-then-final duplication. Need to disable LK VAD in the worker config.

**Open question 2 — LiveKit worker restart behavior.** If the worker process crashes mid-call, does LiveKit failover to another worker instance? For single-process operation this is n/a; multi-worker HA is a later concern.

---

## Realistic effort to un-defer

- Un-defer + code: 3-4h focused work (this session or one dedicated block later)
- User dashboard config: 5 min
- First test call + audio-format bug hunt: 30-60 min
- **Total: half a work session.** Not tonight.

---

## Success criteria when we actually ship

1. Dial +14175743859 (Twilio) → agent answers, all fixes work
2. Dial +12402127040 (LiveKit) → agent answers, all fixes work
3. Compare felt latency on same booking script across both numbers, same call flow
4. Verify none of the Twilio-path features regress (idle ladder, farewell hangup, race fixes, etc)

---

## Why deferred

**Twilio-side has 3 unshipped speedups** (Levers E, C, F) prepped code-only and pending PID 75095 verification. Ship those + verify felt improvement + measure real baseline BEFORE adding a whole new transport layer. Otherwise the A/B is confounded — you won't know if LiveKit "feels different" because of LiveKit or because of the Twilio-side improvements.

**Sequence:**
1. User tests PID 75095 → verify tonight's 8 fixes hold
2. Voice-agent + I bundle-ship E+C+F → user tests again → measure Twilio-side ceiling
3. Return here, un-defer LiveKit, build it, measure A/B against known-good Twilio baseline

---

## Handoff to next session

When ready to un-defer:
1. Read this doc
2. Read `apps/api/app/telephony/livekit/*.py` stubs (docstrings already sketch implementation)
3. `pip install livekit-agents` in venv
4. Write `worker.py` `entrypoint` — subscribe to first participant's audio track, spawn `LiveKitCallSession`
5. Write `audio_bridge.py` — track subscription + PCM 48k → μ-law 8k conversion
6. Write `tts_bridge.py` — create outbound audio track + μ-law 8k → PCM 48k conversion
7. Write `session_adapter.py` — instantiate `CallActor` + `StreamingSTTBridge` + brain, plumb bridges
8. Write `scripts/run_livekit_worker.sh` (mirror `scripts/run_server.sh` pattern)
9. Write `scripts/livekit_dispatch_rule.json`
10. Add `TELEPHONY_PROVIDER` env to `.env` + `config.py`
11. Update `run_server.sh` to conditionally spawn worker sidecar
12. User dashboard config (see section above)
13. Test call to +12402127040 → verify + iterate
