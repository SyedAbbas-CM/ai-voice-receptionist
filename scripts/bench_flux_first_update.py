"""Investigate the Flux first_update anomaly surfaced by bench_flux_encoding.

The encoding bench (2026-08-23) saw ~3300ms to first Update on a 15s
speech sample — abnormal compared to production where DGF_TURN Updates
fire within ~200-500ms of speech onset per prod logs.

This script isolates variables to distinguish causes:

  H1  Bench sample had leading silence → Flux waited on VAD.
  H2  Missing keyterm/language_hint params → different codepath.
  H3  Cold-connection warmup penalty on flux-general-en.
  H4  eot_threshold too strict → Updates emitted but held back.
  H5  Real callers use short utterances that trigger StartOfTurn earlier.

Each test is one WS connection + one controlled audio stream. We record:
  - time to first Update event
  - time to first StartOfTurn event
  - time to first EagerEndOfTurn / EndOfTurn
  - transcript
  - full event tape (event, transcript, eot_conf) for post-mortem

Cases run:

  A  full 15s sample, current-prod params (keyterm + eot=0.7)         (baseline)
  B  same sample, NO keyterm, NO language_hint                        (H2 isolate)
  C  first 3s of sample only (short utterance)                        (H5 isolate)
  D  1s silence + first 3s of sample                                  (H1 confirm)
  E  no silence, no keyterm, eot_threshold=0.5                        (H4 isolate)
  F  cold connection (fresh WS per trial × 3, keep only first)        (H3 confirm)

We do NOT run in loops or batches. One session per case. Bench-first
pattern: write the tool, run only when a real trace demands it.

Run: /Users/az/Desktop/Receptionist\\ Agent/.venv/bin/python3 \\
     scripts/bench_flux_first_update.py

Requires: DEEPGRAM_API_KEY in .env, data/voice_sample.wav.
"""
from __future__ import annotations

import asyncio
import audioop
import json
import time
import wave
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import websockets


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PATH = REPO_ROOT / "data" / "voice_sample.wav"
FLUX_URL = "wss://api.deepgram.com/v2/listen"
FRAME_MS = 20

# Match production keyterm list order (deepgram_stt._DENTAL_KEYTERMS
# — read from source in case the actual list changes).
_DEFAULT_KEYTERMS = [
    "cleaning", "cavity", "filling", "extraction", "root canal",
    "crown", "veneer", "whitening", "checkup", "dental",
    "invisalign", "braces", "wisdom teeth", "gum", "toothache",
]


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


