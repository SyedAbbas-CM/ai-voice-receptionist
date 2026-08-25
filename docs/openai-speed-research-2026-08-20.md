# OpenAI TTFT Reduction — Research Findings

**Question:** we're stuck with OpenAI. Fast tier gave us 2x on the real prompt (1534→772ms). What else can drop OpenAI's actual first-token latency (not just perceived)?

**Scope:** pure-speed levers first — real TTFT reduction, not caching (though caching is noted at the end). Ranked by impact × ease. No code changes yet.

**Session ref:** Oliver's call CAa8d6d3d6751eea6856cb18b53c0ed7c2 had LLM first-token 1900-2200ms. Bench at `docs/llm-ttft-bench-2026-08-20_012206.md` measured openai=1534ms, openai-fast=772ms on the real 24k-char prompt.

---

## Ranking — real TTFT levers

| # | Lever | Est TTFT drop | Ease | Total impact |
|---|---|---|---|---|
| 1 | Cut output tokens (max_tokens + prompt "reply in one short sentence") | Not TTFT, but ~50% total-response time | Trivial (config + 1 prompt line) | HIGH |
| 2 | Predicted Outputs (`prediction` param) | 15-40% on turns where opener is predictable | Small (~1 hour, needs sentence-buffer coordination) | HIGH |
| 3 | Trim the system prompt from 24k → ~8k chars | 20-35% (less input to process before first token) | Medium (~2-3 hours; touches load-bearing sections) | HIGH |
| 4 | Route to a smaller model when the turn is simple (gpt-4o-mini already smallest chat; consider gpt-4.1-nano) | Uncertain — needs bench, may hurt quality | Small (config) | MEDIUM |
| 5 | OpenAI Realtime API (`gpt-realtime`, WebSocket S2S) | Cuts our whole STT+LLM+TTS stack to ~200ms machine latency | LARGE rewrite — WebRTC preferred, replaces Deepgram+ElevenLabs | HUGE but 1-2 weeks |
| 6 | Persistent OpenAI Responses WS (already scaffolded, off) | 100-300ms (TLS + keep-alive per turn) | Medium (~4-6 hours) | MEDIUM |
| 7 | Prompt caching (auto on prefix ≥1024 tokens, byte-stable) | Up to 80% TTFT on cache hits — but only turn 2+ of a session | Free / auto | HIGH but not pure-speed |

---

## Lever 1 — cut output tokens (BEST easy win)

**What:** OpenAI's own latency guide: "generating tokens is almost always the highest latency step. Cutting 50% of output tokens cuts ~50% of latency."  Our current `max_tokens=300` in the streaming path; typical replies use 150-250 tokens.

**Why it hits our numbers:** Fast tier drops the input-side wait; output generation is now the bulk of total-response. Shorter replies = less time until the full sentence is streamed to TTS = caller hears complete thought faster. **The subtodealz prompt already enforces this** ("keep responses short: 20-30 words max per turn").

**Impact:** doesn't shorten TTFT (first-token), but shortens total-response by ~50%. For voice UX, "complete sentence heard" is what matters more than "first byte."

**Concrete:** lower `max_tokens` to 120 (roughly 30 words). Add prompt rule: "Never write more than 20-30 words per turn unless reading back a booking confirmation."

**Ease:** 1 config + 1 prompt line. ~5 min.

---

## Lever 2 — Predicted Outputs (real pure-speed lever)

**What:** OpenAI's `prediction` parameter on Chat Completions. If you know most of what the reply will start with, pass it as a hint. Model uses speculative decoding against the hint. Actual TTFT drops because the model's first tokens are "confirmed" from the prediction rather than generated from scratch.

**Why it fits voice-agent:** our replies START predictably. Acknowledgments ("Sure!", "Got it!", "Of course!"), backchannels ("Mm-hmm.", "Yeah."), and short greeters ("Hi there!", "Thanks!") represent 30-50% of turns. On those, we can predict the FIRST 3-8 tokens with high confidence.

**How to use it:** on each turn dispatch, based on turn intent (from `SemanticPlan.operation` — which T-SP1 already emits when the LLM uses the tool):
- `GREET` → prediction = "Hey there! "
- `ACKNOWLEDGE` → prediction = "Sure! "
- `CONFIRM_ACTION` → prediction = "Perfect, "
- `APOLOGIZE` → prediction = "Sorry about that — "
- `NEUTRAL` → no prediction

