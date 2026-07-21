"""Download Qwen3-TTS-12Hz-1.7B-Base, clone a reference voice, save WAV.

The 1.7B Base variant supports 3-second voice cloning via generate_voice_clone(
ref_audio=..., ref_text=...). This script uses Qwen's own sample reference so it
works even before the user provides their own.

Run:
    source .venv/bin/activate
    python scripts/test_qwen3_tts_clone.py

First run downloads ~4-8GB to ~/.cache/huggingface/hub/.
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


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
# User's own recorded reference — trimmed to first 15s (grounded/calm portion).
# The full 70s file made a single sentence take >1 hour on M1 Pro MPS float32.
REF_AUDIO_URL = str(Path("/Users/az/Desktop/Receptionist Agent/data/voice_sample_15s.wav"))
REF_TEXT = (
    "Today I'm recording a cleaner reference for the voice pipeline. "
    "I want this read to stay grounded, calm, and a little deeper than my normal speaking voice. "
    "The goal is not to sound dramatic."
)

# Flush every print so long-running MPS generation shows live progress.
print = __import__("functools").partial(print, flush=True)  # type: ignore


def pick_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if torch.backends.mps.is_available():
        # MPS + float16/bfloat16 hits NaN/Inf in Qwen3-TTS sampling. Use float32.
        return "mps", torch.float32
    return "cpu", torch.float32


def main() -> int:
    device, dtype = pick_device_and_dtype()
    print(f"[qwen3-clone] device={device} dtype={dtype}")
    print(f"[qwen3-clone] loading {MODEL_ID} (first run downloads ~4-8GB)")

    t0 = time.time()
    try:
        model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map=device,
            dtype=dtype,
            attn_implementation="flash_attention_2",
        )
    except Exception as e:
        print(f"[qwen3-clone] flash_attention_2 unavailable ({e.__class__.__name__}); retrying with default attn")
        model = Qwen3TTSModel.from_pretrained(MODEL_ID, device_map=device, dtype=dtype)
    print(f"[qwen3-clone] model loaded in {time.time() - t0:.1f}s")

    text = (
        "Hey, this is Alex with SubtoDealz. I'm just reaching out about the property "
        "you have listed. Is now a good time to talk?"
    )
    print(f"[qwen3-clone] cloning voice from {REF_AUDIO_URL}")
    print(f"[qwen3-clone] synthesizing: {text!r}")
    t0 = time.time()
    wavs, sr = model.generate_voice_clone(
        text=text,
        language="English",
        ref_audio=REF_AUDIO_URL,
        ref_text=REF_TEXT,
    )
    print(f"[qwen3-clone] synthesized in {time.time() - t0:.1f}s (sr={sr}Hz, samples={len(wavs[0])})")

    out = REPO_ROOT / "data" / "qwen3_clone_smoke.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), wavs[0], sr)
    print(f"[qwen3-clone] wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
