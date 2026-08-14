#!/usr/bin/env python3
"""SOAK harness: replay a recorded WAV file through the Twilio Media
Streams WS path.  Lets us exercise the actor with deterministic audio
(fake-wait triggers, phone dictation, silence gaps) WITHOUT dialing a
real phone.

This does NOT exercise:
  - Twilio's carrier jitter
  - PK -> US network variance
  - Real STT weirdness (Deepgram on recorded audio behaves differently
    from Deepgram on live carrier µ-law)

But it DOES exercise:
  - The actor's WS message parser
  - The whole downstream pipeline (STT bridge, turn manager, brain,
    gate, TTS, ledger)
  - Determinism for regression tests

Usage:
  # single-file replay against a running server
  python apps/api/scripts/replay-audio.py \\
      --ws ws://localhost:8000/twilio/stream \\
      --wav docs/soak/fixtures/hamzah-fake-wait.wav \\
      --from "+923335244772" \\
      --to "+15551110000"

  # batch — run every WAV in a fixture dir
  python apps/api/scripts/replay-audio.py \\
      --ws ws://localhost:8000/twilio/stream \\
      --fixture-dir docs/soak/fixtures/ \\
      --report /tmp/replay-report.json

Behavior:
  - Sends a Twilio-shape `start` event with a fake CallSid
    (RE<uuid> so it doesn't collide with real Twilio call SIDs)
  - Chunks the WAV into 20ms µ-law frames and sends them at real time
    cadence (asyncio.sleep between frames)
  - Waits N seconds after audio ends for the actor to reply
  - Sends `stop` and closes
  - Prints the fake CallSid so you can run
    `scripts/verify-call.sh <sid>` immediately after

Requires: `websockets` package (already a transitive dep of uvicorn).
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import pathlib
import sys
import time
import uuid
import wave
from typing import Optional


TWILIO_SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES_MULAW = TWILIO_SAMPLE_RATE * FRAME_MS // 1000   # 160


def _wav_to_mulaw(path: pathlib.Path) -> bytes:
    """Load a WAV, return µ-law 8kHz bytes ready for Twilio Media
    Streams.  Handles PCM 16-bit at any rate — resamples to 8kHz."""
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())

    if sample_width != 2:
        # widen to 16-bit
        pcm = audioop.lin2lin(pcm, sample_width, 2)
    if n_channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != TWILIO_SAMPLE_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TWILIO_SAMPLE_RATE, None)
    return audioop.lin2ulaw(pcm, 2)


async def _replay_one(
    ws_url: str,
    wav_path: pathlib.Path,
    from_number: str,
    to_number: str,
    trailing_silence_s: float,
    verbose: bool,
) -> tuple[str, str, float]:
    """Replay one WAV.  Returns (call_sid, stream_sid, elapsed_s)."""
    import websockets

    call_sid = f"RE{uuid.uuid4().hex}"   # RE prefix = replay, distinct from CA
    stream_sid = f"MZ{uuid.uuid4().hex}"
    account_sid = "ACreplay0000000000000000000000000"

    mulaw = _wav_to_mulaw(wav_path)
    total_frames = len(mulaw) // FRAME_BYTES_MULAW
    total_ms = total_frames * FRAME_MS

    if verbose:
        print(f"[replay] wav={wav_path.name} frames={total_frames} "
              f"({total_ms}ms) call_sid={call_sid}", flush=True)

    t0 = time.monotonic()
    async with websockets.connect(ws_url) as ws:
        # 1) connected event (Twilio always sends this first)
        await ws.send(json.dumps({
            "event": "connected",
            "protocol": "Call",
            "version": "1.0.0",
        }))

        # 2) start event — mirror Twilio's shape as closely as possible
        await ws.send(json.dumps({
            "event": "start",
            "sequenceNumber": "1",
            "start": {
                "accountSid": account_sid,
                "streamSid": stream_sid,
                "callSid": call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": TWILIO_SAMPLE_RATE,
                    "channels": 1,
                },
                "customParameters": {
                    "from": from_number,
                    "to": to_number,
                    "callerName": "SOAK Replay",
                },
            },
            "streamSid": stream_sid,
        }))

        # 3) media frames at real-time cadence
        seq = 2
        tick = time.monotonic()
        for i in range(total_frames):
            offset = i * FRAME_BYTES_MULAW
            frame = mulaw[offset : offset + FRAME_BYTES_MULAW]
            await ws.send(json.dumps({
                "event": "media",
                "sequenceNumber": str(seq),
                "streamSid": stream_sid,
                "media": {
                    "track": "inbound",
                    "chunk": str(i + 1),
                    "timestamp": str(i * FRAME_MS),
                    "payload": base64.b64encode(frame).decode("ascii"),
                },
            }))
            seq += 1
            # Sleep to next tick (not sleep-for-N) — keeps cadence
            # tight even if send() takes non-zero time.
            tick += FRAME_MS / 1000
            sleep_for = tick - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        # 4) let the actor reply
        if verbose:
            print(f"[replay] audio drained; waiting {trailing_silence_s}s "
                  f"for actor reply...", flush=True)
        await asyncio.sleep(trailing_silence_s)

        # 5) stop
        await ws.send(json.dumps({
            "event": "stop",
            "sequenceNumber": str(seq),
            "streamSid": stream_sid,
            "stop": {"accountSid": account_sid, "callSid": call_sid},
        }))

    elapsed = time.monotonic() - t0
    return call_sid, stream_sid, elapsed


async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True,
                    help="WebSocket URL to the actor's /twilio/stream")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--wav", type=pathlib.Path, help="single WAV to replay")
    grp.add_argument("--fixture-dir", type=pathlib.Path,
                     help="dir of WAVs to replay in sequence")
    ap.add_argument("--from", dest="from_number", default="+923335244772",
                    help="fake caller ANI (default: Karachi test number)")
    ap.add_argument("--to", dest="to_number", default="+15551110000",
                    help="fake dialed number (default: US 555)")
    ap.add_argument("--trailing-silence", type=float, default=8.0,
                    help="seconds to wait after audio for the actor to reply")
    ap.add_argument("--report", type=pathlib.Path,
                    help="write a JSON summary of every call_sid to this path")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if args.wav:
        wavs = [args.wav]
    else:
        wavs = sorted(args.fixture_dir.glob("*.wav"))
        if not wavs:
            print(f"error: no *.wav in {args.fixture_dir}", file=sys.stderr)
            return 3

    results: list[dict] = []
    for wav in wavs:
        try:
            call_sid, stream_sid, elapsed_s = await _replay_one(
                ws_url=args.ws,
                wav_path=wav,
                from_number=args.from_number,
                to_number=args.to_number,
                trailing_silence_s=args.trailing_silence,
                verbose=not args.quiet,
            )
            results.append({
                "wav": str(wav),
                "call_sid": call_sid,
                "stream_sid": stream_sid,
                "elapsed_s": round(elapsed_s, 2),
                "error": None,
            })
            if not args.quiet:
                print(f"[replay] DONE  call_sid={call_sid}  "
                      f"elapsed={elapsed_s:.1f}s\n"
                      f"  verify:  apps/api/scripts/verify-call.sh "
                      f"{call_sid}", flush=True)
        except Exception as e:
            results.append({
                "wav": str(wav),
                "call_sid": None,
                "stream_sid": None,
                "elapsed_s": None,
                "error": f"{type(e).__name__}: {e}",
            })
            print(f"[replay] FAIL  wav={wav.name}: {e}", file=sys.stderr)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2))
        if not args.quiet:
            print(f"[replay] wrote report to {args.report}", flush=True)

    # Exit 0 iff every replay produced a call_sid.
    failed = sum(1 for r in results if r["error"] is not None)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
