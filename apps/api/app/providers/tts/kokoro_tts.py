"""Kokoro-82M local TTS adapter.

Model: https://huggingface.co/hexgrad/Kokoro-82M
License: Apache-2.0
Repo:    https://github.com/hexgrad/kokoro
PyPI:    https://pypi.org/project/kokoro/

Why Kokoro over Qwen3-TTS on M1 Pro:
- 82M params vs 600M (7x smaller)
- ~0.3-0.5 RTF on M1 Pro MPS vs ~4.0 for Qwen3-TTS (10x faster)
- Ranked #1 open TTS on TTS Arena for months
- Apache-2.0 (commercial safe)
- 54 preset voices in 8 languages via voice packs
- Uses iSTFT decoder that's stable on MPS float32 (no NaN issues)

Requires:
- brew install espeak-ng   (system-level phonemizer for English G2P)
- pip install kokoro       (Python package)

Config via env:
  KOKORO_VOICE       Preset voice code. af_heart (default), am_adam,
                     bf_emma, bm_george, etc. See voice pack index at
                     https://huggingface.co/hexgrad/Kokoro-82M
  KOKORO_DEVICE      "mps" | "cuda" | "cpu" | "auto" (default: auto)
  KOKORO_LANG        Single letter language code: 'a' (American English),
                     'b' (British English), 'j' (Japanese), 'z' (Mandarin),
                     'f' (French), 'h' (Hindi), 'i' (Italian), 'p' (Portuguese).
                     Default: 'a'.
  KOKORO_SAMPLE_RATE Output sample rate. Default 24000 (Kokoro's native).

No voice cloning — Kokoro is preset-only. For cloning, use Qwen3-TTS-Base.
"""
from __future__ import annotations

import io
import logging
import wave
from typing import Optional

from app.core.config import settings

from ..base import TTSProvider


log = logging.getLogger(__name__)


