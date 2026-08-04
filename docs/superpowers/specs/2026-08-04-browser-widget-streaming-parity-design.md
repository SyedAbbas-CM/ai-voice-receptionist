# Browser Widget — Streaming Parity + Live Observability

**Date:** 2026-08-04
**Author:** Syed Abbas (with Claude)
**Status:** Design — awaiting user review

## Problem

The Twilio compliance team has frozen outbound voice calls on the account.
The team must still be able to exercise and debug the streaming intelligence
pipeline that was wired up in Sprint 10 (StreamingSTTBridge, TurnManager,
heard-text reconciler, dialogue kernel, two-planner, VPL, capability-aware
routing).

The existing browser call widget at `/call` cannot exercise this pipeline. It
uses a REST batch flow (`POST /chat/start`, push-to-talk mic blob →
`POST /voice/stt`, one shot to `POST /chat/turn`, TTS via
`POST /voice/tts-stream`). Nothing about that flow goes through the
CallActor or any Sprint 10 component.

## Goal

Ship a second widget at `/call-stream` that behaves like a Twilio Media
Streams client: opens a WebSocket to the same `/twilio/stream` endpoint
the phone uses, streams 8 kHz µ-law microphone audio continuously, plays
back the µ-law audio the server sends, and lets the browser barge in mid
sentence. The server-side pipeline (`handle_twilio_stream_via_actor` and
everything downstream) is untouched — the browser is just a new client of
the existing protocol.

A second WebSocket, `/debug/live?call_id=…`, streams every classified event
for the active call from `call_event_log` to a side-panel in the browser
in real time. This is the observability surface. Every intelligence
failure the team wants to hunt is expected to become a visible, colored,
timestamped row here.

## Non-goals

- No changes to the phone (Twilio) path.
- No changes to any file under `packages/runtime/`, `packages/dialogue/`,
  `packages/voice/`, `packages/observability/failure_intelligence.py`,
  or any file that currently participates in the streaming intelligence
  pipeline.
- No changes to the existing `/call` widget. It stays as a working fallback.
- No multi-business selector. Widget uses the default tenant / default
  business, matching how inbound Twilio calls currently resolve tenant.
- No 16 kHz PCM mode. µ-law 8 kHz only, for exact parity with the phone
  path. The audio-quality tax buys single-code-path debug leverage.
- No auth on `/call-stream` or `/debug/live`. Both must be reachable
  from `http://localhost:8000` without an API key. They are dev-only
  and MUST NOT ship to production without adding auth first (see
  Deploy Notes).

## Architecture

```
Browser (/call-stream)                    FastAPI server                        External
─────────────────────                     ─────────────                         ────────
[Mic 48kHz PCM]
  │ AudioWorklet: downsample → 8kHz
  │ Custom µ-law encode
  │ Base64 wrap in {event:media,…}
  ▼
  WS #1 ────────────────────────────────► /twilio/stream ──► handle_twilio_stream_via_actor
                                                                  │
[Speaker AudioContext] ◄── {event:media} ◄── TwilioActorSession ──┤   (unchanged: CallActor,
[Chat bubble render]   ◄── {event:mark}  ◄── (streaming pipeline) │    StreamingSTTBridge,
                                                                  │    TurnManager, HeardText,
[Debug side-panel]     ◄──────────────── /debug/live?call_id=X ◄──┤    Dialogue Kernel, VPL,
                                          (subscription tap on       Two-planner, Router)
                                           call_event_log)            │
                                                                      ▼
                                                                Deepgram, Groq, ElevenLabs
```

## Components

### 1. `apps/call-stream/index.html`

Two-column layout.

Left column (call surface):
- Idle screen: business avatar, "Smile Dental Clinic" name, "Call" button,
  hint text ("Uses your browser mic and speakers · zero cost").
- Active screen: avatar with pulsing ring when agent speaks, business
  name, timer (mm:ss), status pill (`listening` / `agent speaking` /
  `thinking`), live transcript feed (agent + user bubbles), End button.
  No push-to-talk button — mic is always hot for the duration of the call.
- Ended screen: duration, turn count, "Call again" button.

Right column (debug panel):
- Always visible. Scrolling event feed. Color-coded by source (STT green,
  control blue, kernel purple, LLM yellow, error red). Each row shows:
  `HH:MM:SS.mmm  source.kind  <one-line summary>`. Auto-scrolls with a
  "pause auto-scroll" toggle at the top. Clear button.

### 2. `apps/call-stream/audio-pipe.js`

Isolated module. Exports one class, `AudioPipe`, with methods:
- `start(ws)` — attaches to an open WebSocket, requests mic, starts the
  worklet, begins upstream frames.
