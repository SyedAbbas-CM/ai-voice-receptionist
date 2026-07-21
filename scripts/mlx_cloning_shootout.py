"""A/B/C/D compare local MLX voice-cloning models with the same 15s British reference.

Runs each model on the same sentence, measures RTF + peak RAM + TTFA,
writes outputs to data/mlx_shootout/{model_slug}/audio_000.wav for playback.

Candidates:
  - chatterbox-turbo-8bit  (baseline — current prod)
  - confucius4-mlx-int8    (accent-preservation fix)
  - marvis-tts-250m-v0.2   (speed pick)
  - indextts2-fp16         (fidelity pick)

Run:
    source .venv/bin/activate
    python scripts/mlx_cloning_shootout.py

First-run downloads 6-12 GB total. Runs sequentially to avoid MLX contention.
"""
from __future__ import annotations

import gc
import json
import resource
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REF_AUDIO = REPO_ROOT / "data" / "voice_sample_15s.wav"
REF_TEXT = (
    "Today I'm recording a cleaner reference for the voice pipeline. "
    "I want this read to stay grounded, calm, and a little deeper than my normal speaking voice. "
    "The goal is not to sound dramatic."
)
TARGET_TEXT = (
    "Hi, thanks for calling Riverside Family Clinic. "
    "This is Alex — how can I help you today?"
)
OUT_DIR = REPO_ROOT / "data" / "mlx_shootout"


CANDIDATES = [
    # (short_slug, hf_repo_id, notes)
    ("chatterbox-turbo-8bit", "mlx-community/chatterbox-turbo-8bit",
        "Baseline — currently in prod. Known American-drift on British ref."),
    ("confucius4-int8", "mlx-community/Confucius4-TTS-mlx-int8",
        "Direct fix for cross-lingual accent drift. NetEase Youdao. Apache-2.0."),
    ("marvis-tts-250m", "Marvis-AI/marvis-tts-250m-v0.2",
        "Streaming-optimized, CSM-1B backbone. Apache-2.0. Fastest expected."),
    ("indextts2-fp16", "vanch007/mlx-indextts2-standard-fp16",
        "Independent speaker+emotion tokens. Highest raw fidelity expected. MIT."),
]


def peak_ram_mb() -> float:
    """Resident set size peak in MB — coarse but works for process-lifetime peak."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def synth_one(slug: str, model_id: str, notes: str) -> dict:
    from mlx_audio.tts.generate import generate_audio
    import soundfile as sf

    print()
    print("=" * 70)
    print(f"[{slug}]  {model_id}")
    print(f"           {notes}")
    print("=" * 70)

    out_dir = OUT_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    ram_before = peak_ram_mb()
    t0 = time.time()
    try:
        generate_audio(
            text=TARGET_TEXT,
            model=model_id,
            ref_audio=str(REF_AUDIO),
            ref_text=REF_TEXT,
            output_path=str(out_dir),
            file_prefix="audio",
            audio_format="wav",
            save=True,
            verbose=False,
        )
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  ✗ FAILED after {elapsed:.1f}s: {e.__class__.__name__}: {e}")
        return {
            "slug": slug, "model_id": model_id,
            "ok": False, "error": str(e),
            "elapsed_s": elapsed,
        }

    elapsed = time.time() - t0
    ram_after = peak_ram_mb()
    ram_delta = ram_after - ram_before

    # Find the output file (mlx-audio names it audio_000.wav)
    wavs = sorted(out_dir.glob("*.wav"))
    if not wavs:
        print(f"  ✗ FAILED — no wav emitted")
        return {"slug": slug, "model_id": model_id, "ok": False,
                "error": "no wav produced", "elapsed_s": elapsed}

    output_path = wavs[0]
    audio_info = sf.info(str(output_path))
    audio_s = audio_info.duration
    rtf = elapsed / audio_s if audio_s > 0 else float("inf")

    print(f"  ✓ ok")
    print(f"    output:      {output_path.relative_to(REPO_ROOT)}")
    print(f"    synth time:  {elapsed:.2f}s")
    print(f"    audio dur:   {audio_s:.2f}s")
    print(f"    RTF:         {rtf:.2f}  {'(REAL-TIME VIABLE)' if rtf < 1 else '(slower than realtime)'}")
    print(f"    peak RAM:    {ram_after:.0f} MB (+{ram_delta:.0f} MB since prior)")
    print(f"    sample rate: {audio_info.samplerate} Hz")

    return {
        "slug": slug, "model_id": model_id, "ok": True,
        "output_path": str(output_path.relative_to(REPO_ROOT)),
        "elapsed_s": round(elapsed, 2),
        "audio_s": round(audio_s, 2),
        "rtf": round(rtf, 2),
        "peak_ram_mb": round(ram_after, 0),
        "ram_delta_mb": round(ram_delta, 0),
        "sample_rate": audio_info.samplerate,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Reference: {REF_AUDIO}")
    print(f"Target:    {TARGET_TEXT!r}")
    print(f"Output:    {OUT_DIR}")
    print()

    results = []
    for slug, model_id, notes in CANDIDATES:
        try:
            r = synth_one(slug, model_id, notes)
        except KeyboardInterrupt:
            print("\n[shootout] interrupted — writing partial results")
            break
        except Exception as e:
            print(f"[shootout] unexpected: {e}")
            r = {"slug": slug, "model_id": model_id, "ok": False,
                 "error": f"outer: {e.__class__.__name__}: {e}"}
        results.append(r)
        # Nudge MLX to release model between runs so RAM measurements are meaningful
        gc.collect()

    # Summary table
    print()
    print("=" * 70)
    print("SHOOTOUT SUMMARY")
    print("=" * 70)
    print(f"{'model':<28} {'ok':<4} {'rtf':<7} {'time':<8} {'audio':<7} {'ram':<10}")
    print("-" * 70)
    for r in results:
        if r.get("ok"):
            print(f"{r['slug']:<28} ✓    {r['rtf']:<7.2f} {r['elapsed_s']:<8.2f} {r['audio_s']:<7.2f} {r['peak_ram_mb']:.0f} MB")
        else:
            print(f"{r['slug']:<28} ✗    error: {r.get('error', 'unknown')[:80]}")
    print()

    # Write JSON for grepping later
    summary_path = OUT_DIR / "shootout_results.json"
    summary_path.write_text(json.dumps({
        "target_text": TARGET_TEXT,
        "ref_audio": str(REF_AUDIO),
        "results": results,
    }, indent=2))
    print(f"[shootout] wrote {summary_path.relative_to(REPO_ROOT)}")
    print()
    print("Play all outputs to A/B/C:")
    for r in results:
        if r.get("ok"):
            print(f"  open '{REPO_ROOT / r['output_path']}'")


if __name__ == "__main__":
    sys.exit(main() or 0)
