"""Bench Kokoro ONNX backend on M1 Pro.

Run:
    source .venv/bin/activate
    python scripts/test_kokoro_onnx.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

TEST_SENTENCE = (
    "Hi, thanks for calling Riverside Family Clinic. "
    "How can I help you today?"
)


def main() -> int:
    from app.core.config import settings
    settings.kokoro_backend = "onnx"

    from app.providers.tts.kokoro_tts import KokoroTTS

    tts = KokoroTTS()

    print(f"[kokoro-onnx] loading (may download ~350MB to ~/.cache/kokoro-onnx/)")
    t0 = time.time()
    tts._load()
    load_time = time.time() - t0
    print(f"[kokoro-onnx] loaded in {load_time:.1f}s  active_backend={tts._active_backend!r}")

    print(f"[kokoro-onnx] synthesizing {len(TEST_SENTENCE)} chars with voice='af_heart'")
    t0 = time.time()
    import asyncio
    audio_bytes, mime = asyncio.run(tts.synthesize(TEST_SENTENCE))
    synth_time = time.time() - t0

    # Peek WAV metadata for audio duration
    import wave, io
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        audio_duration = w.getnframes() / w.getframerate()

    rtf = synth_time / audio_duration if audio_duration > 0 else float("inf")

    print(f"[kokoro-onnx] synthesized in {synth_time:.2f}s")
    print(f"[kokoro-onnx] audio duration: {audio_duration:.2f}s")
    print(f"[kokoro-onnx] RTF: {rtf:.3f}  ({'faster than realtime' if rtf < 1 else 'slower than realtime'})")

    out = REPO_ROOT / "data" / "kokoro_onnx_smoke.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio_bytes)
    print(f"[kokoro-onnx] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