If the model deviates from the prediction, it costs slightly more (billed on rejected tokens too), but latency wins on hits are documented at 15-40%.

**Impact:** on the ~40% of turns where we can predict the opener correctly, first-token drops from ~770ms (Fast tier) to ~450-600ms.

**Constraint:** does NOT work with audio-in/out modalities. But we're using Chat Completions with text I/O (TTS happens separately), so we're fine.

**Ease:** ~1 hour. Add a `prediction` param based on turn intent. Wire from SemanticPlan.operation to a small mapping table.

---

## Lever 3 — Trim the system prompt from 24k → ~8k chars

**What:** the model has to READ the full input before it can generate the first token. Bigger prompt = longer input-pass = higher TTFT. Our system prompt is 24k chars (~6k tokens); we can probably cut to ~8k chars (~2k tokens) by trimming redundant restatements and examples.

**Why it's real speed (not just cache):** input processing time scales with prompt length. Even on a cache HIT, the SUFFIX that changes each turn (transcript, tools payload) still needs to be processed. On a cache MISS (first turn of every call), the full input matters.

**Impact:** 20-35% TTFT drop on cold-cache turns. Also helps cache-hit turns because rebuilt prefix is smaller.

**Constraint:** load-bearing sections must survive. Those cover ~5k chars (TIME HANDLING, BOOKING RULES, PHONE, COMPLIANCE, HALLUCINATION). Cuts have to come from EXAMPLES, HOW YOU ACTUALLY TALK (which ChatGPT is rewriting for humanness anyway), and the multi-restatement of PERSONA.

**Ease:** ~2-3 hours BUT overlaps entirely with the ChatGPT humanness rewrite that's coming. Do it as PART of that rewrite, not before.

---

## Lever 4 — smaller / faster OpenAI model

**What:** try `gpt-4.1-nano` on the streaming path. Released April 2025, positioned as fastest of the 4.1 family. Some benchmarks report ~30-40% lower TTFT than gpt-4o-mini for short-completion use cases.

**Why:** gpt-4o-mini is small but not THE smallest. gpt-4.1-nano is specifically positioned for latency-critical apps.

**Risk:** quality regression on tool-calling and complex reasoning. Would need a routing rule — simple turns to nano, complex to gpt-4o-mini.

**Impact:** uncertain until bench. Could be 30% TTFT drop OR could be a quality mess.

**Ease:** small config + bench. ~1 hour to test.

**Recommendation:** bench it standalone (extend `llm_ttft_bench.py`) BEFORE wiring.

---

## Lever 5 — OpenAI Realtime API (the big one)

**What:** `gpt-realtime` is a native speech-to-speech model. Ingests audio tokens, emits audio tokens. Eliminates STT+LLM+TTS as three separate hops. Docs claim 190ms end-to-end machine latency, real-world benchmarks report 200-500ms in production, ~800ms E2E TTFT for the current `gpt-realtime-1.5` (April 2026).

**Why:** the fastest legitimate path to sub-second voice AI. Cuts out the entire STT/TTS latency budget (currently ~600ms combined).

