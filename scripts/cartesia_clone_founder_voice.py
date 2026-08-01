"""One-shot script: clone the founder's voice into Cartesia and print the ID.

Usage:
    # 1. Have a clean 5-15 second WAV of the founder speaking naturally
    # 2. CARTESIA_API_KEY must be set (or in .env)
    python scripts/cartesia_clone_founder_voice.py path/to/voice_sample.wav "Founder Voice"

Output: prints the new voice_id. Copy it into .env:
    CARTESIA_VOICE_ID=<the id>

The cloned voice is bound to your Cartesia account. Commercial-use
rights are included on paid plans (Startup $49/mo and up). Free tier
allows cloning for personal use only.

Cartesia's Instant Voice Cloning takes ~5 seconds and works on samples
as short as 3 seconds, but 5-15 seconds of clean speech gives better
naturalness. For production, upgrade to Pro Voice Cloning (submit a
longer sample, wait ~1 hour, get a higher-fidelity clone).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def main(audio_path: str, name: str = "Founder Voice") -> None:
    # Load .env if present so CARTESIA_API_KEY resolves.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        sys.exit("CARTESIA_API_KEY not set. Add it to .env or export it.")

    src = Path(audio_path)
    if not src.exists():
        sys.exit(f"file not found: {src}")
    size_kb = src.stat().st_size / 1024
    if size_kb < 20:
        print(f"warn: {src} is only {size_kb:.1f} KB — voice cloning wants at least ~3 sec of audio", file=sys.stderr)
    if size_kb > 5000:
        print(f"warn: {src} is {size_kb:.1f} KB — Cartesia recommends 5-15 seconds; longer samples may be rejected", file=sys.stderr)

    from cartesia import AsyncCartesia

    async with AsyncCartesia(api_key=api_key) as client:
        with src.open("rb") as f:
            print(f"uploading {src.name} ({size_kb:.1f} KB) to Cartesia...")
            voice = await client.voices.clone(
                clip=(src.name, f, "audio/wav"),
                name=name,
                language="en",
                description=f"Cloned from {src.name} on {os.uname().nodename}",
            )
        voice_id = getattr(voice, "id", None) or getattr(voice, "voice_id", None)
        if not voice_id:
            sys.exit(f"clone returned unexpected shape: {voice!r}")

        print()
        print("=" * 60)
        print(f"Voice cloned successfully.")
        print(f"  voice_id: {voice_id}")
        print(f"  name:     {name}")
        print()
        print("Next steps:")
        print(f"  1. Add to .env:  CARTESIA_VOICE_ID={voice_id}")
        print(f"  2. Flip:         TTS_PROVIDER=cartesia")
        print(f"  3. Restart the server. First call will use the cloned voice.")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(
            "usage: python scripts/cartesia_clone_founder_voice.py "
            "<path/to/sample.wav> [voice_name]"
        )
    path = sys.argv[1]
    display_name = sys.argv[2] if len(sys.argv) > 2 else "Founder Voice"
    asyncio.run(main(path, display_name))
