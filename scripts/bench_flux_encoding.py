"""Bench Deepgram Flux: mulaw@8k vs linear16@48k input.

Question (AUDIT-S4): does feeding Flux raw μ-law 8kHz (the wire format
we already receive from Twilio) beat upsampling to linear16@48k?

Currently our LiveKit path uses linear16@48k (audio_bridge stub) and
our Twilio path could either send mulaw@8k directly or upsample. The
audit claims 8k native saves ~50ms first-partial.

Test method: open TWO Flux WS sessions, one with mulaw@8k config, one
with linear16@48k config. Play the same 15s speech sample through
each in real time (20ms Twilio-style frames). Measure:
  - time to first Update event (first partial)
  - time to first EndOfTurn / EagerEndOfTurn event
  - transcript at end (accuracy sanity)
  - upstream bytes sent (bandwidth sanity)

Standalone: does not import project code. Does not touch the running
server. Uses same Flux config as prod (eot_threshold etc from .env).

Run: /Users/az/Desktop/Receptionist\\ Agent/.venv/bin/python3 scripts/bench_flux_encoding.py
"""
from __future__ import annotations

import asyncio
import audioop
import json
import statistics
import time
import wave
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


ENV = _load_env()
API_KEY = ENV.get("DEEPGRAM_API_KEY")
EOT_THRESHOLD = float(ENV.get("DEEPGRAM_FLUX_EOT_THRESHOLD", "0.7"))
EAGER_EOT_THRESHOLD = float(ENV.get("DEEPGRAM_FLUX_EAGER_EOT_THRESHOLD", "0.5"))
EOT_TIMEOUT_MS = int(ENV.get("DEEPGRAM_FLUX_EOT_TIMEOUT_MS", "3000"))

FLUX_URL = "wss://api.deepgram.com/v2/listen"
SAMPLE_PATH = REPO_ROOT / "data" / "voice_sample_15s.wav"

# 20ms Twilio frame cadence.
FRAME_MS = 20
TRIALS = 3


