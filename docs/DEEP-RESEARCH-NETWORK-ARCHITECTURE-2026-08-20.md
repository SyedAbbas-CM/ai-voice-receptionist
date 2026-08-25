# Deep Research — Network & Session Architecture (Round 2)

**Source:** ChatGPT deep-research pass, delivered 2026-08-20 after seeing our GroqLLM+OpenAI-Fast+shared-HTTP-client improvements.

**Verdict:** the biggest remaining latency lever isn't a provider swap — it's making the entire call behave as **one long-lived real-time session** with four persistent sockets instead of dozens of per-turn reconnects. Plus geography (server in US-East, not Pakistan).

---

## The recommended architecture

```text
US caller
   ↓
Twilio US1 / Ashburn
   ↓  ONE WSS FOR ENTIRE CALL
Agent server: AWS us-east-1 / Northern Virginia
   ├── ONE Deepgram WS for entire call
   ├── ONE ElevenLabs multi-context WS for entire call
   ├── ONE OpenAI Responses WS per call
   │      OR app-lifetime HTTP keepalive pool
   └── app-lifetime Groq HTTP/2 pool
```

**The server should not be in Pakistan for US production calls.**
- Twilio explicitly recommends placing infrastructure near its US1 (eastern-US) processing region.
- ElevenLabs reports ~100-150ms TTFB from North America vs 150-200ms from South Asia.
- Deepgram identifies geography/network transit as a major latency component.

**#1 infrastructure experiment:** AWS `us-east-1` or Northern Virginia VPS → direct public WSS endpoint → no dev tunnel in production path. Benchmark before assuming savings, but highest-confidence network change.

---

## The 10 numbered recommendations

### 1. ElevenLabs multi-context WS — one connection per CALL (upgrades earlier "per turn" recommendation)

**NEW.** ElevenLabs now documents a **multi-context WebSocket** specifically for voice agents. Explicitly recommends **one WebSocket connection per end-user session**, not per sentence and not even per assistant turn. Contexts can be created and closed independently inside that connection, including on barge-in.

Change from:
```text
TURN 1: connect ElevenLabs → speak → close
TURN 2: connect ElevenLabs → speak → close
```

To:
```text
CALL START: connect ElevenLabs
  context agent-turn-1: speak, close context
  context agent-turn-2: speak, close context
  context agent-turn-3: caller interrupts, close context immediately
  context agent-turn-4: speak
CALL END: close ElevenLabs socket
```

Eliminates repeated DNS + TCP + TLS + HTTP upgrade → WebSocket per turn.

**Config to test:**
```text
endpoint:  /multi-stream-input
model:     eleven_flash_v2_5
output_format:      ulaw_8000
inactivity_timeout: 180
auto_mode:          true
```

Defaults to 20s inactivity timeout, up to 180s allowed. `auto_mode=true` removes chunk scheduling but MUST be fed **complete sentences** (partial sentences damage quality).

Reinforces: **first LLM chunk should be a short complete sentence** ("Gotcha — you're looking for a cleaning.") NOT just "Gotcha,".

### 2. Stop transcoding audio anywhere

Telephony pipeline can become codec passthrough:
- Twilio Media Streams requires: μ-law 8000 Hz base64
- Deepgram Flux accepts: mulaw 8000 Hz
- ElevenLabs can emit: ulaw_8000

Use:
```text
TWILIO mulaw 8kHz → base64 decode only → DEEPGRAM mulaw 8kHz
...
ELEVENLABS ulaw_8000 → ideally no audio transcoding → TWILIO ulaw_8000
```

Do NOT:
```text
mulaw8 → PCM16 → resample 16k → STT → PCM → resample 8k → mulaw → Twilio
```
unless accuracy test proves you need it.

Even smaller optimization: ElevenLabs multi-context returns base64, Twilio expects base64 media payload. If ElevenLabs output is already `ulaw_8000`, test whether bridge can forward base64 directly rather than `decode_base64() → encode_base64()` per audio packet.

### 3. Deepgram Flux + EagerEndOfTurn (bigger than provider swap)

Currently on Nova-3. For voice-agent latency specifically, A/B against **Flux**:
- ~260ms end-of-turn detection
- Emits: `EagerEndOfTurn`, `TurnResumed`, `EndOfTurn`, `StartOfTurn`

Instead of:
```text
caller stops → wait until certain → start LLM
```

Do:
```text
caller probably finished → EagerEndOfTurn → START LLM SPECULATIVELY

if caller resumes: TurnResumed → cancel generation
if caller really finished: EndOfTurn → use the response already being generated
```