- `playMulawFrame(bytes)` — schedules a µ-law frame for playback.
- `handleClear()` — cancels every pending `AudioBufferSourceNode` and
  resets the scheduling clock. Called on Twilio `clear` event.
- `handleMark(name)` — the browser fires `mark_ack` back to the server
  when playback catches the end of a chunk that had a mark. This is what
  the ledger's `heard_text_for` needs to reconcile heard-text on interrupt.
- `stop()` — releases mic, stops worklet, closes AudioContext.

Uses `AudioContext.sampleRate` (not a hardcoded 48000) to compute the
downsample ratio so Safari (44.1 kHz) works.

Sends µ-law frames every 20 ms (160 samples at 8 kHz). Matches Twilio's
media frame cadence.

### 3. `apps/call-stream/session.js`

Manages the call lifecycle and the two WebSockets.

On "Call" click:
1. Generate `callId = "browser_" + crypto.randomUUID().slice(0,8)`.
2. Open `WS #1 = ws://<host>/twilio/stream`.
3. Send `{event:"connected", protocol:"Call", version:"1.0.0"}`.
4. Send `{event:"start", start:{streamSid:callId, callSid:callId, tracks:["inbound"], mediaFormat:{encoding:"audio/x-mulaw", sampleRate:8000, channels:1}, customParameters:{origin:"browser"}}}`.
5. Wait for AudioContext resume, request mic, `AudioPipe.start(ws1)`.
6. Open `WS #2 = ws://<host>/debug/live?call_id=twilio_<callId>` (server
   prefixes `twilio_` when it builds session ids — see
   `apps/api/app/routes/twilio.py:535`).
7. Wire debug events to the panel.

On server `{event:"media"}`: `pipe.playMulawFrame(...)`.
On server `{event:"mark"}`: `pipe.handleMark(name)`, and remember the
mark so the next `mark_ack` upstream can report which mark completed.
On server `{event:"clear"}`: `pipe.handleClear()`, flash a red "BARGE" pip
in the debug panel.
On server `{event:"stop"}`: end call cleanly.

On End button: send `{event:"stop"}` on WS #1, close both, transition
to ended screen.

Reconnect policy: one auto-reconnect attempt on WS #1 with 2 s backoff.
Fail → red banner, End call. WS #2 dropouts are non-fatal.

### 4. `WS /debug/live` in `apps/api/app/routes/debug.py`

New endpoint. ~40 lines.

```python
@router.websocket("/debug/live")
async def debug_live(ws: WebSocket, call_id: str) -> None:
    await ws.accept()
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)

    def _on_event(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop rather than block writer

    log.subscribe(call_id, _on_event)
    try:
        # Backfill recent events for this call so late openers see history
        for ev in log.timeline(call_id, limit=50):
            await ws.send_json(ev)
        while True:
            ev = await queue.get()
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    finally:
        log.unsubscribe(call_id, _on_event)
```

### 5. Subscribe / unsubscribe hooks in `packages/observability/call_event_log.py`

Small extension to the existing singleton. ~25 lines.

```python
class CallEventLog:
    def __init__(self, ...):
        ...
        self._subscribers: dict[str, set[Callable[[dict], None]]] = defaultdict(set)
        self._sub_lock = threading.Lock()  # writers may be on many threads

    def subscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].add(cb)

    def unsubscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].discard(cb)
            if not self._subscribers[call_id]:
                del self._subscribers[call_id]

    def write(self, event: dict) -> None:
        # ... existing SQLite insert ...
        cid = event.get("call_id")
        if cid:
            with self._sub_lock:
                subs = list(self._subscribers.get(cid, ()))
            for cb in subs:
                try:
                    cb(event)
                except Exception:
                    pass  # never let a bad subscriber break the writer
```

Zero effect on any existing writer. All existing tests remain valid.

### 6. Mount in `apps/api/app/main.py`

