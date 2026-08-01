# Latency Budget · One Caller Turn

Where the 700 ms goes on a typical booking turn. Numbers are P50 from
Coval and vendor benchmarks (Cartesia Sonic-3 May 2026, Deepgram Nova-3
Feb 2026, Groq Llama-3.3-70B production traces).

| Stage | Component | P50 | Cumulative | Why |
|---|---|---:|---:|---|
| 1 | Twilio · Vapi transport | ~40 ms | 40 ms | PSTN → WebRTC bridge |
| 2 | Deepgram Nova-3 STT (first partial) | 150 ms | 190 ms | Streaming, no upload wait |
| 3 | Input Guard (regex) | < 1 ms | 191 ms | Pure Python, no I/O |
| 4 | Groq Llama-3.3-70B first token | 300 ms | 491 ms | Fastest LPU inference in prod |
| 5 | Tool call (in-memory) | 20-80 ms | ~530 ms | FakeCalendar / vertical tool |
| 6 | Cartesia Sonic-3 first audio byte | 188 ms | 719 ms | SSE streaming, sonic-3 |
| **Total** | | | **~720 ms** | Well under the 800 ms industry-standard "feels natural" threshold |

## What we cut vs baseline

| Was | Now | Δ |
|---|---|---:|
| Local Whisper small.en on CPU | Deepgram Nova-3 streaming | **−1500 to −3000 ms** |
| Chatterbox MLX on MPS | Cartesia Sonic-3 SSE | **−2000 to −3500 ms** |
| Cold TTS on turn 1 | Pre-warmed greeting cache | **−2000 to −3000 ms** on greeting |

## What blows the budget

- **First LLM call on a cold Groq session** — can be 5-10 s if the specific model hasn't been served recently. Fallback to Gemini kicks in after 8 s.
- **Multi-intent utterances** — when the caller packs 3 requests into one, the brain currently serializes tool calls instead of parallelizing. Sprint 3d fixes this (~2× wall-clock win).
- **Tools that hit real integrations** — Toast, Athena, Stripe API round-trips add 200-500 ms each. Cache aggressively.
