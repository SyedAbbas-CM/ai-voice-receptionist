"""Qwen3-TTS local adapter.

Model: https://huggingface.co/collections/Qwen/qwen3-tts
License: Apache-2.0 (verify per weight variant on HF).

Two modes:
  - **preset voice**: use the CustomVoice model + a named speaker (Vivian, etc)
  - **cloning**: use the Base model + reference audio path/URL/bytes

Config via env:
  QWEN3_TTS_MODEL_ID          e.g. Qwen/Qwen3-TTS-12Hz-1.7B-Base (cloning)
                              or  Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice (preset)
  QWEN3_TTS_DEVICE            "auto" | "cuda:0" | "mps" | "cpu"
  QWEN3_TTS_DTYPE             "bfloat16" | "float16" | "float32"
  QWEN3_TTS_DEFAULT_SPEAKER   preset speaker name (CustomVoice models)
  QWEN3_TTS_DEFAULT_LANGUAGE  "English" | "Chinese" | ...
  QWEN3_TTS_INSTRUCT          optional style prompt (CustomVoice only)
  QWEN3_TTS_REF_AUDIO         default reference audio path/URL (Base models)
  QWEN3_TTS_REF_TEXT          transcript of default reference audio

Per-call cloning:
  If the model is a Base (cloning) variant, callers can pass `voice=<url-or-path>`
  to override the env default per synthesize() call. If nothing is set anywhere,
  we fall back to Qwen's own public sample so a demo works with zero config.

Deps:
  pip install qwen-tts soundfile torch
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from app.core.config import settings

from ..base import TTSProvider

log = logging.getLogger(__name__)


# Qwen publishes a public sample reference clip so the cloning API is usable
# with zero configuration. Keep in sync with the model card examples.
QWEN_DEMO_REF_AUDIO = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav"
QWEN_DEMO_REF_TEXT = (
    "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it!"
)


class Qwen3TTS(TTSProvider):
    name = "qwen3"

    def __init__(self) -> None:
        self.model_id = settings.qwen3_tts_model_id or "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        # Auto-pick a sensible device if the user didn't override.
        configured_device = settings.qwen3_tts_device
        if not configured_device or configured_device == "auto":
            try:
                import torch
                if torch.cuda.is_available():
                    self.device = "cuda:0"
                elif torch.backends.mps.is_available():
                    self.device = "mps"
                else:
                    self.device = "cpu"
            except ImportError:
                self.device = "cpu"
        else:
            self.device = configured_device
        # MPS + float16 hits NaN/Inf in Qwen3-TTS sampling. Force float32 on MPS.
        # bfloat16 isn't supported on MPS at all.
        default_dtype = "float32" if self.device == "mps" else "bfloat16"
        self.dtype_name = (settings.qwen3_tts_dtype or default_dtype).lower()
        if self.device == "mps" and self.dtype_name in ("float16", "bfloat16"):
            log.warning("Qwen3-TTS on MPS forced to float32 (float16/bfloat16 cause NaN)")
            self.dtype_name = "float32"
        self.default_speaker = settings.qwen3_tts_default_speaker or "Vivian"
        self.default_language = settings.qwen3_tts_default_language or "English"
        self.default_instruct = settings.qwen3_tts_instruct
        self._model = None
        self._is_clone_model = "Base" in (self.model_id or "")

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
        except ImportError as e:
            raise RuntimeError(
                "Qwen3-TTS requires `pip install qwen-tts torch soundfile`. "
                f"Missing: {e.name}"
            ) from e

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(self.dtype_name, torch.bfloat16)

        # Fall back to CPU if requested GPU isn't available
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            log.warning("Qwen3-TTS: cuda requested but not available, falling back to cpu")
            self.device = "cpu"
            dtype = torch.float32
        if self.device == "mps" and not torch.backends.mps.is_available():
            log.warning("Qwen3-TTS: mps requested but not available, falling back to cpu")
            self.device = "cpu"
            dtype = torch.float32

        kwargs = {"device_map": self.device, "dtype": dtype}
        try:
            self._model = Qwen3TTSModel.from_pretrained(
                self.model_id, attn_implementation="flash_attention_2", **kwargs
            )
        except (ImportError, RuntimeError, ValueError) as e:
            log.warning("Qwen3-TTS: flash-attn unavailable (%s); loading with default attn", e)
            self._model = Qwen3TTSModel.from_pretrained(self.model_id, **kwargs)

        return self._model

    def _to_wav_bytes(self, wav, sample_rate: int) -> bytes:
        import soundfile as sf
        buf = io.BytesIO()
        sf.write(buf, wav, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        """Synthesize text with the configured Qwen3 model.

        For Base (cloning) models, `voice` is treated as a reference-audio
        path/URL — the paired ref_text must come from env
        (QWEN3_TTS_REF_TEXT). If both env vars are unset and no override was
        passed, we fall back to Qwen's public demo sample.

        For CustomVoice models, `voice` is a preset speaker name (Vivian,
        Adam, etc); ref_audio inputs are ignored.
        """
        model = self._load()

        if self._is_clone_model:
            ref_audio = voice or settings.qwen3_tts_ref_audio or QWEN_DEMO_REF_AUDIO
            ref_text = settings.qwen3_tts_ref_text or (
                QWEN_DEMO_REF_TEXT if ref_audio == QWEN_DEMO_REF_AUDIO else None
            )
            if not ref_text:
                raise RuntimeError(
                    "Qwen3-TTS cloning: ref_audio was overridden per-call but "
                    "QWEN3_TTS_REF_TEXT is not set. Use synthesize_clone(text, "
                    "ref_audio, ref_text) to pass both together, or set env."
                )
            wavs, sr = model.generate_voice_clone(
                text=text,
                language=self.default_language,
                ref_audio=ref_audio,
                ref_text=ref_text,
            )
        else:
            speaker = voice or self.default_speaker
            wavs, sr = model.generate_custom_voice(
                text=text,
                language=self.default_language,
                speaker=speaker,
                instruct=self.default_instruct,
            )

        wav_bytes = self._to_wav_bytes(wavs[0], sr)
        return wav_bytes, "audio/wav"

    async def synthesize_clone(
        self,
        text: str,
        ref_audio: str,
        ref_text: str,
        language: Optional[str] = None,
    ) -> tuple[bytes, str]:
        """Explicit voice-clone call. Base models only.

        Callers (like the local Vapi emulator) that already have a specific
        ref clip + transcript pass both directly and avoid the env-var round
        trip."""
        if not self._is_clone_model:
            raise RuntimeError(
                f"synthesize_clone requires a Base variant; current model_id={self.model_id!r}"
            )
        model = self._load()
        wavs, sr = model.generate_voice_clone(
            text=text,
            language=language or self.default_language,
            ref_audio=ref_audio,
            ref_text=ref_text,
        )
        return self._to_wav_bytes(wavs[0], sr), "audio/wav"