Matches the existing `/call` and `/simulator` mount pattern (mounts only
if the directory exists so tests without the folder don't break), with
one extra guard: refuse to mount when `ENVIRONMENT=production` unless
`ALLOW_DEBUG_WIDGETS=true` is explicitly set. This keeps the debug
event stream from being exposed on prod tunnels by accident.

## Data flow (one full call)

1. User clicks "Call". Browser generates `callId = "browser_a1b2c3d4"`.
2. WS #1 opens to `/twilio/stream`. Browser sends `connected` + `start`.
3. Server-side `handle_twilio_stream_via_actor` spawns `TwilioActorSession`.
   Session boots StreamingSTTBridge (Deepgram Nova-3), TurnManager,
   dialogue kernel. Greeting fires via `run_greeting`.
4. TTS µ-law bytes flow back as `{event:"media"}` frames. Each is 20 ms.
   Marks trail each chunk.
5. WS #2 opens to `/debug/live?call_id=twilio_browser_a1b2c3d4`.
   Server flushes the last N events for this call from
   `log.timeline(call_id, limit=50)` so late-openers see history, then
   live-tails via the subscription.
6. Browser's AudioPipe schedules the greeting µ-law for playback.
7. User interrupts. Mic worklet has been streaming µ-law upstream at
   50 fps the whole time. StreamingSTTBridge sees speech, TurnManager
   classifies INTERRUPTION. Actor emits `{event:"clear"}` to browser.
   AudioPipe cancels pending sources. Heard-text reconciler rewrites the
   assistant turn.
8. Debug panel shows the sequence: `stt.partial`, `stt.partial`,
   `control.interruption`, `ledger.reconcile`, `stt.final`,
   `control.end_of_turn`, `kernel.task_added`, `llm.route.pick`,
   `llm.response`, `vpl.compile`, `tts.chunk`. Each is timestamped
   and colored.
9. Call ends when user clicks End. Browser sends `{event:"stop"}` on
   WS #1. Server calls `end_session_async`. WS #2 receives a
   `call_ended` sentinel event and closes.

## Error handling

- **Mic permission denied**: red banner on idle, `Call` disabled with tooltip.
- **AudioContext requires user gesture (autoplay policy)**: resume inside
  the `Call` click handler before anything else.
- **AudioContext sampleRate ≠ 48000**: read `ctx.sampleRate` at start,
  compute downsample ratio dynamically. Never hardcode 48000.
- **WS #1 dies mid-call**: one reconnect attempt (2 s), replay `connected`
  + `start`. Second failure → red banner, force End.
- **WS #2 dies mid-call**: silent retry every 5 s in background. Panel
  shows "debug stream paused (reconnecting…)". Call is unaffected.
- **Server sends `clear` while queue is deep**: cancel every scheduled
  `AudioBufferSourceNode`, reset `nextStartAt`, flush any pending upstream
  buffer.
- **Tab backgrounded**: some browsers suspend the AudioContext or drop
  the mic. On visibility change, if AudioContext is suspended, call
  `ctx.resume()`. If mic track ends, force End and show "Mic dropped".
- **Server-side `_debug_live` queue backpressure**: bounded at 1000; drop
  on full rather than block. Panel will show missing sequence numbers.
- **Barge-in false positives**: this is the whole reason for the debug
  panel — user watches for false INTERRUPTIONS from noise and reports
  them. No automated handling in this spec.

## Testing

### Automated

- **Loopback smoke test** (`apps/api/tests/test_call_stream_widget.py`,
  ~30 lines): connects to both `/twilio/stream` and `/debug/live`, sends
  fake `connected` + `start` + a few silent media frames, asserts a
  greeting event and at least one `tts` event appear in the debug stream.
  Confirms plumbing without a real browser. Uses the existing FakeSTT /
  FakeTTS / FakeVAD fixtures from `test_twilio_actor.py`.
- **Subscribe/unsubscribe unit test** in
  `apps/api/tests/test_call_event_log.py`: two subscribers, one
  unsubscribes, write an event, assert only the remaining one gets it.
  ~20 lines.

### Manual

- Open `http://localhost:8000/call-stream` in Chrome + Safari + Firefox.
  Full call. Confirm audio round-trip, transcript rendering, debug feed.
- Interrupt the greeting at word 3. Confirm `clear` fires, playback stops
  crisply, debug panel shows `control.interruption` + `ledger.reconcile`.
- Ask "book me a cleaning next Thursday". Confirm end-to-end turn: STT
  finals, kernel task, LLM route, VPL compile, TTS playback.
- Backchannel test: say "uh huh" while agent is speaking. Confirm turn
  does NOT bump and playback continues.

Manual regression on the existing widget: open `/call` after the deploy,
confirm push-to-talk still works, no changes to its behavior.

## Deploy notes

- `/call-stream` and `/debug/live` are dev-only surfaces. Before this
  ever ships to a production host, add:
  1. Auth on both endpoints (the same `require_api_key` middleware
     already used on `/chat/*` etc).
  2. Rate limiting on `/debug/live` (one connection per session).
  3. A `PROD` env guard that refuses to mount `/call-stream` when
     `ENVIRONMENT=production` and no auth is present.

These are not part of this spec but are called out here so nobody
accidentally exposes the debug stream on a public tunnel.

## Estimate

- Server (subscribe hook + `/debug/live` endpoint + mount): ~2 h
- Browser (audio worklet, µ-law codec, session, panel, styles): ~6 h
- Tests: ~1 h
- Manual polish + cross-browser: ~2 h

Total: ~11 h of implementation. First working slice (audio round-trip,
no debug panel, no styling polish) should reach clickable-test within
3-4 h.