**Cost:** rewrites the audio pipeline. WebRTC preferred (better than WebSocket for real-time audio per OpenAI docs — WebSocket has TCP head-of-line blocking, WebRTC uses UDP + drops late packets). Replaces Deepgram STT + ElevenLabs TTS entirely. Loses fine-grained control (voice choice constrained to OpenAI's list, custom SSML lost).

**Impact:** if we did this, we'd hit sub-second demos routinely.

**Risk:**
- Voice options are limited to OpenAI's voices — losing "Sarah" or whatever ChatGPT recommends
- Tool calling on Realtime is different API surface
- Loses the SpeechCommitGate + wait-promise guarantees we've built
- Cost per minute is significantly higher

**Recommendation:** **defer**, but flag for a "premium fast lane" if a specific client demands sub-second.

**Ease:** 1-2 weeks work. Major re-architecture.

---

## Lever 6 — Persistent OpenAI Responses WS

**What:** `openai_persistent_ws_enabled` config flag exists but is off. Keeps a warm WS connection to `wss://api.openai.com/v1/responses` instead of a fresh HTTP request per turn.

**Why:** eliminates TLS handshake (~100-200ms from Karachi to us-east-1) per turn. First-turn cost of any call is the biggest single-request TLS penalty.

**Impact:** saves 100-300ms per turn depending on network. Bigger win from Pakistan than from US.

**Ease:** ~4-6 hours (scaffolded but needs proper tool continuation wiring; ChatGPT's earlier audit called it out as incomplete).

**Recommendation:** LATER. Fast tier + Predicted Outputs + prompt trim will get us most of the way; then persistent WS is polish.

---

## Lever 7 — Prompt caching (deeper detail)

**What:** OpenAI automatically caches prefixes ≥1024 tokens when they're byte-identical across requests. No code change. 50% input token cost discount + up to 80% TTFT reduction on cache hits.

**Why partial pure-speed:** caching only helps turn 2+ of a session (cold start on turn 1). But under load with multiple concurrent callers, the SYSTEM PROMPT (24k chars, byte-stable across ALL sessions of the same business) benefits from cache hits across calls too. **This IS pure speed for turn 1 IF another call has recently populated the same prefix.**

**Cache mechanics (from OpenAI docs):**
- Prefix cache starts at 1024 tokens, grows in 128-token increments
- Cache TTL: default 5-10 min in-memory (idle) + always cleared within 1 hour of last use
- `prompt_cache_retention="24h"` param extends TTL to 24h (KV tensors → GPU-local storage). **We should set this** because a business's system prompt doesn't change
- `prompt_cache_key=<string>` param: routing hint that pins requests to the same backend. **Should be tenant_id or business_id** — same business = same backend = higher hit rate

**How to measure hits:**
- Response usage field: `usage.prompt_tokens_details.cached_tokens` (Chat Completions)
- Alt: `usage.input_tokens_details.cached_tokens` (Responses API)
- Log both cached + total prompt tokens per call → cache hit rate = cached / total

**Ease:** already on. Add `prompt_cache_key` + `prompt_cache_retention="24h"` params (~5 min). Add telemetry (~15 min).

**Recommendation:** **SHIP THIS ALONGSIDE LEVER 1.** Cost is 20 min, impact under concurrent load is real, telemetry lets us measure everything else honestly.

---

## Lever 8 — Warm structured-output schemas at boot (new finding)

**What:** OpenAI compiles JSON schemas into constrained grammars on first use. Docs report **200-400ms first-call latency penalty** on a new schema; subsequent calls are cached.

**Why voice-agent hit:** we have `emit_semantic_plan` (T-SP1) as a structured-output-shaped tool. The FIRST caller in any session eats the schema-compile penalty on top of everything else. Boot-time warmup with a dummy request eliminates it.

**Bench-worthy claim:** the community forum reports first-schema calls range from 200ms to 60 seconds in bad cases. We should verify with our own bench.

**Ease:** ~30 min. Send a dummy request with our tool schemas at server boot (right after `router: initialized` in the warmup sequence). Ignore the response.

**Impact:** eliminates a variable 200-400ms first-turn tax on every call.

**Recommendation:** ship with the other cache work. Both are one-time boot hooks + verification.

---

## Recommendation — ship this order

Given "pure speed" priority, and constraint that ChatGPT is rewriting the prompt for humanness soon:

### Ship NOW (before ChatGPT's prompt lands)
1. **Lever 1** — cut `max_tokens` from 300 → 120 in streaming path. ~5 min. Cuts total-response ~50%.
2. **Lever 7 (caching enhancements)** — add `prompt_cache_key=<business_id>` + `prompt_cache_retention="24h"` params + `cached_tokens` telemetry. ~30 min. Under concurrent load this is real turn-1 speed on subsequent callers.
3. **Lever 8 (schema warmup)** — send dummy request with our tool schemas at boot. ~30 min. Eliminates 200-400ms first-turn penalty for structured outputs.
4. **Lever 2 (Predicted Outputs)** — on GREET / ACKNOWLEDGE / CONFIRM / APOLOGIZE intents. ~1 hour. Requires T-SP1 SemanticPlan to be firing (verify on next call). 40% of turns drop to ~450-600ms.

### Ship WITH ChatGPT's humanness rewrite
5. **Lever 3** — trim system prompt from 24k → 8k chars while ChatGPT's rewrite is being merged. 20-35% TTFT drop.

### Bench BEFORE deciding
6. **Lever 4** — extend `llm_ttft_bench.py` to include `gpt-4.1-nano`. If it's 30% faster with acceptable tool-calling quality, wire routing.

### Later
7. **Lever 6** — persistent WS. When we're already sub-1s and want polish.

### Deferred
8. **Lever 5** — Realtime API. Reserve for a "premium fast lane" if a specific client asks for sub-500ms.

---

## What this DOESN'T get us

- **Sub-500ms across the board.** That requires Realtime API.
- **First-turn TTFT parity with cached turns.** Cold start is always the worst.

## What this DOES get us realistically

- Fast tier alone: 1534 → 772ms (2x, already shipped)
- + max_tokens shrink: 772ms first-byte unchanged, but TOTAL response time down ~40-50%
- + Predicted Outputs on ~40% of turns: those turns drop 772 → ~450-600ms
- + prompt trim (with ChatGPT's rewrite): 772 → ~500-650ms
- Combined: **p50 across all turns should land at ~600-750ms** — sub-1s territory, sellable

---

## Sources

- [OpenAI Fast mode | OpenAI API](https://developers.openai.com/api/docs/guides/priority-processing) — "up to 2.5x faster speeds and more consistent latency"
- [Fast mode FAQ | OpenAI Help Center](https://help.openai.com/en/articles/11647665-fast-mode-faq) — renamed from Priority on 2026-07-30
- [Predicted Outputs | OpenAI API](https://platform.openai.com/docs/guides/predicted-outputs) — supported on gpt-4o, gpt-4o-mini, gpt-4.1 family
- [Speed Up OpenAI API Responses With Predicted Outputs](https://cobusgreyling.medium.com/speed-up-openai-api-responses-with-predicted-outputs-3a2285fff261) — real-world usage patterns
- [Latency optimization | OpenAI API](https://developers.openai.com/api/docs/guides/latency-optimization) — "cutting 50% of output tokens ~= 50% of latency"
- [OpenAI Realtime API voice agents production guide 2026](https://www.forasoft.com/blog/article/openai-realtime-api-voice-agent-production-guide-2026) — 190ms E2E claim, WebRTC vs WebSocket
- [OpenAI Realtime API Cuts Voice Agent Latency 25% (TechTimes 2026-07-07)](https://www.techtimes.com/articles/319860/20260707/openai-realtime-api-cuts-voice-agent-latency-25-adds-reasoning-mini-model.htm)
- [How to Optimize Voice Agent Latency: 12 Techniques for 2026](https://futureagi.com/blog/how-to-optimize-voice-agent-latency-2026/) — prompt caching + speculative decoding
- [Prompt caching | OpenAI API](https://developers.openai.com/api/docs/guides/prompt-caching) — automatic on prefixes ≥1024 tokens, up to 80% TTFT drop on cache hits
- [Talking to Machines: Low-Latency Voice Agents with OpenAI Realtime API (DEV Community)](https://dev.to/deepak_mishra_35863517037/talking-to-machines-building-low-latency-voice-agents-with-openai-realtime-api-3c7p)
- [OpenAI Prompt Caching: A Deep Dive (Portkey)](https://portkey.ai/blog/openais-prompt-caching-a-deep-dive/) — `prompt_cache_key` + `prompt_cache_retention` params, `cached_tokens` telemetry field
- [Prompt Caching 101 (OpenAI Cookbook)](https://cookbook.openai.com/examples/prompt_caching101) — 1024-token minimum, 128-token increments, TTL details
- [Structured Outputs tokens and latency (OpenAI Developer Community)](https://community.openai.com/t/structured-outputs-tokens-and-latency/900927) — 200-400ms first-call schema-compile penalty
- [Introducing Structured Outputs in the API (OpenAI blog)](https://openai.com/index/introducing-structured-outputs-in-the-api/) — how the schema constraint compilation works
- [Solving Voice AI Latency: 5 Seconds to Sub-1 Second](https://medium.com/@reveorai/solving-voice-ai-latency-from-5-seconds-to-sub-1-second-responses-d0065e520799) — target TTFT under 200ms for real-time voice
- [Fast mode for API Customers | OpenAI](https://openai.com/api-fast-mode/) — Fast mode pricing + when to use it
- [AI Model Latency Benchmarks 2026: TTFT & TPS Data (Digital Applied)](https://www.digitalapplied.com/blog/ai-model-latency-benchmarks-2026-ttft-throughput)