def _load_sample_16k() -> bytes:
    """Load 15s WAV → int16 mono @ 16kHz (native rate)."""
    with wave.open(str(SAMPLE_PATH), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        native_rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if native_rate == 16000:
        return raw
    # Resample to 16k so we have a canonical source.
    resampled, _ = audioop.ratecv(raw, 2, 1, native_rate, 16000, None)
    return resampled


def _prep_mulaw_8k(pcm_16k: bytes) -> bytes:
    """Downsample 16k → 8k → μ-law encode. Matches Twilio wire shape."""
    pcm_8k, _ = audioop.ratecv(pcm_16k, 2, 1, 16000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def _prep_linear16_48k(pcm_16k: bytes) -> bytes:
    """Upsample 16k → 48k linear16. Matches LiveKit path shape."""
    pcm_48k, _ = audioop.ratecv(pcm_16k, 2, 1, 16000, 48000, None)
    return pcm_48k


def _build_url(encoding: str, sample_rate: int) -> str:
    params: list[tuple[str, str]] = [
        ("model", "flux-general-en"),
        ("encoding", encoding),
        ("sample_rate", str(sample_rate)),
        ("eot_threshold", str(EOT_THRESHOLD)),
        ("eager_eot_threshold", str(EAGER_EOT_THRESHOLD)),
        ("eot_timeout_ms", str(EOT_TIMEOUT_MS)),
    ]
    return f"{FLUX_URL}?{urlencode(params)}"


async def _bench_one(
    label: str,
    encoding: str,
    sample_rate: int,
    audio_bytes: bytes,
    bytes_per_frame: int,
) -> dict:
    """One trial: stream `audio_bytes` in bytes_per_frame chunks at 20ms
    cadence, collect Flux events, report timings."""
    url = _build_url(encoding, sample_rate)
    headers = {"Authorization": f"Token {API_KEY}"}

    first_update_ms: Optional[float] = None
    first_start_of_turn_ms: Optional[float] = None
    first_eager_eot_ms: Optional[float] = None
    first_end_of_turn_ms: Optional[float] = None
    final_transcript = ""
    event_count = 0
    bytes_sent = 0
    error: Optional[str] = None

    t_open = time.perf_counter()

    async def _reader(ws):
        nonlocal first_update_ms, first_start_of_turn_ms
        nonlocal first_eager_eot_ms, first_end_of_turn_ms
        nonlocal final_transcript, event_count, error
        try:
            async for raw in ws:
                event_count += 1
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                evt = msg.get("event") or msg.get("type")
                transcript = msg.get("transcript") or ""
                now = (time.perf_counter() - t_open) * 1000.0
                if evt == "Update" and first_update_ms is None and transcript:
                    first_update_ms = now
                if evt == "StartOfTurn" and first_start_of_turn_ms is None:
                    first_start_of_turn_ms = now
                if evt == "EagerEndOfTurn" and first_eager_eot_ms is None:
                    first_eager_eot_ms = now
                if evt == "EndOfTurn":
                    if first_end_of_turn_ms is None:
                        first_end_of_turn_ms = now
                    if transcript:
                        final_transcript = transcript
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            error = f"reader:{type(e).__name__}:{e}"

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            reader_task = asyncio.create_task(_reader(ws))
            # Real-time paced send: one bytes_per_frame chunk per 20ms.
            t_send_start = time.perf_counter()
            frame_idx = 0
            for i in range(0, len(audio_bytes), bytes_per_frame):
                chunk = audio_bytes[i:i + bytes_per_frame]
                if not chunk:
                    break
                await ws.send(chunk)
                bytes_sent += len(chunk)
                frame_idx += 1
                # Sleep until the next 20ms boundary since t_send_start.
                target = t_send_start + frame_idx * (FRAME_MS / 1000.0)
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            # Send Finalize to force flush.
            await ws.send(json.dumps({"type": "Finalize"}))
            # Wait up to 2s for final events.
            try:
                await asyncio.wait_for(reader_task, timeout=2.0)
            except asyncio.TimeoutError:
                reader_task.cancel()
    except Exception as e:
        error = f"connect:{type(e).__name__}:{e}"

    return {
        "label": label,
        "encoding": encoding,
        "sample_rate": sample_rate,
        "first_update_ms": first_update_ms,
        "first_start_of_turn_ms": first_start_of_turn_ms,
        "first_eager_eot_ms": first_eager_eot_ms,
        "first_end_of_turn_ms": first_end_of_turn_ms,
        "final_transcript": final_transcript,
        "event_count": event_count,
        "bytes_sent": bytes_sent,
        "error": error,
    }


def _median(values: list[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return statistics.median(xs)


async def main() -> None:
    if not API_KEY:
        print("FATAL: no DEEPGRAM_API_KEY in .env")
        return
    if not SAMPLE_PATH.exists():
        print(f"FATAL: no sample at {SAMPLE_PATH}")
        return

    pcm_16k = _load_sample_16k()
    print(f"loaded sample: {len(pcm_16k)} bytes @ 16kHz "
          f"({len(pcm_16k) / 2 / 16000:.2f}s)")

    mulaw_8k = _prep_mulaw_8k(pcm_16k)
    lin16_48k = _prep_linear16_48k(pcm_16k)
    print(f"mulaw@8k:    {len(mulaw_8k)} bytes ({len(mulaw_8k) / 8000:.2f}s)")
    print(f"linear16@48k: {len(lin16_48k)} bytes ({len(lin16_48k) / 2 / 48000:.2f}s)")
    print()

    # Bytes per 20ms frame at each config.
    # mulaw@8k: 8000 samples/s * 0.020s * 1 byte/sample = 160 bytes
    # linear16@48k: 48000 * 0.020 * 2 = 1920 bytes
    configs = [
        ("mulaw@8k", "mulaw", 8000, mulaw_8k, 160),
        ("linear16@48k", "linear16", 48000, lin16_48k, 1920),
    ]

    all_results: dict[str, list[dict]] = {c[0]: [] for c in configs}

    for trial in range(1, TRIALS + 1):
        print(f"--- trial {trial}/{TRIALS} ---")
        for label, enc, sr, audio, bpf in configs:
            print(f"  {label} ...", flush=True, end=" ")
            r = await _bench_one(label, enc, sr, audio, bpf)
            all_results[label].append(r)
            if r["error"]:
                print(f"ERR {r['error']}")
                continue
            fu = r["first_update_ms"]
            fe = r["first_end_of_turn_ms"]
            print(
                f"first_update={fu:.0f}ms " if fu else "first_update=NONE "
            , end="")
            print(
                f"first_eot={fe:.0f}ms " if fe else "first_eot=NONE "
            , end="")
            print(f"bytes={r['bytes_sent']} events={r['event_count']}")
        # Small gap between trials so Flux doesn't rate-throttle us.
        await asyncio.sleep(1.0)
        print()

    print("=" * 70)
    print("SUMMARY (median across trials)")
    print("=" * 70)
    print(f"{'config':<16} {'first_update':>14} {'first_eot':>12} "
          f"{'bytes/s':>10} {'transcript_ok':>14}")
    for label, results in all_results.items():
        fu = _median([r["first_update_ms"] for r in results])
        fe = _median([r["first_end_of_turn_ms"] for r in results])
        bs = statistics.mean([r["bytes_sent"] for r in results]) / 15.0  # 15s sample
        ok = sum(
            1 for r in results
            if r["final_transcript"] and len(r["final_transcript"]) > 20
        )
        print(
            f"{label:<16} "
            f"{f'{fu:.0f}ms' if fu else 'NONE':>14} "
            f"{f'{fe:.0f}ms' if fe else 'NONE':>12} "
            f"{bs:>10.0f} "
            f"{f'{ok}/{TRIALS}':>14}"
        )

    # Verdict
    print()
    fu_mulaw = _median([r["first_update_ms"] for r in all_results["mulaw@8k"]])
    fu_lin = _median([r["first_update_ms"] for r in all_results["linear16@48k"]])
    if fu_mulaw and fu_lin:
        delta = fu_lin - fu_mulaw  # positive = mulaw is faster
        verdict = "WIN" if delta > 50 else "NEUTRAL" if abs(delta) <= 50 else "LOSS"
        print(
            f"first_update delta (linear16@48k - mulaw@8k) = "
            f"{'+' if delta > 0 else ''}{delta:.0f}ms  [mulaw {verdict}]"
        )

    fe_mulaw = _median([r["first_end_of_turn_ms"] for r in all_results["mulaw@8k"]])
    fe_lin = _median([r["first_end_of_turn_ms"] for r in all_results["linear16@48k"]])
    if fe_mulaw and fe_lin:
        delta = fe_lin - fe_mulaw
        verdict = "WIN" if delta > 50 else "NEUTRAL" if abs(delta) <= 50 else "LOSS"
        print(
            f"first_eot    delta (linear16@48k - mulaw@8k) = "
            f"{'+' if delta > 0 else ''}{delta:.0f}ms  [mulaw {verdict}]"
        )

    # Print sample transcripts for accuracy check.
    print()
    print("--- transcripts (first trial each config) ---")
    for label, results in all_results.items():
        if results:
            t = results[0].get("final_transcript", "")
            print(f"  {label}: {t[:120]!r}")


if __name__ == "__main__":
    asyncio.run(main())
