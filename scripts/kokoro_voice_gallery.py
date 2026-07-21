"""Synthesize the same clinic greeting in every Kokoro preset voice
so you can A/B them.

Writes each WAV to data/kokoro_voices/<voice>.wav — open the folder in
Finder and click through them.

Run:
    source .venv/bin/activate
    python scripts/kokoro_voice_gallery.py
    open data/kokoro_voices/
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

GREETING = "Hi, thanks for calling Riverside Family Clinic. How can I help you today?"

# Curated shortlist — most receptionist-appropriate voices
SHORTLIST = [
    "af_heart",     # American female — default, neutral
    "af_bella",     # American female — warm
    "af_nicole",    # American female — clear
    "af_sarah",     # American female — professional
    "af_river",     # American female — friendly
    "am_adam",      # American male — clear
    "am_michael",   # American male — mature
    "am_puck",      # American male — younger
    "bf_emma",      # British female — calm
    "bm_george",    # British male — professional
]


async def main() -> int:
    from app.core.config import settings
    from app.providers.tts.kokoro_tts import KokoroTTS

    # Use ONNX backend for speed
    settings.kokoro_backend = "onnx"
    tts = KokoroTTS()

    print("[gallery] loading Kokoro (once)")
    tts._load()
    print(f"[gallery] active backend: {tts._active_backend}")

    out_dir = REPO_ROOT / "data" / "kokoro_voices"
    out_dir.mkdir(parents=True, exist_ok=True)

    import time
    for voice in SHORTLIST:
        t0 = time.time()
        audio, mime = await tts.synthesize(GREETING, voice=voice)
        dt = time.time() - t0
        out = out_dir / f"{voice}.wav"
        out.write_bytes(audio)
        print(f"  {voice:12s} -> {out.name} ({dt:.1f}s)")

    print(f"\n[gallery] wrote {len(SHORTLIST)} clips to {out_dir}")
    print(f"[gallery] open in Finder: open {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
