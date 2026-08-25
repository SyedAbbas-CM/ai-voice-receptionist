# ChatGPT Audit Briefing — 3s Karachi Latency Floor

**Date:** 2026-08-24 02:55 PKT
**Bundle:** `/Users/az/Desktop/receptionist-codebase-2026-08-24_0254-3s-floor-audit-2026-08-24.zip`
**Live PID:** 46015 (`gpt-4o-mini`, `RESPONSE_CACHE_BYPASS=true`, Flux eot_timeout=1000ms)

## Verbatim user report (this session)

> "im not sure if it got faster but i wont say it got slower tho thats for sure lets make this number smaller"
> 
> "not sure if it causeda nything difference i asked two basic questions can you hear me who am i talking with i counted physically on my fingers 3 seconds"

## Real trace from that call (CA34075f5b61cd146212c8aa35fb7b2169, 02:52:03-14)

### Turn 1 "Can you hear me?"
| Event | Wall-clock | Δ prev |
|---|---|---|
| STT_VAD speech_start | 02:52:03.325 | — |
| STT_PARTIAL "Hello. Can you hear me?" | 02:52:03.924 | +599ms |
| **STT_FINAL** | 02:52:04.014 | +90ms |
| **LLM_FIRST_TEXT** | 02:52:05.033 | **+1019ms** (Karachi→OpenAI Iowa) |
| TWILIO_FIRST_MEDIA_SENT | 02:52:05.412 | +379ms |
| **TWILIO_FIRST40_ACK** | 02:52:05.832 | **+420ms** (Twilio→carrier→ear) |

### Turn 2 "Who am I talking with?" (should hit cache — DOES NOT because bypass=true)
| Event | Wall-clock | Δ prev |
|---|---|---|
| STT_VAD speech_start | 02:52:11.204 | — |
| STT_PARTIAL | 02:52:12.141 | +937ms |
| STT_FINAL | 02:52:12.264 | +123ms |
| brain-job dispatched | 02:52:12.265 | +1ms |
| **TTS_STREAM_START (transport=http, no cache)** | 02:52:13.229 | **+964ms** (full LLM roundtrip) |
| TWILIO_FIRST_MEDIA_SENT | 02:52:13.508 | +279ms |
| **TWILIO_FIRST40_ACK** | 02:52:14.283 | **+775ms** |

Total: ~2.4-3s felt mouth-close-to-ear on every turn.

## Why every turn is ~3s

**Two chunks dominate:**

1. **~800-1000ms:** Karachi → OpenAI Iowa LLM roundtrip
2. **~600-800ms:** TWILIO_FIRST40_ACK — Twilio-side delivery (Karachi server → Twilio US → PK carrier → phone speaker)

Everything else (Flux STT, sentence buffer, sanitize, brain routing) is <150ms combined.

## What we already fixed tonight and it's live

