"""Multi-call verification spike (2026-08-19).

Question: does the server correctly handle N simultaneous Twilio
Media Stream WebSockets, or does it serialize / crash / cross calls?

What this does:
  1. Opens N asyncio tasks in parallel, each opens a WS to
     ws://localhost:8000/twilio/stream
  2. Each task sends: connected → start (unique streamSid + callSid)
     → a few frames of mulaw silence → stop
  3. Records what each side sees: did the server accept? did it send
     a greeting audio? did it close cleanly?
  4. Reports per-call summary + overall pass/fail

Not a load test — the goal is CORRECTNESS at N=2 first, then N=5.
Load testing is a separate future task.

Usage:
  python scripts/multi_call_probe.py --n 2
  python scripts/multi_call_probe.py --n 5 --duration 8
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field

import websockets  # already a project dep


@dataclass
class CallSummary:
    call_sid: str
    stream_sid: str
    accepted: bool = False
    started: bool = False
    greeting_seen: bool = False
    frames_sent: int = 0
    frames_received: int = 0
    marks_received: int = 0
    close_reason: str = ""
    error: str = ""
    events: list[str] = field(default_factory=list)
    started_at: float = 0.0
    first_media_out_at: float = 0.0
    stopped_at: float = 0.0


SILENCE_FRAME = bytes([0xFF] * 160)  # 20ms of mulaw silence


async def run_one_call(
    ws_url: str, call_ix: int, duration_s: float,
) -> CallSummary:
    call_sid = f"CApr{uuid.uuid4().hex[:30]}"
    stream_sid = f"MZpr{uuid.uuid4().hex[:30]}"
    summary = CallSummary(call_sid=call_sid, stream_sid=stream_sid)
    summary.started_at = time.monotonic()

    try:
        async with websockets.connect(ws_url) as ws:
            summary.accepted = True

            await ws.send(json.dumps({
                "event": "connected",
                "protocol": "Call",
                "version": "1.0.0",
            }))

            await ws.send(json.dumps({
                "event": "start",
                "sequenceNumber": "1",
                "start": {
                    "accountSid": "ACprobetest",
                    "callSid": call_sid,
                    "streamSid": stream_sid,
                    "tracks": ["inbound"],
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                    "customParameters": {},
                },
                "streamSid": stream_sid,
            }))
            summary.started = True

            async def send_frames() -> None:
                for i in range(int(duration_s * 50)):
                    payload = base64.b64encode(SILENCE_FRAME).decode()
                    await ws.send(json.dumps({
                        "event": "media",
                        "sequenceNumber": str(i + 2),
                        "media": {
                            "track": "inbound",
                            "chunk": str(i + 1),
                            "timestamp": str(i * 20),
                            "payload": payload,
                        },
                        "streamSid": stream_sid,
                    }))
                    summary.frames_sent += 1
                    await asyncio.sleep(0.02)

            async def receive_events() -> None:
                try:
                    async for raw in ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue
                        kind = evt.get("event", "?")
                        summary.events.append(kind)
                        if kind == "media":
                            summary.frames_received += 1
                            if summary.first_media_out_at == 0:
                                summary.first_media_out_at = time.monotonic()
                                summary.greeting_seen = True
                        elif kind == "mark":
                            summary.marks_received += 1
                except Exception as e:
                    summary.error = f"recv-loop: {e!r}"

            send_task = asyncio.create_task(send_frames())
            recv_task = asyncio.create_task(receive_events())

            try:
                await asyncio.wait_for(send_task, timeout=duration_s + 2)
            except asyncio.TimeoutError:
                summary.error = "send-timeout"

            await ws.send(json.dumps({
                "event": "stop",
                "sequenceNumber": str(summary.frames_sent + 100),
                "stop": {"accountSid": "ACprobetest", "callSid": call_sid},
                "streamSid": stream_sid,
            }))
            summary.stopped_at = time.monotonic()

            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass

            summary.close_reason = "sent-stop"

    except Exception as e:
        summary.error = repr(e)
    return summary


async def main(n: int, duration: float, ws_url: str) -> None:
    print(f"probe: opening {n} concurrent Twilio WS connections to {ws_url}")
    print(f"probe: each sends {duration}s of silence then stops")
    print()

    t0 = time.monotonic()
    results = await asyncio.gather(*[
        run_one_call(ws_url, i, duration) for i in range(n)
    ])
    dt = time.monotonic() - t0

    print(f"probe: done in {dt:.1f}s")
    print()
    print("=" * 78)

    for i, r in enumerate(results):
        print(f"call #{i} — {r.call_sid}")
        print(f"  accepted:        {r.accepted}")
        print(f"  started:         {r.started}")
        print(f"  greeting-seen:   {r.greeting_seen}")
        first_media = (
            f"{(r.first_media_out_at - r.started_at) * 1000:.0f}ms"
            if r.first_media_out_at else "never"
        )
        print(f"  first-media-out: {first_media}")
        print(f"  frames-sent:     {r.frames_sent}")
        print(f"  frames-received: {r.frames_received}")
        print(f"  marks-received:  {r.marks_received}")
        print(f"  close:           {r.close_reason}")
        if r.error:
            print(f"  ERROR:           {r.error}")
        print()

    print("=" * 78)
    passed = sum(1 for r in results if r.accepted and r.started and r.greeting_seen and not r.error)
    print(f"VERDICT: {passed}/{n} calls fully succeeded")
    if passed == n:
        print("→ multi-call is WORKING at n=" + str(n))
    else:
        print("→ multi-call has issues — see per-call output above")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=2, help="number of concurrent calls")
    p.add_argument("--duration", type=float, default=5.0,
                   help="seconds of silence to stream per call")
    p.add_argument("--ws", default="ws://localhost:8000/twilio/stream")
    args = p.parse_args()
    asyncio.run(main(args.n, args.duration, args.ws))
