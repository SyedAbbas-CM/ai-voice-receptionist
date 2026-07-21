"""Benchmark Qwen3-TTS 0.6B and 1.7B on this machine.

Downloads whichever variant isn't cached yet, then times a single synthesis
of a standard SubtoDealz-length sentence. Reports:
  - model load time (once per model)
  - synthesis wall-clock
  - audio duration
  - realtime factor (RTF) = synth_time / audio_duration
       RTF > 1.0 = slower than realtime (bad for live calls)
       RTF < 1.0 = faster than realtime (usable for live calls)

Run:
    source .venv/bin/activate
    python scripts/benchmark_qwen3_tts.py
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


MODELS = [
    "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
]

REF_AUDIO = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav"
REF_TEXT = "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it!"

TEST_SENTENCE = (
    "Hey Bob, this is Alex with SubtoDealz. I'm just reaching out about the "
    "property you have listed at 123 Elm Street. Is now a good time to talk?"
)


def pick_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if torch.backends.mps.is_available():
        return "mps", torch.float32
    return "cpu", torch.float32


def bench_one(model_id, device, dtype):
    print(f"\n=== {model_id} ===")
    print(f"loading (may download if not cached)...")
    t0 = time.time()
    try:
        model = Qwen3TTSModel.from_pretrained(
            model_id, device_map=device, dtype=dtype,
            attn_implementation="flash_attention_2",
        )
    except Exception:
        model = Qwen3TTSModel.from_pretrained(model_id, device_map=device, dtype=dtype)
    load_time = time.time() - t0
    print(f"  loaded in {load_time:.1f}s")

    # Warm run (some kernels compile on first call)
    print("  warmup synthesis...")
    t0 = time.time()
    wavs, sr = model.generate_voice_clone(
        text="Hello.", language="English",
        ref_audio=REF_AUDIO, ref_text=REF_TEXT,
    )
    warmup_time = time.time() - t0
    print(f"  warmup done in {warmup_time:.1f}s")

    # Real benchmark
    print(f"  synthesizing test sentence ({len(TEST_SENTENCE)} chars)...")
    t0 = time.time()
    wavs, sr = model.generate_voice_clone(
        text=TEST_SENTENCE, language="English",
        ref_audio=REF_AUDIO, ref_text=REF_TEXT,
    )
    synth_time = time.time() - t0

    audio_samples = len(wavs[0])
    audio_duration = audio_samples / sr
    rtf = synth_time / audio_duration

    print(f"  synthesized in {synth_time:.1f}s")
    print(f"  audio duration: {audio_duration:.2f}s ({audio_samples} samples @ {sr}Hz)")
    print(f"  RTF: {rtf:.2f}  ({'faster than realtime' if rtf < 1 else 'slower than realtime'})")

    out = REPO_ROOT / "data" / f"bench_{model_id.split('/')[-1]}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), wavs[0], sr)
    print(f"  wrote {out}")

    return {
        "model": model_id,
        "load_time_s": load_time,
        "warmup_s": warmup_time,
        "synth_s": synth_time,
        "audio_s": audio_duration,
        "rtf": rtf,
    }


def main():
    device, dtype = pick_device_and_dtype()
    print(f"device={device}  dtype={dtype}")

    if device == "cuda:0":
        props = torch.cuda.get_device_properties(0)
        print(f"gpu: {props.name}  vram: {props.total_memory / 1024**3:.1f} GB")
    elif device == "mps":
        print("gpu: Apple Silicon (MPS)")

    results = []
    for mid in MODELS:
        try:
            results.append(bench_one(mid, device, dtype))
        except Exception as e:
            print(f"FAILED on {mid}: {e}")

    print("\n\n=== SUMMARY ===")
    print(f"{'model':<40} {'load':>8} {'synth':>8} {'audio':>8} {'RTF':>6}")
    for r in results:
        print(f"{r['model']:<40} {r['load_time_s']:>7.1f}s {r['synth_s']:>7.1f}s {r['audio_s']:>7.2f}s {r['rtf']:>6.2f}")


if __name__ == "__main__":
    main()