def _load_pcm_16k(path: Path) -> bytes:
    """Load a WAV and resample to 16k int16 mono."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if rate == 16000:
        return raw
    resampled, _ = audioop.ratecv(raw, 2, 1, rate, 16000, None)
    return resampled


def _to_mulaw_8k(pcm_16k: bytes) -> bytes:
    pcm_8k, _ = audioop.ratecv(pcm_16k, 2, 1, 16000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def _slice_ms(mulaw_8k: bytes, start_ms: int, dur_ms: int) -> bytes:
    """Slice from μ-law 8kHz stream. 8 bytes per ms."""
    start = start_ms * 8
    end = start + dur_ms * 8
    return mulaw_8k[start:end]


def _silence_mulaw(dur_ms: int) -> bytes:
    """μ-law encoded silence. 0xFF is the μ-law encoding for 0."""
    return b"\xff" * (dur_ms * 8)


def _build_url(
    *,
    keyterms: bool = True,
    language_hint: Optional[str] = None,
    eot_threshold: float = 0.7,
    eager_eot_threshold: float = 0.5,
    model: str = "flux-general-en",
) -> str:
    params: list[tuple[str, str]] = [
        ("model", model),
        ("encoding", "mulaw"),
        ("sample_rate", "8000"),
        ("eot_threshold", str(eot_threshold)),
        ("eager_eot_threshold", str(eager_eot_threshold)),
        ("eot_timeout_ms", "3000"),
    ]
    if language_hint:
        params.append(("language_hint", language_hint))
    if keyterms:
        for kt in _DEFAULT_KEYTERMS:
            params.append(("keyterm", kt))
    return f"{FLUX_URL}?{urlencode(params)}"


async def _run_case(
    label: str,
    audio: bytes,
    *,
    keyterms: bool = True,
    language_hint: Optional[str] = None,
    eot_threshold: float = 0.7,
    tape_all_events: bool = False,
) -> dict:
    """Open Flux WS, stream `audio` at 20ms cadence, collect timings.

    audio is expected as μ-law 8kHz. 160 bytes per frame.
    """
    url = _build_url(
        keyterms=keyterms,
        language_hint=language_hint,
        eot_threshold=eot_threshold,
    )
    headers = {"Authorization": f"Token {API_KEY}"}

    events: list[dict] = []
    first_update_ms: Optional[float] = None
    first_start_ms: Optional[float] = None
    first_eager_ms: Optional[float] = None
    first_eot_ms: Optional[float] = None
    final_transcript = ""
    error: Optional[str] = None

    t_open = time.perf_counter()

    async def _reader(ws):
        nonlocal first_update_ms, first_start_ms
        nonlocal first_eager_ms, first_eot_ms, final_transcript, error
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                now = (time.perf_counter() - t_open) * 1000.0
                evt = msg.get("event") or msg.get("type")
                transcript = msg.get("transcript") or ""
                eot_conf = msg.get("end_of_turn_confidence")
                if tape_all_events:
                    events.append({
                        "t_ms": round(now),
                        "event": evt,
                        "transcript": transcript,
                        "eot_conf": eot_conf,
                    })
                if evt == "Update" and first_update_ms is None and transcript:
                    first_update_ms = now
                if evt == "StartOfTurn" and first_start_ms is None:
                    first_start_ms = now
                if evt == "EagerEndOfTurn" and first_eager_ms is None:
                    first_eager_ms = now
                if evt == "EndOfTurn":
                    if first_eot_ms is None:
                        first_eot_ms = now
                    if transcript:
                        final_transcript = transcript
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            error = f"reader:{type(e).__name__}:{e}"

    frame_bytes = 160  # μ-law 8kHz, 20ms
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            reader_task = asyncio.create_task(_reader(ws))
            t_send_start = time.perf_counter()
            frame_idx = 0
            for i in range(0, len(audio), frame_bytes):
                chunk = audio[i:i + frame_bytes]
                if not chunk:
                    break
                # Pad partial trailing frame with silence so Flux gets
                # exactly 20ms boundaries end-to-end (mirrors what Twilio
                # sends in production).
                if len(chunk) < frame_bytes:
                    chunk = chunk + b"\xff" * (frame_bytes - len(chunk))
                await ws.send(chunk)
                frame_idx += 1
                target = t_send_start + frame_idx * (FRAME_MS / 1000.0)
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            # Force flush so end_of_turn fires deterministically.
            await ws.send(json.dumps({"type": "Finalize"}))
            try:
                await asyncio.wait_for(reader_task, timeout=3.0)
            except asyncio.TimeoutError:
                reader_task.cancel()
    except Exception as e:
        error = f"connect:{type(e).__name__}:{e}"

    return {
        "label": label,
        "first_update_ms": first_update_ms,
        "first_start_of_turn_ms": first_start_ms,
        "first_eager_eot_ms": first_eager_ms,
        "first_end_of_turn_ms": first_eot_ms,
        "final_transcript": final_transcript,
        "audio_duration_ms": len(audio) / 8,  # 8 bytes per ms at μ-law 8k
        "keyterms": keyterms,
        "language_hint": language_hint,
        "eot_threshold": eot_threshold,
        "events": events if tape_all_events else None,
        "error": error,
    }


def _fmt_ms(v: Optional[float]) -> str:
    return f"{v:.0f}ms" if v is not None else "NONE"


async def main() -> None:
    if not API_KEY:
        print("FATAL: no DEEPGRAM_API_KEY in .env")
        return
    if not SAMPLE_PATH.exists():
        print(f"FATAL: no sample at {SAMPLE_PATH}")
        return

    print(f"loading {SAMPLE_PATH.name} ...")
    pcm_16k = _load_pcm_16k(SAMPLE_PATH)
    mulaw = _to_mulaw_8k(pcm_16k)
    total_ms = len(mulaw) // 8
    print(f"  {total_ms}ms of μ-law 8k audio ready.\n")

    # Slices we need.
    full = mulaw
    first_3s = _slice_ms(mulaw, 0, 3000)
    sil_1s_plus_3s = _silence_mulaw(1000) + first_3s

    results: list[dict] = []

    print("=" * 72)
    print("Case A  baseline (15s, keyterm+en, eot=0.7)")
    print("=" * 72)
    r = await _run_case(
        "A_baseline_full15s_keyterms",
        full[:15 * 8 * 1000],  # first 15s
        keyterms=True, eot_threshold=0.7, tape_all_events=True,
    )
    results.append(r)
    print(f"  first_update={_fmt_ms(r['first_update_ms'])} "
          f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
          f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])} "
          f"tx={r['final_transcript'][:60]!r}")
    if r["events"]:
        print("  first 10 events on the tape:")
        for ev in r["events"][:10]:
            print(f"    {ev['t_ms']:>5}ms  {ev['event']:<16} "
                  f"tx={ev['transcript'][:40]!r} eot={ev['eot_conf']}")
    await asyncio.sleep(0.5)
    print()

    print("=" * 72)
    print("Case B  no keyterms, no language_hint  (H2)")
    print("=" * 72)
    r = await _run_case(
        "B_no_keyterms_no_hint",
        full[:15 * 8 * 1000],
        keyterms=False, language_hint=None, eot_threshold=0.7,
    )
    results.append(r)
    print(f"  first_update={_fmt_ms(r['first_update_ms'])} "
          f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
          f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])}")
    await asyncio.sleep(0.5)
    print()

    print("=" * 72)
    print("Case C  first 3s only, short-utterance shape  (H5)")
    print("=" * 72)
    r = await _run_case(
        "C_first_3s_keyterms",
        first_3s,
        keyterms=True, eot_threshold=0.7, tape_all_events=True,
    )
    results.append(r)
    print(f"  first_update={_fmt_ms(r['first_update_ms'])} "
          f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
          f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])} "
          f"tx={r['final_transcript'][:60]!r}")
    if r["events"]:
        for ev in r["events"][:8]:
            print(f"    {ev['t_ms']:>5}ms  {ev['event']:<16} "
                  f"tx={ev['transcript'][:40]!r} eot={ev['eot_conf']}")
    await asyncio.sleep(0.5)
    print()

    print("=" * 72)
    print("Case D  1s silence + 3s speech  (H1 confirm leading-silence)")
    print("=" * 72)
    r = await _run_case(
        "D_1s_silence_then_3s",
        sil_1s_plus_3s,
        keyterms=True, eot_threshold=0.7,
    )
    results.append(r)
    print(f"  first_update={_fmt_ms(r['first_update_ms'])} "
          f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
          f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])} "
          f"tx={r['final_transcript'][:60]!r}")
    await asyncio.sleep(0.5)
    print()

    print("=" * 72)
    print("Case E  no silence, eot=0.5 (looser)  (H4)")
    print("=" * 72)
    r = await _run_case(
        "E_first_3s_eot_0_5",
        first_3s,
        keyterms=True, eot_threshold=0.5,
    )
    results.append(r)
    print(f"  first_update={_fmt_ms(r['first_update_ms'])} "
          f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
          f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])}")
    await asyncio.sleep(0.5)
    print()

    print("=" * 72)
    print("Case F  cold connection × 3 (H3 warmup penalty)")
    print("=" * 72)
    for i in range(3):
        r = await _run_case(
            f"F_cold_conn_{i + 1}",
            first_3s,
            keyterms=True, eot_threshold=0.7,
        )
        results.append(r)
        print(f"  trial {i + 1}: first_update={_fmt_ms(r['first_update_ms'])} "
              f"first_start={_fmt_ms(r['first_start_of_turn_ms'])} "
              f"first_eot={_fmt_ms(r['first_end_of_turn_ms'])}")
        # Sleep between to force cold-ish reconnect (not truly cold since
        # DNS + TLS cache but at least a fresh WS handshake per trial).
        await asyncio.sleep(2.0)
    print()

    # Summary — a quick anomaly-triage matrix.
    print("=" * 72)
    print("SUMMARY — first_update deltas point to the true cause")
    print("=" * 72)
    print(f"{'case':<32} {'first_update':>14} {'first_start':>14} "
          f"{'first_eot':>12} {'audio_dur':>10}")
    for r in results:
        print(
            f"{r['label']:<32} "
            f"{_fmt_ms(r['first_update_ms']):>14} "
            f"{_fmt_ms(r['first_start_of_turn_ms']):>14} "
            f"{_fmt_ms(r['first_end_of_turn_ms']):>12} "
            f"{r['audio_duration_ms']:>8.0f}ms"
        )
    print()
    print("Interpretation guide:")
    print("  If C (first 3s) << A (full 15s): the sample HAD leading")
    print("    silence Flux waited on; production hits it never.")
    print("  If D >> C by ~1000ms: leading silence confirmed as cause.")
    print("  If B ≈ A: keyterm params aren't the delta.")
    print("  If E << A: eot_threshold was suppressing early Updates.")
    print("  If F trial 1 >> F trial 2/3: cold-connect penalty exists.")


if __name__ == "__main__":
    asyncio.run(main())
