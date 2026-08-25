"""Bench ElevenLabs /stream first-byte across chunk_size values.

Question: does httpx `aiter_bytes(chunk_size=1600)` in our production
TTS path (`elevenlabs_tts.py:123`) withhold the earliest playable audio?

Test method: hit EL's /stream endpoint with same voice, same model, same
output_format as production. For each chunk_size, measure:
  - time to first yielded chunk (this is what production sees as "first byte")
  - size of first chunk (proves whether we're getting withheld or streamed-through)
  - total bytes / total time (throughput sanity)

Run: python scripts/bench_el_chunk_size.py
Env: reads ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID from .env

Standalone: does not import any project code. Does not touch the running
server. Uses its own httpx client.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    """Minimal .env parser — we do NOT want to import the project's config."""
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


ENV = _load_env()
API_KEY = ENV.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
VOICE_ID = ENV.get("ELEVENLABS_VOICE_ID") or "cgSgspJ2msm6clMCkdW9"
MODEL = ENV.get("ELEVENLABS_MODEL") or "eleven_flash_v2_5"

# Same output_format the Twilio path uses in production.
OUTPUT_FORMAT = "ulaw_8000"

# Representative test text — booking confirmation flavor, ~15 words.
TEST_TEXT = (
    "Perfect, you're all set for your new patient exam with X-rays "
    "tomorrow at two thirty with Doctor Chen. See you then!"
)

# Chunk sizes to bench.
# 1600 = ~200ms of audio at μ-law 8kHz (current production setting)
# 640  = ~80ms
# 320  = ~40ms
# 160  = ~20ms (one Twilio frame)
CHUNK_SIZES = [1600, 640, 320, 160]

# Trials per chunk size — enough to get a stable median.
TRIALS_PER_SIZE = 5


async def bench_one(chunk_size: int, client: httpx.AsyncClient) -> dict:
    """One request: return {first_byte_ms, first_chunk_bytes, total_bytes,
    total_ms}."""
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
        f"?output_format={OUTPUT_FORMAT}"
    )
    body = {
        "text": TEST_TEXT,
        "model_id": MODEL,
        # Match production voice_settings (elevenlabs_tts.py:112-120)
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    headers = {
        "xi-api-key": API_KEY,
        "Content-Type": "application/json",
    }

    t_req = time.perf_counter()
    first_byte_ms: float | None = None
    first_chunk_bytes = 0
    total_bytes = 0

    async with client.stream("POST", url, headers=headers, json=body) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
            if not chunk:
                continue
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - t_req) * 1000.0
                first_chunk_bytes = len(chunk)
            total_bytes += len(chunk)

    total_ms = (time.perf_counter() - t_req) * 1000.0
    return {
        "first_byte_ms": first_byte_ms or 0.0,
        "first_chunk_bytes": first_chunk_bytes,
        "total_bytes": total_bytes,
        "total_ms": total_ms,
    }


async def main() -> None:
    if not API_KEY:
        print("FATAL: no ELEVENLABS_API_KEY in .env or env")
        return

    # Match production client shape: HTTP/2 + keepalive.
    # (elevenlabs_tts.py:49-65)
    client = httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(60.0, connect=5.0),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=300.0,
        ),
    )
    try:
        print(f"voice_id: {VOICE_ID}")
        print(f"model:    {MODEL}")
        print(f"format:   {OUTPUT_FORMAT}")
        print(f"text:     {TEST_TEXT!r}")
        print(f"trials:   {TRIALS_PER_SIZE} per chunk size")
        print()

        # Warmup: prime the HTTP/2 connection so we're not measuring TLS
        # handshake in the first real trial.
        print("warmup...", flush=True)
        await bench_one(1600, client)
        print("done.\n")

        results: dict[int, list[dict]] = {}
        for cs in CHUNK_SIZES:
            trials = []
            print(f"chunk_size={cs} ({cs / 8:.0f}ms of audio):")
            for i in range(TRIALS_PER_SIZE):
                r = await bench_one(cs, client)
                trials.append(r)
                print(
                    f"  trial {i + 1}: first_byte={r['first_byte_ms']:.0f}ms "
                    f"first_chunk={r['first_chunk_bytes']}B "
                    f"total={r['total_ms']:.0f}ms ({r['total_bytes']}B)"
                )
            results[cs] = trials
            print()

        # Summary
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(
            f"{'chunk_size':>12} {'p50_first_byte':>16} {'p90_first_byte':>16} "
            f"{'median_first_chunk':>20}"
        )
        for cs, trials in results.items():
            fbms = sorted(t["first_byte_ms"] for t in trials)
            fcb = sorted(t["first_chunk_bytes"] for t in trials)
            p50 = fbms[len(fbms) // 2]
            p90_idx = min(len(fbms) - 1, int(len(fbms) * 0.9))
            p90 = fbms[p90_idx]
            fcb_med = fcb[len(fcb) // 2]
            print(
                f"{cs:>12} {p50:>13.0f}ms {p90:>13.0f}ms {fcb_med:>18}B"
            )

        # Interpretation
        print()
        base_p50 = statistics.median(t["first_byte_ms"] for t in results[1600])
        for cs in CHUNK_SIZES:
            if cs == 1600:
                continue
            p50 = statistics.median(t["first_byte_ms"] for t in results[cs])
            delta = base_p50 - p50
            verdict = (
                "WIN" if delta > 50 else "NEUTRAL" if abs(delta) <= 50 else "LOSS"
            )
            print(
                f"  chunk_size={cs}: delta_vs_1600 = "
                f"{'+' if delta > 0 else ''}{delta:.0f}ms  [{verdict}]"
            )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
