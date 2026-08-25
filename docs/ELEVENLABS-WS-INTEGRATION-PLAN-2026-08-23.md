# ElevenLabs Multi-Context WebSocket Integration Plan

**Date:** 2026-08-23
**Owner:** networking (me)
**Priority:** ChatGPT audit rank #4 (100-200ms savings/turn)
**Status:** Planning — no code yet
**Blocker for build:** verify current HTTP first-byte with `TWILIO_FIRST_MEDIA_SENT` log after next call
**Related:** [[reliability-shipped-2026-08-14]], [[elevenlabs-ws-bench-finding]]

## Problem

Every TTS request opens a fresh HTTP POST to ElevenLabs and waits ~300ms for the first audio byte (measured in `scripts/bench_el_chunk_size.py`). That's TLS handshake + connection pool acquisition + EL's compute cold-start on each request.

For a call with 6 turns, that's 6 × 300ms = 1.8 seconds of avoidable connection overhead per call.

## Prior bench (do not repeat)

`docs/rnd-2026-08/` had a bench finding stored in memory: **ElevenLabs WS `/stream-input` was 5x SLOWER than HTTP `/stream` for full-text sends — only wins with token-by-token streaming.** [[elevenlabs-ws-bench-finding]]

**That bench measured the wrong endpoint for our use case.** Multi-context WebSocket (`multi-stream-input`) is a different API — designed exactly for our conversational agent pattern:
- One WS connection lives for the entire call
- Each turn opens a new "context" on the shared connection
- Context boundaries are the sentence boundaries our SentenceBuffer already produces
- No per-turn TLS/pool cost — that happened once at call start

## Architecture

### Current (HTTP per turn)

```
call start
    └─ (nothing)

turn N:
    LLM stream → SentenceBuffer → sentence
                                    ↓
    POST /v1/text-to-speech/{voice}/stream  (~300ms first-byte)
    ↑
    New TLS + pool acquire + EL cold path
    ↓
    audio bytes → Twilio WSS
```

### Proposed (WS multi-context)

```
call start
    └─ Open WS to /v1/text-to-speech/{voice}/multi-stream-input?...
       Send initial config (voice_settings, output_format=ulaw_8000)
       Keep warm for the whole call

turn N:
    LLM stream → SentenceBuffer → sentence
                                    ↓
    Send {"context_id": f"turn-{N}", "text": sentence} on existing WS
    ↑
    Zero handshake — WS already warm
    ↓
    Receive audio frames tagged with context_id → route to Twilio WSS

interruption on turn N:
    Send {"context_id": f"turn-{N}", "close_context": true}
    Start next turn on new context immediately
```

### Expected savings (per ChatGPT audit)

- **100-200ms** first-byte on turns 2..N per call
- Compounds across turn count — 6-turn call saves ~1s
- Turn 1 still pays TLS/WS-handshake once at call start (may be amortized behind LLM warmup)

## Files that will change

1. **`apps/api/app/providers/tts/elevenlabs_tts.py`** — add `MultiContextSession` class
2. **`apps/api/app/routes/twilio_actor.py`** — open WS at call start (in `TwilioActorSession.start()`), route sentences through session instead of HTTP, close on hangup
3. **`packages/core_agent/streaming.py`** — SentenceBuffer emits sentences that carry a context ID (probably just monotonic turn counter)

## Failure modes + fallbacks

1. **WS drops mid-call** → fall back to per-turn HTTP for the rest of the call. Log `EL_WS_DEGRADED_TO_HTTP call=... reason=...`. Don't retry-loop the WS — the caller is more sensitive to jitter than to per-turn cost.

2. **Context ID collision / EL returns audio for wrong context** → drop the mismatched audio, log `EL_WS_CONTEXT_MISMATCH`. Never speak audio not tagged for the current speech generation.

3. **WS handshake fails at call start** → skip WS entirely, use HTTP path. Log `EL_WS_HANDSHAKE_FAILED`. Don't block call start on this.

4. **EL sends binary + text frames interleaved** → parse text as JSON control frames (context lifecycle), forward binary as audio bytes.

## Data structures

```python
class MultiContextSession:
    """Owns one persistent WS to ElevenLabs for the duration of a call.
    Each turn opens a new context; audio is routed by context_id."""

    def __init__(self, api_key: str, voice_id: str, model: str,
                 output_format: str = "ulaw_8000"):
        ...
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._contexts: dict[str, asyncio.Queue] = {}  # context_id → audio Q
        self._reader_task: Optional[asyncio.Task] = None
        self._degraded: bool = False  # True → callers should use HTTP path

    async def open(self) -> bool:
        """Open the WS and start the reader loop.
        Returns True on success, False on failure (caller falls to HTTP)."""

    async def start_context(self, context_id: str) -> asyncio.Queue:
        """Register a new context. Returns the audio queue for it."""

    async def send_text(self, context_id: str, text: str,
                        flush: bool = False) -> None:
        """Send text for a context. Set flush=True on last sentence."""

    async def close_context(self, context_id: str) -> None:
        """Explicitly close a context (barge-in, cancellation)."""

    async def close(self) -> None:
        """Tear down the WS at call end."""
```

## Test approach

1. **Standalone bench:** `scripts/bench_el_multicontext_ws.py` — measures WS handshake time, per-context first-byte, cost per additional context. Compare to `bench_el_chunk_size.py` HTTP baseline.

2. **Unit tests:** mock the WS, verify context routing, verify degradation on drop, verify no cross-context audio leak.

3. **Live test on staging call:** use `LOG_LEVEL=debug` + tail for `EL_WS_*` events. Verify no `EL_WS_HANDSHAKE_FAILED` on 3+ consecutive calls.

## Rollout plan

1. Bench first (`bench_el_multicontext_ws.py`). If real savings < 50ms per turn on repeat calls, kill the project (same pattern as S1/S4 killed benches).

2. Build behind feature flag `settings.elevenlabs_use_multicontext_ws: bool = False`.

3. Ship with default OFF. Enable for one test call. Verify.

4. Enable in production only if 5 clean test calls in a row.

## Do NOT build yet

- Wait for voice-agent to finish NextActionPolicy wiring (their #1 above ours)
- Wait for one clean test call showing `TWILIO_FIRST_MEDIA_SENT` numbers so we can measure this against a fresh baseline
- Bench script must land before code — bench-first pattern

## References

- [ElevenLabs multi-context docs](https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input) (per ChatGPT audit)
- [Reducing latency guide](https://elevenlabs.io/docs/developer-guides/reducing-latency)
- Prior HTTP bench: `scripts/bench_el_chunk_size.py`
