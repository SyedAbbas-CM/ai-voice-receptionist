"""Smoke test: load Kokoro-82M, synthesize one clinic sentence, report RTF.

Run:
    source .venv/bin/activate
    python scripts/test_kokoro_tts.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_SENTENCE = (
    "Hi, thanks for calling Riverside Family Clinic. "
    "How can I help you today?"
)


def main() -> int:
    print(f"[kokoro] loading (first run downloads ~300 MB to HF cache)")
    t0 = time.time()
    from kokoro import KPipeline
    try:
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    try:
        pipeline = KPipeline(lang_code="a", device=device)
    except Exception as e:
        print(f"[kokoro] failed on device={device} ({e}), retrying CPU")
        pipeline = KPipeline(lang_code="a", device="cpu")
        device = "cpu"

    load_time = time.time() - t0
    print(f"[kokoro] loaded in {load_time:.1f}s on device={device}")

    print(f"[kokoro] synthesizing {len(TEST_SENTENCE)} chars with voice='af_heart'")
    t0 = time.time()

    import numpy as np
    chunks = []
    for result in pipeline(TEST_SENTENCE, voice="af_heart"):
        audio = result[-1] if isinstance(result, tuple) else getattr(result, "audio", None)
        if audio is None:
            continue
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        chunks.append(np.asarray(audio, dtype="float32"))

    synth_time = time.time() - t0
    joined = np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")
    sr = 24000
    audio_duration = len(joined) / sr
    rtf = synth_time / audio_duration if audio_duration > 0 else float("inf")

    print(f"[kokoro] synthesized in {synth_time:.2f}s")
    print(f"[kokoro] audio duration: {audio_duration:.2f}s ({len(joined)} samples @ {sr}Hz)")
    print(f"[kokoro] RTF: {rtf:.3f}  ({'faster than realtime' if rtf < 1 else 'slower than realtime'})")

    # Save so you can listen
    import soundfile as sf
    out = REPO_ROOT / "data" / "kokoro_smoke.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), joined, sr)
    print(f"[kokoro] wrote {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