Trims ~100-200ms in latency-sensitive configs, but causes **50-70% more LLM calls** due to speculative generation. For demo speed, the trade is worth benchmarking.

Start:
```text
eager_eot_threshold = 0.4
eot_threshold       = 0.7
```

**Important context for our repo:** Flux was tried on 2026-08-11 and emitted empty events on Twilio mulaw. That was ~9 days ago; may have been fixed on Deepgram's side, or may need an isolation-bench (see `.env` comment). Verify before enabling in production.

### 4. Audio chunk size — Flux prefers 80ms

Deepgram recommends 20-100ms streaming buffers; Flux specifically recommends **80ms audio chunks**.

Instead of random Python queue sizes:
```text
Twilio frames → Python queue → maybe queue grows → Deepgram
```

Deliberate timestamp-driven assembler:
```text
Twilio frames → timestamp-driven ~80ms Flux chunk → Deepgram immediately
```

Make queue **bounded**. Every provider can individually look "fast" while the application has created half a second of hidden latency.

### 5. Twilio outbound buffer = latency queue (critical for barge-in)

Twilio does NOT instantly play every byte you send. Outgoing audio is buffered and played in order.

Two Twilio control messages that matter:
- `mark` — determine when previously sent audio actually finished
- `clear` — immediately empty queued audio

**Barge-in path must be:**
```text
Deepgram StartOfTurn / speech detected
     ├── cancel LLM generation
     ├── cancel ElevenLabs context
     ├── discard local TTS queue
     └── send Twilio CLEAR
              ↓
       queued audio disappears
```

Do NOT merely stop generating new TTS. If 800ms of audio is already in Twilio's playout buffer, caller hears it.

Track `mark` IDs to distinguish these states: generated / sent to Twilio / actually played / cleared before playback.

### 6. OpenAI Responses WS — worth testing, don't expect magic on every turn

Fast/Priority mode is a real service tier (already shipped in our repo).

Next experiment: **Responses WebSocket mode** — persistent WS + incremental inputs + `previous_response_id`. Eliminates repeated continuation overhead. Most useful for long-running, tool-call-heavy workflows. **Up to ~40% E2E improvement on workflows with 20+ tool calls.**

- Simple receptionist turns ("what time do you close?") — don't expect 40%
- Appointment workflow loops (extract intent → availability → model → tool → model → booking → model confirmation) — much more interesting

**Experiment:**
```text
A. current OpenAI SSE + warm HTTP connection
B. OpenAI SSE + Fast
C. Responses WS + Fast
```

Use **actual 24k prompt and actual tools**, not "Hello world".

Measure: TCP/TLS setup, request sent, first model byte, first text token, first complete sentence, first TTS audio.

### 7. Connection lifetime policy

| Connection | Lifetime |
|---|---|
| Twilio ↔ agent WSS | entire phone call |
| Agent ↔ Deepgram WSS | entire phone call |
| Agent ↔ ElevenLabs multi-context WSS | entire phone call |
| Agent ↔ OpenAI Responses WS | entire phone call, if experiment wins |
| Agent ↔ OpenAI HTTP | application lifetime pooled client |
| Agent ↔ Groq | application lifetime HTTP/2 pooled client |

Our Groq fix from earlier today already moved to the final row.

### 8. Remove proxy and tunnel layers from the hot path

Development:
```text
Twilio → internet → tunnel provider → Pakistan → local machine
```
Fine for dev; contaminates every production latency number.

Production:
```text
Twilio → AWS ALB/NLB or direct TLS endpoint → voice process
```
Same US-East region as application.

Twilio requires Media Streams over secure `wss` on TCP 443.

If keeping Nginx/ALB:
- WebSocket upgrade enabled
- SSE buffering disabled
- No request buffering
- Long idle timeouts
- No cross-region backend

### 9. Twilio webhook routing — call startup only

Twilio lets you choose HTTP webhook egress edges: `e=ashburn` (US East).

For an app in Northern Virginia:
```text
incoming webhook → Ashburn → us-east app
```

**Affects call setup, TwiML webhook, initial stream establishment — NOT each conversational LLM turn.** Don't confuse with the more important persistent Media Stream path.

### 10. The "ultra-fast" architecture in one diagram

