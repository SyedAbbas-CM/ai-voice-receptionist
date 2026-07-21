"""Chatterbox Turbo (MLX) local TTS adapter.

Model:   https://huggingface.co/mlx-community/chatterbox-turbo-8bit
License: MIT (commercial-safe, no royalty)
Runtime: mlx-audio (pip install mlx-audio)  — Apple MLX GPU backend

Why this over Kokoro:
- Zero-shot voice CLONING from a 5-15s reference clip (Kokoro is preset-only)
- MLX-native — sidesteps the 18 GB MPS OOM cap that killed Fish Speech
- ~500M params, ~2 GB peak RAM at 8-bit quant
- Benchmarked RTF 0.51x on M1 Pro (twice as fast as real-time)

Config via env:
  CHATTERBOX_MODEL       Repo id / path. Default: mlx-community/chatterbox-turbo-8bit
  CHATTERBOX_REF_AUDIO   Path to reference clip for cloning (WAV, 24kHz mono ideal).
                          If unset, uses model's default voice.
  CHATTERBOX_REF_TEXT    Transcript of the reference clip (required when
                          CHATTERBOX_REF_AUDIO is set).
  CHATTERBOX_TEMPERATURE Sampling temperature. Default 0.7.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.core.config import settings

from ..base import TTSProvider


log = logging.getLogger(__name__)


class ChatterboxMLX(TTSProvider):
    """Chatterbox Turbo via mlx-audio. Loads once, synthesizes many times."""

    name = "chatterbox"

    def __init__(self) -> None:
        self.model_id = getattr(settings, "chatterbox_model", None) or "mlx-community/chatterbox-turbo-8bit"
        self.ref_audio = getattr(settings, "chatterbox_ref_audio", None)
        self.ref_text = getattr(settings, "chatterbox_ref_text", None)
        self.temperature = float(getattr(settings, "chatterbox_temperature", 0.7) or 0.7)
        self._warm = False

    def _warmup(self) -> None:
        # mlx-audio loads model lazily on first call. Kick a tiny synth so the
        # weight-load latency doesn't hit the caller's first spoken turn.
        if self._warm:
            return
        try:
            from mlx_audio.tts.generate import generate_audio
            with tempfile.TemporaryDirectory() as td:
                generate_audio(
                    text="Hello.",
                    model=self.model_id,
                    output_path=td,
                    file_prefix="warmup",
                    audio_format="wav",
                    save=True,
                    verbose=False,
                )
            self._warm = True
            log.info("Chatterbox MLX warmup done")
        except Exception as e:
            log.warning("Chatterbox warmup failed (non-fatal): %s", e)

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        """Synthesize text and return (wav_bytes, mime).

        `voice` is treated as a reference-audio path override — per-call
        cloning. If unset, falls back to CHATTERBOX_REF_AUDIO env, and
        finally the model's default voice.
        """
        from mlx_audio.tts.generate import generate_audio

        ref_audio = voice or self.ref_audio
        ref_text = self.ref_text if ref_audio == self.ref_audio else None

        # mlx-audio writes files to disk; capture into memory via a temp dir.
        with tempfile.TemporaryDirectory() as td:
            generate_audio(
                text=text,
                model=self.model_id,
                ref_audio=ref_audio,
                ref_text=ref_text,
                output_path=td,
                file_prefix="synth",
                audio_format="wav",
                save=True,
                verbose=False,
                temperature=self.temperature,
            )
            # mlx-audio may add _000, _001 suffixes for multi-segment synth.
            # For our short receptionist turns it produces one file — pick the
            # first (and only) .wav.
            files = sorted(Path(td).glob("*.wav"))
            if not files:
                raise RuntimeError("Chatterbox produced no output file")
            with open(files[0], "rb") as f:
                data = f.read()
            return data, "audio/wav"
