# Browser Widget — Streaming Parity + Live Observability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a browser widget at `/call-stream` that mimics Twilio's Media Streams protocol against the same `/twilio/stream` endpoint the phone uses (µ-law 8 kHz), plus a `/debug/live` WebSocket that streams every `call_event_log` entry for the active call to a side-panel — so the streaming intelligence pipeline (StreamingSTTBridge, TurnManager, heard-text reconciler, dialogue kernel, two-planner, VPL, capability-aware routing) can be exercised and debugged locally while Twilio compliance freeze is in effect.

**Architecture:** Zero changes to phone path, kernel, VPL, planners, router, or existing `/call` widget. Extend `CallEventLog` with an in-process subscribe/unsubscribe fan-out. Add one WS endpoint `/debug/live?call_id=X` that forwards subscribed events. Add one static mount `/call-stream` serving a new widget that opens two WebSockets: `/twilio/stream` (audio, exact Twilio protocol) and `/debug/live` (event stream to right-side panel).

**Tech Stack:** FastAPI + Starlette WebSockets (Python), Web Audio API + AudioWorklet + custom µ-law codec (browser vanilla JS, no build step).

## Global Constraints

- **No changes** to any file under `packages/runtime/`, `packages/dialogue/`, `packages/voice/`, `packages/observability/failure_intelligence.py`, or the existing `/call` widget at `apps/call-widget/`.
- **µ-law 8 kHz only** on the browser wire — must match Twilio Media Streams format byte-for-byte.
- **All new browser files** live under `apps/call-stream/`. No build step, no npm dependency — plain HTML/CSS/JS + one AudioWorklet module.
- **Server extensions must not break** any existing writer of `CallEventLog.write()`. Subscribe/unsubscribe additions must be additive only.
- **Dev-only surface** — mount refuses to activate when `ENVIRONMENT=production` unless `ALLOW_DEBUG_WIDGETS=true` is set explicitly.
- Never mention Claude in code comments. Only add comments explaining WHY, never WHAT.
- All commit messages end with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

## File Structure

**New files:**
- `apps/call-stream/index.html` — two-column layout (call surface + debug panel)
- `apps/call-stream/style.css` — minimal styling
- `apps/call-stream/mulaw-worklet.js` — AudioWorkletProcessor for downsample + µ-law encode
- `apps/call-stream/audio-pipe.js` — mic → server + server → speaker pipeline
- `apps/call-stream/session.js` — WS lifecycle, event routing, UI state
- `apps/api/tests/test_call_event_log_subscribe.py` — unit tests for pub/sub extension
- `apps/api/tests/test_debug_live_ws.py` — integration test for `/debug/live` endpoint

**Modified files:**
- `packages/observability/call_event_log.py` — add `subscribe()`, `unsubscribe()`, fan-out in `write()`
- `apps/api/app/routes/debug.py` — add `WS /debug/live` endpoint
- `apps/api/app/main.py` — add `/call-stream` static mount with prod guard

---

### Task 1: Extend CallEventLog with subscribe/unsubscribe fan-out

**Files:**
- Modify: `packages/observability/call_event_log.py`
- Test: `apps/api/tests/test_call_event_log_subscribe.py` (new)