```text
                    ┌───────────────────────────────┐
                    │ Agent host: US EAST / VA     │
                    │                               │
US caller ─ Twilio ─┤ Twilio WSS     [call-long]   │
                    │      │                        │
                    │      ▼                        │
                    │ Deepgram Flux WS [call-long] │
                    │      │                        │
                    │ EagerEndOfTurn                │
                    │      │                        │
                    │      ├──► Groq warm HTTP/2    │
                    │      │           OR           │
                    │      └──► OpenAI persistent WS│
                    │                  + Fast       │
                    │                    │           │
                    │             short sentence    │
                    │                    │           │
                    │ ElevenLabs Multi-Context WS   │
                    │ [ONE PER CALL]                │
                    │ ulaw_8000                     │
                    │                    │           │
                    └────────────────────┼───────────┘
                                         │
                                         ▼
                                      Twilio
                                         │
                                         ▼
                                       caller
```

**Each call = only four long-lived sockets.** No per-turn TTS handshake. Potentially no per-turn OpenAI transport handshake. No STT reconnect. No audio resampling. No TTS transcoding.

---

## What to make Claude Code do next, in order

1. **Deploy exact current agent in `us-east-1`** and run same calls against local/Pakistan deployment. Establish real network geography effect.
2. **Implement ElevenLabs multi-context WS: one connection per call.** Supersedes our earlier "one WS per assistant turn" design.
3. **Make audio path completely `mulaw/ulaw 8000` end to end.** Twilio→Deepgram and ElevenLabs→Twilio should require no resampling/transcoding.
4. **A/B Nova-3 vs Flux.** First normal Flux EOT; then `EagerEndOfTurn`.
5. **Implement hard barge-in cancellation:** local queue clear + ElevenLabs context cancel + LLM cancel + Twilio `clear`.
6. **Benchmark OpenAI Responses WS vs current Fast SSE path.** Keep only if real appointment/tool workload improves.

---

## Telemetry needed

Every turn should produce:

```text
caller_last_audio_timestamp

deepgram_last_audio_sent
deepgram_eager_eot_received
deepgram_final_eot_received

llm_request_started
llm_first_byte
llm_first_token
llm_first_complete_sentence

elevenlabs_text_sent
elevenlabs_first_audio_received

twilio_first_audio_sent
twilio_mark_acknowledged
```

Plus connection telemetry:

```text
twilio_ws_connect_ms

deepgram:
  dns_ms
  tcp_ms
  tls_ms
  websocket_upgrade_ms

openai:
  connection_reused
  first_byte_ms

groq:
  connection_reused
  first_byte_ms

elevenlabs:
  dns_ms
  tcp_ms
  tls_ms
  websocket_upgrade_ms
  x-region
```

Deepgram provides tooling to break connection latency into DNS→TCP→TLS→WebSocket upgrade. ElevenLabs exposes serving region via `x-region` header.

Only then you know whether a 900ms turn is:
```text
250 endpointing / 300 OpenAI / 140 ElevenLabs / 210 network+buffering
```
vs:
```text
80 endpointing / 300 OpenAI / 120 ElevenLabs / 400 internal queues
```

Those need completely different fixes.

---

## Current speed priority (from this deep research)

1. **US-East deployment**
2. **Persistent ElevenLabs multi-context WS**
3. **Zero-transcode `ulaw_8000`**
4. **Flux + EagerEndOfTurn**
5. **Twilio buffer/`clear` correctness**
6. **Persistent OpenAI Responses WS benchmark**
7. **Micro-tune queues/proxies/socket settings**

If those land cleanly, **sub-second becomes the normal case rather than the lucky case**. Next engineering target: **500-700ms p50 end-of-user-turn → first audible useful speech**, watching p95 much more aggressively than p50. Engineering target, not a provider guarantee.

---

## Cross-references

- Repo's Groq streaming + shared client fix: `apps/api/app/providers/llm/groq_llm.py` (session log 2026-08-20 01:20)
- OpenAI Fast tier already enabled: `.env OPENAI_SERVICE_TIER=fast`
- Flux config exists but currently OFF: `deepgram_use_flux=False` — see comment about Twilio mulaw incompat 2026-08-11
- Persistent OpenAI WS scaffolded but off: `openai_persistent_ws_enabled=False`
- Cloudflare tunnel is currently the "dev tunnel in production path" this doc warns against — see `WORKING-NOTES.md` "Current server" section

---

## Related documents

- `docs/openai-speed-research-2026-08-20.md` — Round 1 pure-speed research (OpenAI-specific TTFT levers)
- `docs/HUMANNESS-RESEARCH-BRIEF-2026-08-20.md` — original research brief sent to ChatGPT
- `deep-research-report-humanness.md` — Round 1 humanness deep-research report
- `docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md` — codebase-vs-market gap audit
- `docs/UNIFIED-IMPLEMENTATION-PLAN.md` — consolidated task plan
- `VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md` — original 2600-line master roadmap