class KokoroTTS(TTSProvider):
    name = "kokoro"

    # Voices that ship with Kokoro. Users can pass any of these as the
    # `voice` argument to synthesize() OR set KOKORO_VOICE in env.
    # Naming: <lang><gender>_<name>  → af = American Female, am = American Male,
    #                                   bf = British Female, bm = British Male
    KNOWN_VOICES = {
        "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica",
        "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
        "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
        "am_michael", "am_onyx", "am_puck", "am_santa",
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    }

    def __init__(self) -> None:
        self.voice = settings.kokoro_voice or "af_heart"
        self.lang = settings.kokoro_lang or "a"
        self.sample_rate = int(settings.kokoro_sample_rate or 24000)

        # Backend: "onnx" (CoreML on Mac, ~0.15 RTF), "pytorch" (native, ~2.5 RTF on M1),
        # or "auto" — prefer onnx if available.
        self.backend = (settings.kokoro_backend or "auto").lower()

        # Auto-pick device: MPS > CUDA > CPU (only relevant for pytorch backend)
        configured_device = settings.kokoro_device or "auto"
        if configured_device == "auto":
            try:
                import torch
                if torch.cuda.is_available():
                    self.device = "cuda"
                elif torch.backends.mps.is_available():
                    self.device = "mps"
                else:
                    self.device = "cpu"
            except ImportError:
                self.device = "cpu"
        else:
            self.device = configured_device

        self._pipeline = None
        self._onnx_model = None
        self._active_backend: Optional[str] = None  # set after _load

    def _load(self):
        if self._pipeline is not None or self._onnx_model is not None:
            return

        backend = self.backend
        if backend == "auto":
            # Prefer ONNX (10x faster on M1). Fall back to pytorch if it can't load.
            try:
                self._load_onnx()
                self._active_backend = "onnx"
                return
            except Exception as e:
                log.warning("Kokoro ONNX backend unavailable (%s); using pytorch fallback", e)
                self._load_pytorch()
                self._active_backend = "pytorch"
                return
        if backend == "onnx":
            self._load_onnx()
            self._active_backend = "onnx"
            return
        if backend == "pytorch":
            self._load_pytorch()
            self._active_backend = "pytorch"
            return
        raise RuntimeError(f"unknown KOKORO_BACKEND={backend!r}; use auto|onnx|pytorch")

    def _load_onnx(self):
        """Load the CoreML/ONNX backend. Model + voices files download to a
        local `~/.cache/kokoro-onnx/` dir on first use (~350 MB total)."""
        try:
            from kokoro_onnx import Kokoro
        except ImportError as e:
            raise RuntimeError(
                "Kokoro ONNX backend needs `pip install kokoro-onnx onnxruntime`."
            ) from e

        # kokoro_onnx auto-downloads the model + voice pack on first run;
        # newer versions accept from_pretrained-style loading. Fall back to
        # explicit URL download if the API differs.
        model_path, voices_path = self._ensure_onnx_files()
        log.info("loading Kokoro ONNX model=%s voices=%s", model_path, voices_path)
        self._onnx_model = Kokoro(model_path, voices_path)

    def _ensure_onnx_files(self) -> tuple[str, str]:
        """Download the ONNX weights + voice pack if not already cached."""
        import os
        import urllib.request

        cache_dir = os.path.expanduser("~/.cache/kokoro-onnx")
        os.makedirs(cache_dir, exist_ok=True)

        model_path = os.path.join(cache_dir, "kokoro-v1.0.onnx")
        voices_path = os.path.join(cache_dir, "voices-v1.0.bin")

        urls = {
            model_path: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
            voices_path: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        }
        for path, url in urls.items():
            if os.path.exists(path):
                continue
            log.info("downloading %s -> %s", url, path)
            urllib.request.urlretrieve(url, path)
        return model_path, voices_path

    def _load_pytorch(self):
        """Load the original PyTorch KPipeline. Slower on M1 (~2.5 RTF) but
        works when ONNX isn't installed."""
        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise RuntimeError(
                "Kokoro TTS requires `pip install kokoro` and system `espeak-ng` "
                "(brew install espeak-ng on macOS). "
                f"Missing: {e.name}"
            ) from e

        log.info("loading Kokoro pipeline lang=%r device=%r", self.lang, self.device)
        # KPipeline downloads the ~300 MB model on first use to HF cache.
        try:
            self._pipeline = KPipeline(lang_code=self.lang, device=self.device)
        except Exception as e:
            log.warning("Kokoro on device=%s failed (%s); falling back to CPU", self.device, e)
            self._pipeline = KPipeline(lang_code=self.lang, device="cpu")
            self.device = "cpu"

    def _pcm_to_wav_bytes(self, samples, sample_rate: int) -> bytes:
        """Convert Kokoro's float32 numpy array to 16-bit PCM WAV bytes.

        Kokoro returns audio as float32 in [-1, 1]. We scale to int16 and
        wrap in a WAV container so downstream callers (browser sim, Twilio
        route, ElevenLabs-compat facade) all consume the same shape."""
        import numpy as np

        # Ensure numpy array on CPU
        if hasattr(samples, "detach"):
            samples = samples.detach().cpu().numpy()
        arr = np.asarray(samples, dtype="float32")
        # Clip to safe range then scale
        arr = np.clip(arr, -1.0, 1.0)
        pcm16 = (arr * 32767.0).astype("<i2").tobytes()

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm16)
        return buf.getvalue()

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        """Synthesize `text` with the configured or overridden voice.

        Routes to whichever backend loaded (`_active_backend`):
        - onnx: single-shot synthesis via kokoro_onnx.Kokoro.create()
        - pytorch: chunked via KPipeline generator
        """
        import numpy as np

        self._load()  # side-effect: sets _active_backend + one of the two model handles
        chosen_voice = voice or self.voice
        if chosen_voice not in self.KNOWN_VOICES:
            log.warning(
                "Kokoro voice %r not in known set; passing through anyway. "
                "Set KOKORO_VOICE to one of: %s",
                chosen_voice, ", ".join(sorted(self.KNOWN_VOICES)[:6]) + ", ..."
            )

        chunks = []
        try:
            if self._active_backend == "onnx":
                # kokoro_onnx returns (samples, sample_rate) in one call
                samples, sr = self._onnx_model.create(text, voice=chosen_voice, speed=1.0, lang="en-us")
                self.sample_rate = int(sr)
                chunks.append(np.asarray(samples, dtype="float32"))
            else:
                for result in self._pipeline(text, voice=chosen_voice):
                    audio = result[-1] if isinstance(result, tuple) else getattr(result, "audio", None)
                    if audio is None:
                        continue
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    chunks.append(np.asarray(audio, dtype="float32"))
        except Exception as e:
            log.exception("Kokoro synthesis failed: %s", e)
            raise

        if not chunks:
            # Empty output — return a silence WAV so callers don't crash
            return self._pcm_to_wav_bytes(np.zeros(int(self.sample_rate * 0.2), dtype="float32"), self.sample_rate), "audio/wav"

        joined = np.concatenate(chunks)
        return self._pcm_to_wav_bytes(joined, self.sample_rate), "audio/wav"