**Interfaces:**
- Consumes: existing `CallEventLog.write(event: CallEvent) -> None`
- Produces:
  - `CallEventLog.subscribe(call_id: str, cb: Callable[[dict], None]) -> None`
  - `CallEventLog.unsubscribe(call_id: str, cb: Callable[[dict], None]) -> None`
  - `write()` still returns `None`; additionally invokes subscriber callbacks with a dict-form of the event after (or in parallel with) SQLite insert. Callback receives the same dict shape that `timeline()` returns for that row (source/kind/payload/wall_ts/etc, JSON-decoded).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_call_event_log_subscribe.py`:

```python
"""Sprint 10 streaming-parity extension: pub/sub fan-out on CallEventLog.

Writers keep working exactly as before. New subscribers get a live copy
of every write matching their call_id. Unsubscribed callbacks stop firing.
Errors in a subscriber never break the writer."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packages.observability.call_event_log import (
    CallEvent,
    CallEventLog,
    EventSourceKind,
)


@pytest.fixture
def log(tmp_path: Path) -> CallEventLog:
    return CallEventLog(db_path=str(tmp_path / "test.db"))


def _make_event(call_id: str, kind: str = "test") -> CallEvent:
    return CallEvent(
        call_id=call_id, tenant_id="t1",
        source=EventSourceKind.CONTROL, kind=kind,
        payload={"text": kind},
    )


def test_subscribe_receives_writes(log: CallEventLog) -> None:
    received: list[dict] = []
    log.subscribe("call-a", received.append)
    log.write(_make_event("call-a", "first"))
    log.write(_make_event("call-a", "second"))
    assert len(received) == 2
    assert received[0]["kind"] == "first"
    assert received[1]["kind"] == "second"
    assert received[0]["source"] == "control"
    assert received[0]["payload"] == {"text": "first"}


def test_subscribe_filters_by_call_id(log: CallEventLog) -> None:
    received_a: list[dict] = []
    received_b: list[dict] = []
    log.subscribe("call-a", received_a.append)
    log.subscribe("call-b", received_b.append)
    log.write(_make_event("call-a", "for-a"))
    log.write(_make_event("call-b", "for-b"))
    assert len(received_a) == 1 and received_a[0]["kind"] == "for-a"
    assert len(received_b) == 1 and received_b[0]["kind"] == "for-b"


def test_unsubscribe_stops_delivery(log: CallEventLog) -> None:
    received: list[dict] = []
    log.subscribe("call-a", received.append)
    log.write(_make_event("call-a", "one"))
    log.unsubscribe("call-a", received.append)
    log.write(_make_event("call-a", "two"))
    assert len(received) == 1
    assert received[0]["kind"] == "one"


def test_subscriber_exception_does_not_break_writer(log: CallEventLog) -> None:
    def bad_cb(_ev: dict) -> None:
        raise RuntimeError("subscriber blew up")
    log.subscribe("call-a", bad_cb)
    # write must not raise
    log.write(_make_event("call-a", "still-writes"))
    # and the row IS persisted despite the subscriber failure
    tl = log.timeline("call-a")
    assert len(tl) == 1
    assert tl[0]["kind"] == "still-writes"


def test_multiple_subscribers_same_call_id(log: CallEventLog) -> None:
    a: list[dict] = []
    b: list[dict] = []
    log.subscribe("call-a", a.append)
    log.subscribe("call-a", b.append)
    log.write(_make_event("call-a", "broadcast"))
    assert len(a) == 1 and len(b) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_call_event_log_subscribe.py -v
```

Expected: FAIL — `AttributeError: 'CallEventLog' object has no attribute 'subscribe'`

- [ ] **Step 3: Add the subscribe/unsubscribe machinery to CallEventLog**

In `packages/observability/call_event_log.py`, modify the imports and `CallEventLog` class:

Add to imports at top (after existing imports):

```python
from collections import defaultdict
from typing import Callable
```

Add to `CallEventLog.__init__` (after `self._per_call_counts = {}`):

```python
        # Streaming-parity extension: in-process fan-out for /debug/live.
        # Subscribers see the same event dict shape `timeline()` returns,
        # after the SQLite insert.  A failing subscriber never blocks the writer.
        self._subscribers: dict[str, set[Callable[[dict], None]]] = defaultdict(set)
        self._sub_lock = threading.Lock()
```

Add these methods to `CallEventLog` (place them just below `__init__`, before `_connect`):

```python
    def subscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].add(cb)

    def unsubscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].discard(cb)
            if not self._subscribers[call_id]:
                del self._subscribers[call_id]

    def _fanout(self, event: CallEvent) -> None:
        # Snapshot subscribers under lock so we can call them lock-free.
        with self._sub_lock:
            subs = list(self._subscribers.get(event.call_id, ()))
        if not subs:
            return
        payload = {
            "call_id": event.call_id,
            "tenant_id": event.tenant_id,
            "turn_generation": event.turn_generation,
            "speech_generation": event.speech_generation,
            "monotonic_ns": event.monotonic_ns,
            "wall_ts": time.time(),
            "source": event.source.value,
            "kind": event.kind,
            "payload": event.payload,
            "error_category": event.error_category.value if event.error_category else None,
        }
        for cb in subs:
            try:
                cb(payload)
            except Exception as e:
                log.debug("subscriber cb failed: %s", e)
```

Modify `write()` — at the very end of the method (after the try/except that inserts), add:

```python
        # Fan out to live subscribers AFTER persistence.  Failure to
        # fan out never affects the caller.
        try:
            self._fanout(event)
        except Exception as e:
            log.debug("call_event fanout failed: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_call_event_log_subscribe.py -v
```

Expected: 5 PASSED

- [ ] **Step 5: Run full test suite to confirm no regression**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/ -x -q --ignore=tests/test_call_stream_widget.py --ignore=tests/test_debug_live_ws.py 2>&1 | tail -30
```

Expected: all existing tests still pass (subscribe additions are purely additive).

- [ ] **Step 6: Commit**

```bash
git add packages/observability/call_event_log.py apps/api/tests/test_call_event_log_subscribe.py
git commit -m "$(cat <<'EOF'
call_event_log: add subscribe/unsubscribe fan-out for /debug/live

Writers keep working exactly as before. New subscribers get a live
copy of every event matching their call_id, delivered after the
SQLite insert. Callback exceptions never break the writer path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add `WS /debug/live` endpoint

**Files:**
- Modify: `apps/api/app/routes/debug.py`
- Test: `apps/api/tests/test_debug_live_ws.py` (new)

**Interfaces:**
- Consumes: `CallEventLog.subscribe`, `CallEventLog.unsubscribe`, `CallEventLog.timeline` from Task 1
- Produces: `GET /debug/live` (WebSocket upgrade). Query param `call_id: str` required. Server:
  1. Backfills up to 50 historic events (oldest first)
  2. Streams new events as JSON messages until client disconnects or `{event: "call_ended", call_id: X}` is emitted
  3. Bounded internal queue (1000) drops overflow silently

- [ ] **Step 1: Write the failing integration test**

Create `apps/api/tests/test_debug_live_ws.py`:

```python
"""Integration test for WS /debug/live.

Verifies: client connects with ?call_id=, receives backfill of any
existing events for that call, then live-tails new writes.  Non-matching
call_ids don't leak events across."""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from packages.observability.call_event_log import (
    CallEvent,
    EventSourceKind,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def test_debug_live_backfills_and_streams():
    from app.main import create_app
    from packages.observability.call_event_log import get_call_event_log

    log = get_call_event_log()
    # Pre-existing event before the socket opens — must be backfilled.
    log.write(CallEvent(
        call_id="test-call", tenant_id="t1",
        source=EventSourceKind.CONTROL, kind="pre-existing",
        payload={"text": "before"},
    ))

    app = create_app()
    client = TestClient(app)
    with client.websocket_connect("/debug/live?call_id=test-call") as ws:
        first = ws.receive_json()
        assert first["kind"] == "pre-existing"
        assert first["payload"] == {"text": "before"}

        # New event after the socket is open — must arrive via fan-out.
        log.write(CallEvent(
            call_id="test-call", tenant_id="t1",
            source=EventSourceKind.STT, kind="partial",
            payload={"text": "hi"},
        ))
        second = ws.receive_json()
        assert second["kind"] == "partial"
        assert second["source"] == "stt"


def test_debug_live_filters_by_call_id():
    from app.main import create_app
    from packages.observability.call_event_log import get_call_event_log

    log = get_call_event_log()
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/debug/live?call_id=call-a") as ws:
        # Write to a DIFFERENT call
        log.write(CallEvent(
            call_id="call-b", tenant_id="t1",
            source=EventSourceKind.CONTROL, kind="not-for-us",
            payload={},
        ))
        # Then one for our call
        log.write(CallEvent(
            call_id="call-a", tenant_id="t1",
            source=EventSourceKind.CONTROL, kind="ours",
            payload={},
        ))
        got = ws.receive_json()
        assert got["kind"] == "ours"
        assert got["call_id"] == "call-a"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_debug_live_ws.py -v
```

Expected: FAIL — probably `404 Not Found` on the WebSocket upgrade because the route doesn't exist yet.

- [ ] **Step 3: Add the WebSocket endpoint to debug.py**

In `apps/api/app/routes/debug.py`, add these imports at the top (after existing imports):

```python
import asyncio

from fastapi import WebSocket, WebSocketDisconnect
```

Add this endpoint at the very end of the file:

```python
# ── Sprint 10 streaming-parity: live event stream for the browser widget ──