1. **Sync SQLite → async thread pool** (`packages/response_cache/cache.py:aget/aput`). Was causing 12-second event-loop freezes (verified in CAff590033 trace: `EVENT_LOOP_LAG 12914.2ms`). Fix: `asyncio.to_thread` wrapper. **Gone.**
2. **Deepgram Flux `eot_timeout_ms: 3000 → 1500 → 1000`** iteratively. Trace shows STT_FINAL now fires within 90-130ms of last STT_PARTIAL (was ~1000ms).
3. **Structured-input 2000ms application-side sleep → 500ms Flux-gated cooldown**. K1 lexical-hold `_INCOMPLETE_TRAILING_WORDS` fully skipped on Flux path (Flux's semantic EOT is authoritative).
4. **`FAREWELL_HANGUP_ABORTED` bug** — check was `if self._idle_task is not None` which is ALWAYS true after `_arm_idle_followup()` runs. Now uses `_caller_spoke_since_farewell` flag. Verified fires: `FAREWELL_HANGUP call=CA3c9daf0658 — closing call`.
5. **Prompt version stamping** (`CALL_START_PROMPT prompt_sha=... prompt_chars=... model=...`). Traceability across builds.
6. **TWILIO_FIRST_MEDIA_SENT metric** (once per reply, was per-frame due to loop-local flag bug).
7. **POST_EOT_HOLD_MS metric** with reason codes (`structured_ask` / `k1_incomplete_word` / `none`).
8. **LEAKED_META guard** — drops LLM output paraphrasing internal slot names ("caller provided name", tool names). Defense-in-depth alongside voice-agent's brain-side JSON drop.
9. **Model swap** gpt-5.4-nano → gpt-4o-mini (via multi-provider tournament — see `scripts/voice_llm_bench_multiprovider.py`).

## What's on disk from voice-agent but flag=OFF (inert)

**NextActionPolicy A1/A2 wiring** (`packages/dialogue/next_action_policy.py`, `packages/core_agent/next_action_synthesizer.py`, brain.py intercept). Skips 2nd LLM call on booking-confirm turns by rendering deterministically from tool_results. Flag: `NEXT_ACTION_POLICY_ENABLED=false` default. Ready to activate.

## Missing/deferred

- **`enter_slot_capture()` in production workflow** — the `StructuredInputSession` is fully built + tested, has ZERO non-test callers. Every phone/name/address turn goes through the general brain path with 500ms cooldown as safety-net. Voice-agent's next task (#49). ~4-6h careful work.
- **Only "phone" parser** in `packages/slot_parsers/registry.py`. Name/email/date/time/yes-no not registered.
- **Flux `end_of_turn_confidence` + `turn_index` discarded** before app layer sees them (`packages/runtime/streaming_stt_bridge.py`). Extension task tracked.
- **Dynamic Flux Configure per turn** — spec exists in Deepgram docs, no bench in repo. Would enable per-turn EOT tuning based on expected input type. Blocked on bench.
- **US-East server migration** — server currently runs from Karachi behind Cloudflare tunnel. Every LLM call + every Twilio Media Streams packet hairpins through the Arabian Sea. Hetzner CX22 Ashburn ($4.59/mo) would eliminate ~600-1000ms per turn according to my analysis. User has not committed to it yet.

## Current stack (verify against zip)

| Component | Choice | Rationale |
|---|---|---|
| Telephony | Twilio Media Streams | US1 region (only viable option per our research) |
| STT | Deepgram Flux (nova-3-turbo class w/ native EOT) | Best in-class semantic EOT |
| LLM | OpenAI gpt-4o-mini | Won our multi-provider tournament (see script) |
| TTS | ElevenLabs Flash v2.5 (HTTP streaming) | Multi-context WS deferred |
| Prompt | 21158 chars (post-compaction), 98% OpenAI prefix-cache hit rate | |
| Runtime | Python 3.11 + FastAPI + asyncio | Karachi laptop via Cloudflare tunnel |

## Six questions for the audit

1. **Is the 3-second felt latency actually explainable by network floor from Karachi (Karachi→OpenAI Iowa RTT ~600-800ms + Karachi→Twilio US→PK carrier ~700-900ms) or is there hidden dead time in our code we haven't found?** Please look for:
   - Any sync I/O inside async paths beyond the sqlite fix we just shipped
   - Serialized paths that could be parallelized (e.g. TTS starts after full LLM completion vs. streaming)
   - Buffers/queues holding data longer than needed
   - Any timer/sleep pattern we haven't spotted

2. **Twilio Media Streams "TWILIO_FIRST40_ACK send_to_ack_ms" is consistently 400-800ms.** Is this fundamental to Twilio's US1 region serving a PK caller, or is there a Media Streams config knob (e.g. "audio codec fastpath", "bidirectional session ordering") that would reduce it?

3. **The "Who am I talking with?" turn takes ~1 second of pure LLM roundtrip.** With gpt-4o-mini + 98% prompt-cache hit rate, is 800-1000ms first-token from Karachi to Iowa the realistic floor, or is there provider tuning we're missing (service_tier=priority, HTTP/2 keepalive verification, prefill hints, etc.)?

4. **The Flux endpointing is now ~90-130ms — is that at the physical floor, or is there a way to fire on `EagerEndOfTurn` for confident short utterances (like "who am I talking to?") that would let us start LLM work BEFORE Final?** We have Eager .4 / Final .7 configured but haven't wired speculation properly.

5. **Do you see any architectural pattern in the codebase (see zip) that would let the entire pipeline run at a lower floor without the US-East migration?** Or is the migration the only lever left?

6. **What's the fastest realistic single-turn latency achievable from Karachi with an OpenAI-compatible provider?** We benched Groq/Mistral/Fireworks/NVIDIA earlier — Mistral's `ministral-3b-latest` had p50=452ms first-byte (vs OpenAI's 637ms) but rate-limits on free tier. Is there a provider with paid tier + South Asia POP that beats OpenAI?

## Please tell us in your response

- What to ship next (concrete file changes)
- Whether US-East migration is inevitable or optional
- Any low-hanging fruit we've missed
- Whether we should consider Twilio ConversationRelay (managed) as a benchmark control

## Zip contents

- 660+ Python files across `apps/api`, `packages/`, `scripts/`
- All prior audit docs under `docs/` including your prior audits
- Current live call logs are NOT included (data/logs excluded from bundle)
- `.env` is NOT included (secrets)
- `.env.example` IS included for env-var visibility
