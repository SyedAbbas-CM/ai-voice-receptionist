"""Standalone smoke test: download Qwen3-TTS, synthesize one sentence, write WAV.

Run:
    source .venv/bin/activate
    python scripts/test_qwen3_tts.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"


def pick_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if torch.backends.mps.is_available():
        # MPS + float16 hits NaN/Inf in the sampling distribution for Qwen3-TTS.
        # float32 is stable but slower. bfloat16 isn't supported on MPS.
        return "mps", torch.float32
    return "cpu", torch.float32


def main() -> int:
    device, dtype = pick_device_and_dtype()
    print(f"[qwen3] device={device} dtype={dtype}")
    print(f"[qwen3] loading {MODEL_ID} (first run downloads ~1-2GB to HF cache)")

    t0 = time.time()
    try:
        model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map=device,
            dtype=dtype,
            attn_implementation="flash_attention_2",
        )
    except Exception as e:
        print(f"[qwen3] flash_attention_2 unavailable ({e.__class__.__name__}); retrying with default attn")
        model = Qwen3TTSModel.from_pretrained(MODEL_ID, device_map=device, dtype=dtype)
    print(f"[qwen3] model loaded in {time.time() - t0:.1f}s")

    text = "Hi, thanks for calling Riverside Family Clinic. How can I help you today?"
    print(f"[qwen3] synthesizing: {text!r}")
    t0 = time.time()
    wavs, sr = model.generate_custom_voice(
        text=text,
        language="English",
        speaker="Vivian",
        instruct="warm, professional receptionist",
    )
    print(f"[qwen3] synthesized in {time.time() - t0:.1f}s (sr={sr}Hz, samples={len(wavs[0])})")

    out = REPO_ROOT / "data" / "qwen3_smoke.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), wavs[0], sr)
    print(f"[qwen3] wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