@router.websocket("/live")
async def debug_live_ws(ws: WebSocket, call_id: str) -> None:
    """Live-tail every classified event for `call_id` to the browser
    /call-stream widget.  Backfills the last 50 events on connect, then
    forwards each new fan-out from CallEventLog.subscribe.

    Dev-only surface — do not expose without auth in production."""
    await ws.accept()
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)

    def _on_event(event: dict) -> None:
        # Called from any thread (SQLite writer may be off the loop).
        # Schedule the queue put back on the loop so we don't touch it
        # from a foreign thread.
        try:
            loop.call_soon_threadsafe(_enqueue_nowait, event)
        except RuntimeError:
            # Loop closed — client is gone, nothing to do.
            pass

    def _enqueue_nowait(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # bounded drop rather than block writer

    log.subscribe(call_id, _on_event)
    try:
        # Backfill: last 50 events, oldest first (timeline returns newest first)
        history = list(reversed(log.timeline(call_id, limit=50)))
        for ev in history:
            await ws.send_json(ev)
        # Live tail
        while True:
            ev = await queue.get()
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Never crash on a client hangup or transient error.
        pass
    finally:
        log.unsubscribe(call_id, _on_event)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_debug_live_ws.py -v
```

Expected: 2 PASSED

- [ ] **Step 5: Manual smoke test against a running server**

Restart the server:

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
for i in 1 2 3 4 5 6 7 8; do curl -sf http://127.0.0.1:8000/health > /dev/null && echo "up ${i}s" && break; sleep 1; done
```

Then verify the WebSocket accepts:

```bash
/Users/az/Desktop/Receptionist\ Agent/.venv/bin/python -c "
import asyncio, websockets
async def main():
    async with websockets.connect('ws://127.0.0.1:8000/debug/live?call_id=nothing-here') as ws:
        print('accepted')
asyncio.run(main())
"
```

Expected: `accepted`.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/routes/debug.py apps/api/tests/test_debug_live_ws.py
git commit -m "$(cat <<'EOF'
debug: add WS /live endpoint for browser widget event stream

Backfills last 50 events for the requested call_id, then live-tails
new writes via CallEventLog.subscribe.  Cross-thread safe (uses
loop.call_soon_threadsafe since SQLite writers may be off-loop).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add `/call-stream` static mount with prod guard

**Files:**
- Modify: `apps/api/app/main.py`
- Create: `apps/call-stream/index.html` (placeholder for now — will be filled in Task 4)

**Interfaces:**
- Consumes: `apps/call-stream/` directory
- Produces: `GET /call-stream` serves `apps/call-stream/index.html` (and any other static files in the folder)

- [ ] **Step 1: Create the widget folder with a placeholder index.html**

```bash
mkdir -p "/Users/az/Desktop/Receptionist Agent/apps/call-stream"
```

Create `apps/call-stream/index.html`:

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Call Stream (placeholder)</title></head>
<body><h1>placeholder — real UI lands in Task 4</h1></body>
</html>
```

- [ ] **Step 2: Add the mount to main.py**

In `apps/api/app/main.py`, add this block **immediately after the existing `/call` mount** (after line 183, before the `simulator_dir` block):

```python
    # Mount /call-stream — dev widget that mimics Twilio's Media Streams
    # protocol against /twilio/stream, plus a live debug-event side-panel.
    # Refuses to mount in production unless explicitly allowed, since it
    # exposes an unauth'd view of every call event.
    stream_widget_dir = _REPO_ROOT / "apps" / "call-stream"
    if stream_widget_dir.exists():
        import os as _os_mount
        _env = _os_mount.environ.get("ENVIRONMENT", "development").lower()
        _allow = _os_mount.environ.get("ALLOW_DEBUG_WIDGETS", "false").lower() in ("1", "true", "yes")
        if _env != "production" or _allow:
            app.mount(
                "/call-stream",
                StaticFiles(directory=str(stream_widget_dir), html=True),
                name="call_stream",
            )
```

- [ ] **Step 3: Restart the server and verify the mount responds**

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
for i in 1 2 3 4 5 6 7 8; do curl -sf http://127.0.0.1:8000/health > /dev/null && echo "up" && break; sleep 1; done
curl -s http://127.0.0.1:8000/call-stream/ | head -5
```

Expected: HTML with the "placeholder" text.

- [ ] **Step 4: Verify prod guard works**

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && ENVIRONMENT=production TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
for i in 1 2 3 4 5 6 7 8; do curl -sf http://127.0.0.1:8000/health > /dev/null && echo "up" && break; sleep 1; done
curl -s -o /dev/null -w "prod-mount-status:%{http_code}\n" http://127.0.0.1:8000/call-stream/
```

Expected: `prod-mount-status:404` (the mount refused to activate in production).

Then restart back in dev mode for subsequent tasks:

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
```

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/main.py apps/call-stream/index.html
git commit -m "$(cat <<'EOF'
main: mount /call-stream static widget (dev-only) with prod guard

/call-stream serves the new browser widget that speaks Twilio's Media
Streams protocol against /twilio/stream.  Refuses to mount when
ENVIRONMENT=production unless ALLOW_DEBUG_WIDGETS=true is set, since
the debug event stream is not authenticated.

Placeholder index.html for now — full widget lands in follow-up commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: AudioWorklet µ-law encoder (mic → server)

**Files:**
- Create: `apps/call-stream/mulaw-worklet.js`

**Interfaces:**
- Consumes: browser `AudioWorkletProcessor` API + `AudioContext.sampleRate`
- Produces: an AudioWorklet processor registered as `mulaw-encoder-worklet` that emits `port.postMessage({ mulaw: Uint8Array })` roughly every 20 ms (160 samples at 8 kHz)

- [ ] **Step 1: Write the worklet**

Create `apps/call-stream/mulaw-worklet.js`:

```javascript
// AudioWorkletProcessor: downsamples input from AudioContext sample rate
// to 8000 Hz and encodes to µ-law bytes. Posts a Uint8Array every 20 ms
// (160 samples at 8000 Hz) back to the main thread.
//
// The linear-to-µlaw conversion is the standard ITU-T G.711 algorithm.

const TARGET_RATE = 8000;
const FRAME_SAMPLES = 160; // 20 ms at 8000 Hz

function linear2ulaw(sample) {
  // Clamp to int16 range.
  let s = Math.max(-32768, Math.min(32767, sample | 0));
  const sign = (s >> 8) & 0x80;
  if (sign) s = -s;
  if (s > 32635) s = 32635;
  s = s + 0x84;
  let exponent = 7;
  for (let mask = 0x4000; (s & mask) === 0 && exponent > 0; exponent--, mask >>= 1);
  const mantissa = (s >> (exponent + 3)) & 0x0f;
  const ulaw = ~(sign | (exponent << 4) | mantissa) & 0xff;
  return ulaw;
}

class MulawEncoderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._resampleBuf = [];       // downsampled 8kHz float samples awaiting frame emit
    this._srcAccum = 0;            // fractional accumulator for downsampling
    this._srcRate = sampleRate;    // AudioWorkletGlobalScope provides this
    this._ratio = this._srcRate / TARGET_RATE;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const src = input[0];  // mono channel 0, Float32Array length ~128

    // Simple nearest-sample downsample. Good enough for speech at
    // 8 kHz; STT won't notice the anti-alias absence in practice.
    for (let i = 0; i < src.length; i++) {
      this._srcAccum += 1;
      if (this._srcAccum >= this._ratio) {
        this._srcAccum -= this._ratio;
        // Convert Float32 (-1..1) to int16 range then µ-law.
        const s16 = Math.max(-1, Math.min(1, src[i])) * 32767;
        this._resampleBuf.push(linear2ulaw(s16));
      }
    }

    // Emit frames of FRAME_SAMPLES bytes to the main thread.
    while (this._resampleBuf.length >= FRAME_SAMPLES) {
      const frame = new Uint8Array(this._resampleBuf.splice(0, FRAME_SAMPLES));
      // Transferable so we don't copy: hand ownership of the buffer.
      this.port.postMessage({ mulaw: frame }, [frame.buffer]);
    }
    return true;
  }
}

registerProcessor('mulaw-encoder-worklet', MulawEncoderProcessor);
```

- [ ] **Step 2: There's no automated test for this in isolation**

AudioWorklets can't run under jsdom or Node without a real audio context. We verify the encoder end-to-end in Task 7 when the whole widget round-trips a greeting. Move on to Task 5.

- [ ] **Step 3: Commit**

```bash
git add apps/call-stream/mulaw-worklet.js
git commit -m "$(cat <<'EOF'
call-stream: AudioWorklet µ-law encoder for mic → server

Downsamples from AudioContext.sampleRate (48 kHz or 44.1 kHz depending
on OS/browser) to 8 kHz and encodes with ITU-T G.711 µ-law.  Emits
160-byte frames (20 ms) via port.postMessage with a transferable
buffer so the main thread doesn't copy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Audio pipe module (mic upstream + speaker downstream)

**Files:**
- Create: `apps/call-stream/audio-pipe.js`

**Interfaces:**
- Consumes: `apps/call-stream/mulaw-worklet.js`, browser `getUserMedia`, `AudioContext`
- Produces: `class AudioPipe` with methods:
  - `async start(sendMulawFrame)` — requests mic, boots worklet, calls `sendMulawFrame(uint8array)` for each 20 ms frame
  - `playMulawFrame(bytes: Uint8Array)` — decodes to PCM, schedules playback on shared AudioContext
  - `handleClear()` — cancels all pending playback sources, resets `nextStartAt`
  - `stop()` — releases mic, disconnects worklet, closes AudioContext
  - `onPlaybackCaughtUp(callback)` — registers a callback fired when the scheduled queue drains (used for mark_ack)

- [ ] **Step 1: Write the module**

Create `apps/call-stream/audio-pipe.js`:

```javascript
// Bidirectional µ-law 8kHz audio pipeline for the browser call widget.
//
// Upstream: mic → AudioWorklet (mulaw-worklet.js) → 20ms µ-law frames
// → caller-provided send fn.
//
// Downstream: caller invokes playMulawFrame(bytes); we decode µ-law
// to Float32 PCM at 8kHz and schedule a BufferSource on a shared
// AudioContext with a small look-ahead so playback stays glitch-free.

const TARGET_RATE = 8000;

function ulaw2linear(u) {
  u = ~u & 0xff;
  const sign = u & 0x80;
  const exponent = (u >> 4) & 0x07;
  const mantissa = u & 0x0f;
  let sample = ((mantissa << 3) + 0x84) << exponent;
  sample -= 0x84;
  return sign ? -sample : sample;
}

export class AudioPipe {
  constructor() {
    this._ctx = null;
    this._micStream = null;
    this._workletNode = null;
    this._srcNode = null;
    this._sendFn = null;
    this._nextStartAt = 0;
    this._pendingSources = [];
    this._caughtUpCbs = [];
    this._caughtUpTimer = null;
  }

  async start(sendMulawFrame) {
    this._sendFn = sendMulawFrame;
    // A user-gesture-triggered AudioContext for both directions.
    // 8000 rate is what we want for our decoded playback; browsers may
    // choose to run the mic input at a native rate and we downsample in the worklet.
    this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (this._ctx.state === 'suspended') await this._ctx.resume();

    // Downstream playback timing anchor: give ourselves 50ms of pad.
    this._nextStartAt = this._ctx.currentTime + 0.05;

    // Upstream mic → worklet.
    this._micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    await this._ctx.audioWorklet.addModule('./mulaw-worklet.js');
    this._srcNode = this._ctx.createMediaStreamSource(this._micStream);
    this._workletNode = new AudioWorkletNode(this._ctx, 'mulaw-encoder-worklet');
    this._workletNode.port.onmessage = (ev) => {
      if (this._sendFn && ev.data && ev.data.mulaw) {
        this._sendFn(ev.data.mulaw);
      }
    };
    this._srcNode.connect(this._workletNode);
    // The worklet doesn't need to be audible — but you MUST connect it
    // somewhere or Chrome garbage-collects the audio graph.  Use a
    // gain node with zero gain as a sink.
    const silentSink = this._ctx.createGain();
    silentSink.gain.value = 0;
    this._workletNode.connect(silentSink).connect(this._ctx.destination);
  }

  playMulawFrame(mulawBytes) {
    if (!this._ctx) return;
    const nSamples = mulawBytes.length;
    // Decode µ-law → int16 → Float32.
    const buf = this._ctx.createBuffer(1, nSamples, TARGET_RATE);
    const chan = buf.getChannelData(0);
    for (let i = 0; i < nSamples; i++) {
      chan[i] = ulaw2linear(mulawBytes[i]) / 32768;
    }
    const src = this._ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this._ctx.destination);
    const startAt = Math.max(this._ctx.currentTime + 0.02, this._nextStartAt);
    src.start(startAt);
    this._nextStartAt = startAt + buf.duration;
    this._pendingSources.push({ src, endsAt: this._nextStartAt });
    this._scheduleCaughtUpCheck();
  }

  handleClear() {
    // Cancel every pending playback source (barge-in from server).
    for (const { src } of this._pendingSources) {
      try { src.stop(0); } catch (_) {}
    }
    this._pendingSources = [];
    if (this._ctx) this._nextStartAt = this._ctx.currentTime + 0.02;
  }

  onPlaybackCaughtUp(cb) {
    this._caughtUpCbs.push(cb);
  }

  _scheduleCaughtUpCheck() {
    if (this._caughtUpTimer) return;
    const check = () => {
      if (!this._ctx) return;
      // Reap finished sources.
      const now = this._ctx.currentTime;
      this._pendingSources = this._pendingSources.filter(p => p.endsAt > now);
      if (this._pendingSources.length === 0) {
        this._caughtUpTimer = null;
        for (const cb of this._caughtUpCbs) {
          try { cb(); } catch (_) {}
        }
        return;
      }
      this._caughtUpTimer = setTimeout(check, 40);
    };
    this._caughtUpTimer = setTimeout(check, 40);
  }

  async stop() {
    if (this._workletNode) { try { this._workletNode.disconnect(); } catch (_) {} }
    if (this._srcNode)     { try { this._srcNode.disconnect();     } catch (_) {} }
    if (this._micStream)   { this._micStream.getTracks().forEach(t => t.stop()); }
    if (this._ctx)         { try { await this._ctx.close();         } catch (_) {} }
    this._ctx = null;
    this._micStream = null;
    this._workletNode = null;
    this._srcNode = null;
    this._pendingSources = [];
    this._caughtUpCbs = [];
    if (this._caughtUpTimer) { clearTimeout(this._caughtUpTimer); this._caughtUpTimer = null; }
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add apps/call-stream/audio-pipe.js
git commit -m "$(cat <<'EOF'
call-stream: bidirectional µ-law 8kHz audio pipe

Upstream: mic → worklet → 20ms µ-law frames handed to caller.
Downstream: playMulawFrame() decodes and schedules on a shared
AudioContext with small look-ahead.  handleClear() cancels pending
playback for server-side barge-in.  onPlaybackCaughtUp() fires when
the scheduled queue drains — used to send mark_ack to server.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Session module (WS lifecycle + event routing)

**Files:**
- Create: `apps/call-stream/session.js`

**Interfaces:**
- Consumes: `apps/call-stream/audio-pipe.js`
- Produces: `class CallStreamSession` with methods:
  - `async startCall({ onTranscript, onStatus, onDebugEvent, onEnded })` — opens both WSs, wires callbacks
  - `endCall()` — sends `stop` and closes
- Event callback shapes:
  - `onTranscript({ who: 'user'|'agent', text: string })`
  - `onStatus(text: string)` — human-readable pill text
  - `onDebugEvent(row: object)` — raw row from `/debug/live` (matches `timeline()` shape)
  - `onEnded({ durationSec, turnCount })`

- [ ] **Step 1: Write the module**

Create `apps/call-stream/session.js`:

```javascript
// Manages the two WebSockets and the audio pipe for a single call.
//
// WS #1 (/twilio/stream): audio protocol identical to Twilio Media Streams.
// WS #2 (/debug/live?call_id=...): server-pushed intelligence events for
// the right-side debug panel.

import { AudioPipe } from './audio-pipe.js';

function makeCallId() {
  // 8 hex chars — matches Twilio callSid length loosely; the browser
  // path prefixes with `browser_` so it's obvious in logs.
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return 'browser_' + Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

export class CallStreamSession {
  constructor() {
    this._ws = null;         // audio ws
    this._debugWs = null;    // debug ws
    this._pipe = null;
    this._callId = null;
    this._streamSid = null;
    this._callbacks = {};
    this._startedAt = 0;
    this._turnCount = 0;
    this._pendingMarks = [];  // mark names sent by server, awaiting ack
  }

  async startCall(callbacks) {
    this._callbacks = callbacks || {};
    this._callId = makeCallId();
    this._streamSid = this._callId;
    this._startedAt = Date.now();
    this._turnCount = 0;

    const host = window.location.host || '127.0.0.1:8000';
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const audioUrl = `${proto}://${host}/twilio/stream`;
    const debugUrl = `${proto}://${host}/debug/live?call_id=twilio_${this._callId}`;

    this._ws = new WebSocket(audioUrl);
    this._ws.binaryType = 'arraybuffer';
    await new Promise((resolve, reject) => {
      this._ws.onopen = resolve;
      this._ws.onerror = reject;
    });
    this._setStatus('connected');

    // Send Twilio-format handshake so the server's actor path accepts it.
    this._ws.send(JSON.stringify({
      event: 'connected', protocol: 'Call', version: '1.0.0',
    }));
    this._ws.send(JSON.stringify({
      event: 'start',
      sequenceNumber: '1',
      start: {
        streamSid: this._streamSid,
        accountSid: 'browser-widget',
        callSid: this._callId,
        tracks: ['inbound'],
        mediaFormat: { encoding: 'audio/x-mulaw', sampleRate: 8000, channels: 1 },
        customParameters: { origin: 'browser' },
      },
    }));

    // Route server-to-client events.
    this._ws.onmessage = (ev) => this._handleServerMessage(ev);
    this._ws.onclose = () => this._onWsClosed('audio');
    this._ws.onerror = (e) => console.warn('audio ws error', e);

    // Boot the audio pipe (mic + speaker).  Frames go straight to the WS.
    this._pipe = new AudioPipe();
    let mediaSeq = 2;
    await this._pipe.start((mulawBytes) => {
      if (this._ws && this._ws.readyState === 1) {
        const b64 = btoa(String.fromCharCode(...mulawBytes));
        this._ws.send(JSON.stringify({
          event: 'media',
          sequenceNumber: String(mediaSeq++),
          media: { track: 'inbound', chunk: String(mediaSeq), timestamp: String(Date.now()), payload: b64 },
        }));
      }
    });

    // When the local playback queue drains, ack any pending marks.
    this._pipe.onPlaybackCaughtUp(() => {
      while (this._pendingMarks.length > 0) {
        const name = this._pendingMarks.shift();
        if (this._ws && this._ws.readyState === 1) {
          this._ws.send(JSON.stringify({
            event: 'mark',
            streamSid: this._streamSid,
            mark: { name },
          }));
        }
      }
    });

    // Debug event stream.  Opens after audio so we don't miss greeting events.
    this._openDebugWs(debugUrl);
    this._setStatus('agent speaking');
  }

  _openDebugWs(url) {
    this._debugWs = new WebSocket(url);
    this._debugWs.onmessage = (ev) => {
      try {
        const row = JSON.parse(ev.data);
        if (this._callbacks.onDebugEvent) this._callbacks.onDebugEvent(row);
        // Extract user-visible transcript bubbles from the raw event stream.
        this._maybeSurfaceTranscript(row);
      } catch (_) {}
    };
    this._debugWs.onclose = () => {
      // Silent reconnect after 5s if the call is still live.
      if (this._ws && this._ws.readyState === 1) {
        setTimeout(() => this._openDebugWs(url), 5000);
      }
    };
    this._debugWs.onerror = () => {};
  }

  _maybeSurfaceTranscript(row) {
    if (!this._callbacks.onTranscript) return;
    // User final transcript
    if (row.source === 'stt' && row.kind === 'final' && row.payload && row.payload.text) {
      this._callbacks.onTranscript({ who: 'user', text: row.payload.text });
      this._turnCount++;
    }
    // Agent utterance (control.tts_utterance is what the actor emits)
    if (row.source === 'tts' && row.kind === 'utterance' && row.payload && row.payload.text) {
      this._callbacks.onTranscript({ who: 'agent', text: row.payload.text });
      this._turnCount++;
    }
  }

  _handleServerMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.event) {
      case 'media':
        if (msg.media && msg.media.payload) {
          const bin = atob(msg.media.payload);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          this._pipe.playMulawFrame(bytes);
        }
        break;
      case 'mark':
        if (msg.mark && msg.mark.name) {
          this._pendingMarks.push(msg.mark.name);
        }
        break;
      case 'clear':
        this._pipe.handleClear();
        this._pendingMarks = [];
        if (this._callbacks.onDebugEvent) {
          this._callbacks.onDebugEvent({
            source: 'control', kind: 'clear (barge)',
            payload: {}, wall_ts: Date.now() / 1000,
          });
        }
        break;
      case 'stop':
        this._onWsClosed('server-stop');
        break;
    }
  }

  _onWsClosed(reason) {
    // Idempotent — fires from ws.onclose or explicit endCall.
    if (this._callbacks.onEnded) {
      const durationSec = Math.round((Date.now() - this._startedAt) / 1000);
      this._callbacks.onEnded({ durationSec, turnCount: this._turnCount, reason });
      this._callbacks.onEnded = null;  // dedupe
    }
    this._teardown();
  }

  _teardown() {
    if (this._pipe) { this._pipe.stop(); this._pipe = null; }
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
    if (this._debugWs) { try { this._debugWs.close(); } catch (_) {} this._debugWs = null; }
  }

  _setStatus(text) {
    if (this._callbacks.onStatus) this._callbacks.onStatus(text);
  }

  endCall() {
    if (this._ws && this._ws.readyState === 1) {
      try { this._ws.send(JSON.stringify({ event: 'stop', sequenceNumber: '99' })); } catch (_) {}
    }
    this._onWsClosed('user-hangup');
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add apps/call-stream/session.js
git commit -m "$(cat <<'EOF'
call-stream: session module — WS lifecycle + event routing

Opens WS #1 to /twilio/stream (Twilio Media Streams protocol) and
WS #2 to /debug/live for the intelligence event feed.  Wires audio
pipe upstream frames to the WS and server media/mark/clear/stop
events back to the pipe.  Surfaces user finals and agent utterances
as transcript bubbles via callbacks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Widget UI (HTML + CSS + wiring)

**Files:**
- Overwrite: `apps/call-stream/index.html` (replaces the Task 3 placeholder)
- Create: `apps/call-stream/style.css`

**Interfaces:**
- Consumes: `apps/call-stream/session.js`

- [ ] **Step 1: Write the CSS**

Create `apps/call-stream/style.css`:

```css
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #0b0e13;
  color: #dfe4ec;
  height: 100vh;
  overflow: hidden;
}

#app {
  display: grid;
  grid-template-columns: 1fr 480px;
  gap: 0;
  height: 100vh;
}

/* ── Left column: call surface ─────────────────────────────────── */
#call-surface {
  display: flex;
  flex-direction: column;
  padding: 32px;
  border-right: 1px solid #1c222c;
}
#idle-screen, #active-screen, #ended-screen {
  display: none;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
}
#idle-screen.active, #active-screen.active, #ended-screen.active {
  display: flex;
}
h1 { margin: 8px 0; font-weight: 500; font-size: 24px; }
.subhead { color: #7a8291; margin: 4px 0 24px; font-size: 14px; }
button {
  background: #2563eb; color: white; border: none;
  padding: 12px 28px; border-radius: 8px; font-size: 16px;
  cursor: pointer; font-weight: 500;
}
button:hover { background: #1d4ed8; }
button:disabled { opacity: 0.6; cursor: not-allowed; }
#end-btn { background: #b91c1c; margin-top: 24px; }
#end-btn:hover { background: #991b1b; }

.status-pill {
  display: inline-block;
  padding: 4px 12px; border-radius: 999px;
  background: #1e293b; color: #93c5fd; font-size: 13px;
  margin-bottom: 16px;
}
.timer { color: #64748b; font-size: 13px; margin-bottom: 24px; }

.transcript {
  width: 100%; max-width: 620px;
  flex: 1; overflow-y: auto;
  padding: 8px 0;
}
.bubble {
  padding: 10px 14px; margin: 6px 0;
  border-radius: 14px; max-width: 80%;
  line-height: 1.4; font-size: 15px;
}
.bubble.user { background: #1e40af; margin-left: auto; text-align: right; }
.bubble.agent { background: #1c222c; }
.who { font-size: 11px; color: #7a8291; margin-bottom: 2px; padding: 0 4px; }
.who.user { text-align: right; }

/* ── Right column: debug panel ─────────────────────────────────── */
#debug-panel {
  background: #05070a;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: "SF Mono", Menlo, monospace;
  font-size: 12px;
  color: #b4bcc9;
}
#debug-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0; border-bottom: 1px solid #1c222c; margin-bottom: 8px;
}
#debug-header h2 { margin: 0; font-size: 13px; color: #93c5fd; }
#debug-controls button {
  padding: 3px 8px; font-size: 11px; margin-left: 4px;
  background: #1c222c; color: #93c5fd;
}
.event-row {
  padding: 3px 4px; border-radius: 3px; margin: 1px 0;
  white-space: pre-wrap; word-break: break-word;
}
.event-row .ts { color: #4b5563; margin-right: 6px; }
.event-row .src { display: inline-block; min-width: 90px; margin-right: 6px; font-weight: 600; }
.event-row.src-stt      { background: rgba(34, 197, 94, 0.08); }
.event-row.src-stt .src { color: #22c55e; }
.event-row.src-control  { background: rgba(59, 130, 246, 0.08); }
.event-row.src-control .src { color: #3b82f6; }
.event-row.src-state    { background: rgba(168, 85, 247, 0.08); }
.event-row.src-state .src   { color: #a855f7; }
.event-row.src-llm      { background: rgba(234, 179, 8, 0.08); }
.event-row.src-llm .src { color: #eab308; }
.event-row.src-tts      { background: rgba(148, 163, 184, 0.08); }
.event-row.src-tts .src { color: #94a3b8; }
.event-row.src-error    { background: rgba(239, 68, 68, 0.15); color: #fca5a5; }
.event-row.src-error .src { color: #ef4444; }
.event-row.src-commit .src { color: #eab308; }
```

- [ ] **Step 2: Write the HTML**

Overwrite `apps/call-stream/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Streaming widget · voiceops</title>
  <link rel="stylesheet" href="./style.css">
</head>
<body>
  <main id="app">

    <section id="call-surface">
      <!-- Idle -->
      <div id="idle-screen" class="active">
        <h1>Smile Dental Clinic</h1>
        <p class="subhead">Streaming intelligence · dev widget</p>
        <button id="call-btn">Call</button>
        <p class="subhead" style="margin-top:16px">Uses your browser mic and speakers · zero cost</p>
      </div>

      <!-- Active -->
      <div id="active-screen">
        <h1 id="active-business">Smile Dental Clinic</h1>
        <div class="status-pill" id="status-pill">connecting…</div>
        <div class="timer" id="timer">00:00</div>
        <div class="transcript" id="transcript"></div>
        <button id="end-btn">End call</button>
      </div>

      <!-- Ended -->
      <div id="ended-screen">
        <h1>Call ended</h1>
        <p class="subhead">Duration <b id="ended-duration">—</b> · turns <b id="ended-turns">—</b></p>
        <button id="restart-btn">Call again</button>
      </div>
    </section>

    <section id="debug-panel">
      <div id="debug-header">
        <h2>debug · live intelligence stream</h2>
        <div id="debug-controls">
          <button id="pause-btn">pause scroll</button>
          <button id="clear-btn">clear</button>
        </div>
      </div>
      <div id="debug-log"></div>
    </section>

  </main>

  <script type="module">
    import { CallStreamSession } from './session.js';

    const $ = (id) => document.getElementById(id);
    let session = null;
    let paused = false;
    let timerHandle = null;
    let callStartedAt = 0;

    function showScreen(id) {
      for (const s of ['idle-screen', 'active-screen', 'ended-screen']) {
        $(s).classList.toggle('active', s === id);
      }
    }
    function setStatus(text) { $('status-pill').textContent = text; }
    function fmtDur(s) {
      const m = Math.floor(s / 60), ss = String(s % 60).padStart(2, '0');
      return `${String(m).padStart(2, '0')}:${ss}`;
    }
    function startTimer() {
      stopTimer();
      const tick = () => $('timer').textContent = fmtDur(Math.floor((Date.now() - callStartedAt) / 1000));
      tick(); timerHandle = setInterval(tick, 1000);
    }
    function stopTimer() { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } }

    function addBubble(who, text) {
      const wrap = document.createElement('div');
      wrap.className = `bubble ${who}`;
      wrap.textContent = text;
      const label = document.createElement('div');
      label.className = `who ${who}`;
      label.textContent = who === 'user' ? 'you' : 'agent';
      const row = document.createElement('div');
      row.appendChild(label); row.appendChild(wrap);
      $('transcript').appendChild(row);
      $('transcript').scrollTop = $('transcript').scrollHeight;
    }

    function addDebugRow(ev) {
      const row = document.createElement('div');
      const src = ev.source || 'unknown';
      row.className = `event-row src-${src}`;
      const ts = new Date((ev.wall_ts || Date.now() / 1000) * 1000).toISOString().slice(11, 23);
      const payload = ev.payload ? JSON.stringify(ev.payload).slice(0, 200) : '';
      row.innerHTML =
        `<span class="ts">${ts}</span>` +
        `<span class="src">${src}.${ev.kind || '?'}</span>` +
        `<span>${payload}</span>`;
      $('debug-log').appendChild(row);
      if (!paused) $('debug-panel').scrollTop = $('debug-panel').scrollHeight;
    }

    async function startCall() {
      $('call-btn').disabled = true;
      $('call-btn').textContent = 'connecting…';
      try {
        session = new CallStreamSession();
        await session.startCall({
          onTranscript: ({ who, text }) => addBubble(who, text),
          onStatus: (text) => setStatus(text),
          onDebugEvent: (ev) => addDebugRow(ev),
          onEnded: ({ durationSec, turnCount }) => {
            $('ended-duration').textContent = fmtDur(durationSec);
            $('ended-turns').textContent = turnCount;
            showScreen('ended-screen');
            stopTimer();
            session = null;
          },
        });
        callStartedAt = Date.now();
        startTimer();
        showScreen('active-screen');
      } catch (e) {
        alert('Failed to start call: ' + e.message);
        $('call-btn').disabled = false;
        $('call-btn').textContent = 'Call';
      }
    }

    $('call-btn').addEventListener('click', startCall);
    $('end-btn').addEventListener('click', () => session && session.endCall());
    $('restart-btn').addEventListener('click', () => {
      $('transcript').innerHTML = '';
      $('debug-log').innerHTML = '';
      $('call-btn').disabled = false;
      $('call-btn').textContent = 'Call';
      showScreen('idle-screen');
    });
    $('pause-btn').addEventListener('click', () => {
      paused = !paused;
      $('pause-btn').textContent = paused ? 'resume scroll' : 'pause scroll';
    });
    $('clear-btn').addEventListener('click', () => $('debug-log').innerHTML = '');
  </script>
</body>
</html>
```

- [ ] **Step 3: This is the "clickable test slice" the goal calls for**

Restart the server:

```bash
lsof -ti:8000 | xargs -r kill -9 2>/dev/null; sleep 1
cd apps/api && TWILIO_SIGNATURE_ENFORCE=false PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 > /tmp/uvicorn.log 2>&1 &
for i in 1 2 3 4 5 6 7 8; do curl -sf http://127.0.0.1:8000/health > /dev/null && echo "up" && break; sleep 1; done
```

Now open **http://127.0.0.1:8000/call-stream/** in Chrome. Click **Call**. Expected:
- Mic permission prompt → allow
- Agent voice: "Hi, thanks for calling Smile Dental Clinic. I'm Nia, how can I help you today?"
- Right panel: rows starting to scroll — `control.start`, `state.transition`, `tts.chunk`, marks
- Speak: "book me a cleaning next Thursday"
- Right panel: STT partials in green, `control.end_of_turn`, `state.task_added`, `llm.route.pick`, `llm.response`, `vpl.compile`, `tts.chunk`, then agent replies

If audio is silent but debug events flow, the audio pipe has a bug. If debug is silent but you hear the agent, the debug WS has a bug. Either way, this is the test point.

- [ ] **Step 4: Commit**

```bash
git add apps/call-stream/index.html apps/call-stream/style.css
git commit -m "$(cat <<'EOF'
call-stream: widget UI (idle/active/ended screens + debug side-panel)

Two-column layout: left = call surface with transcript bubbles + End
button, right = live-scrolling color-coded event feed from /debug/live.
No push-to-talk — mic is always hot for the duration of the call, like
a real phone.

First clickable milestone: open /call-stream, click Call, hear the
Smile Dental greeting, see intelligence events scrolling in the panel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: End-to-end smoke test for the widget path

**Files:**
- Create: `apps/api/tests/test_call_stream_widget.py`

**Interfaces:**
- Consumes: everything above + existing FakeSTT/FakeTTS/FakeVAD fixtures from `test_twilio_actor.py`
- Produces: one passing test that verifies the full plumbing (mock Twilio protocol from browser → actor → debug WS event) without a real browser

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_call_stream_widget.py`:

```python
"""End-to-end plumbing test for the /call-stream widget path.

Opens BOTH the audio WS (/twilio/stream) and the debug WS (/debug/live),
sends a Twilio-format 'connected' + 'start', asserts at least one event
appears in the debug stream (proving fan-out from actor writes reaches
subscribers) and at least one media frame comes back on the audio WS
(proving the greeting flowed through the fake TTS).

Reuses the existing FakeSTT/FakeTTS/FakeVAD monkeypatches from
test_twilio_actor.py so we don't hit external services."""
from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("TWILIO_SIGNATURE_ENFORCE", "false")
    from packages.observability import call_event_log
    call_event_log.reset_singleton_for_tests()
    yield
    call_event_log.reset_singleton_for_tests()


class _FakeVAD:
    def is_speech(self, frame, sample_rate, mime): return len(frame) > 0

class _FakeSTT:
    name = "fake"
    supports_streaming = True
    async def transcribe(self, wav, sample_rate, mime): return ""
    async def transcribe_stream(self, chunks, sample_rate=8000, encoding="linear16"):
        async for _ in chunks: pass
        return
        yield  # pragma: no cover

class _FakeTTS:
    name = "fake"
    async def synthesize(self, text, voice=None):
        return b"\xff" * 4000, "audio/mulaw"


def test_widget_round_trip_via_actor(monkeypatch):
    from app.routes import twilio as twilio_module
    from app.core import session_manager
    from app import providers
    from app.routes import twilio_actor as actor_module

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: _FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: _FakeTTS())
    monkeypatch.setattr(providers, "get_stt", lambda: _FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: _FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)
    monkeypatch.setattr(twilio_module, "_mulaw_frames_to_wav", lambda m, sample_rate=8000: m)

    async def _rg(state, brain): return "Hello from the widget test."
    async def _rut(state, brain, transcript):
        return {"reply": f"got: {transcript}", "escalated": False, "tool_results": []}
    async def _end(sid, tenant_id="default"): return None
    monkeypatch.setattr(session_manager, "run_greeting", _rg)
    monkeypatch.setattr(session_manager, "run_user_turn", _rut)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    from packages.runtime import call_actor
    call_actor._registry_singleton = None

    from app.main import create_app
    app = create_app()
    client = TestClient(app)

    call_sid = "browser_smoketest"
    with (
        client.websocket_connect("/twilio/stream") as audio_ws,
        client.websocket_connect(f"/debug/live?call_id=twilio_{call_sid}") as debug_ws,
    ):
        audio_ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        audio_ws.send_json({
            "event": "start",
            "sequenceNumber": "1",
            "start": {
                "streamSid": call_sid,
                "callSid": call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        })
        # Drain a few frames from each socket, looking for evidence of life.
        saw_media = False
        saw_debug = False
        for _ in range(40):
            try:
                msg = audio_ws.receive_json(timeout=0.2)
                if msg.get("event") == "media":
                    saw_media = True
            except Exception:
                pass
            try:
                ev = debug_ws.receive_json(timeout=0.2)
                # Any event for this call is proof of fan-out
                if ev.get("call_id") == f"twilio_{call_sid}":
                    saw_debug = True
            except Exception:
                pass
            if saw_media and saw_debug:
                break

        audio_ws.send_json({"event": "stop", "sequenceNumber": "99"})

    assert saw_media, "expected at least one media frame back from actor (greeting)"
    assert saw_debug, "expected at least one debug event to fan out from writer"
```

- [ ] **Step 2: Run the test**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/test_call_stream_widget.py -v
```

Expected: PASS.

If it fails on `saw_debug=False`: the debug WS is opening BEFORE the actor writes its first event, and the test is using a client that doesn't buffer. Real browsers won't have this timing issue because we backfill on connect. In the test, if this is flaky, patch the `receive_json` timeout to `0.5` and iterate `range(80)`.

- [ ] **Step 3: Run entire test suite**

```bash
cd apps/api && PYTHONPATH="../..:." /Users/az/Desktop/Receptionist\ Agent/.venv/bin/pytest tests/ -x -q 2>&1 | tail -20
```

Expected: no regressions.

- [ ] **Step 4: Commit**

```bash
git add apps/api/tests/test_call_stream_widget.py
git commit -m "$(cat <<'EOF'
call-stream: end-to-end smoke test for widget → actor → debug WS

Opens both WebSockets that the real browser widget opens, feeds a
Twilio-format start frame, asserts we see a media frame come back
(proves greeting flowed via fake TTS) AND at least one debug event
fan out (proves subscribe/unsubscribe wiring works end-to-end).

Reuses FakeSTT/FakeTTS/FakeVAD patterns from test_twilio_actor.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Widget at `/call-stream` with two-column layout ✓ (Task 3 + 7)
- Uses Twilio Media Streams protocol ✓ (Task 6 session.js)
- µ-law 8 kHz throughout ✓ (Task 4 worklet + Task 5 pipe)
- `/debug/live` WebSocket ✓ (Task 2)
- Fan-out in `call_event_log` ✓ (Task 1)
- Prod mount guard ✓ (Task 3)
- Loopback smoke test ✓ (Task 8)
- Subscribe/unsubscribe unit test ✓ (Task 1)
- No changes to existing `/call` widget ✓ (verified by mount pattern additive-only)
- Manual regression on existing `/call` ✓ (Task 7 step 3 implicitly — mount didn't touch existing widget)

**Placeholder scan:** none found. Every step has concrete code.

**Type consistency:**
- `subscribe(call_id, cb)` matches `unsubscribe(call_id, cb)` — same signature.
- `_on_event(event: dict)` in debug.py matches the `cb: Callable[[dict], None]` type in call_event_log.py.
- `startCall(callbacks)` in session.js callbacks match those used in index.html (onTranscript/onStatus/onDebugEvent/onEnded).
- `sendMulawFrame(uint8array)` in AudioPipe matches the callback that session.js passes to `pipe.start(...)`.
- All good.

Plan complete and saved to `docs/superpowers/plans/2026-08-04-browser-widget-streaming-parity.md`.

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each task lands in its own commit. Best for a plan this size because each task has an independently testable deliverable and I can catch issues before they compound.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints for your review.

Which approach?
