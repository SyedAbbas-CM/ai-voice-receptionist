"""S13-A: Prosodic end-of-turn detector using Pipecat smart-turn-v3.

Runs LOCALLY (M1 Neural Engine or CPU) via ONNX Runtime.  8 MB model,
BSD-2 license, ~12ms per inference.  Replaces silence-based endpointing
as the primary "caller is done" signal.

Model:
    pipecat-ai/smart-turn-v3 (huggingface.co)
    Input: 16 kHz mono PCM, up to 8 seconds → log-mel spectrogram
           (80 mel bins × 800 frames, matches Whisper-Tiny preprocessing)
    Output: single logit → sigmoid → P(end_of_turn) ∈ [0, 1]

Usage:
    detector = SmartTurnDetector()   # lazy-loads model on first call
    p_eot = detector.predict(pcm16_bytes_int16_le, sample_rate=16000)
    if p_eot > 0.7:
        # caller is done → commit turn / fire brain speculatively

The detector is CALL-agnostic — one instance shared across all calls.
Thread-safe: onnxruntime.InferenceSession is thread-safe for read.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


# Tuning constants matched to Whisper-Tiny mel spectrogram parameters.
# Do not change these without also retraining or swapping the model.
_SAMPLE_RATE = 16000
_N_MELS = 80
_N_FFT = 400          # 25 ms window at 16 kHz
_HOP_LENGTH = 160     # 10 ms hop
_N_FRAMES = 800       # 8 seconds max input
_MAX_SAMPLES = _HOP_LENGTH * _N_FRAMES  # 128 000 samples = 8 s

# Threshold tuning.  Pipecat's default is 0.5.  Bumping to 0.7 makes
# us more conservative (fewer premature commits, slightly slower turns).
# For a receptionist that's the right trade.
DEFAULT_EOT_THRESHOLD = 0.7

# Model cache location — matches HF hub_download default.
_MODEL_REPO = "pipecat-ai/smart-turn-v3"
_MODEL_FILE = "smart-turn-v3.2-cpu.onnx"


class SmartTurnDetector:
    """Wraps the ONNX session + audio preprocessing.  Single instance;
    concurrent inference is safe under CPUExecutionProvider."""

    _singleton_lock = threading.Lock()
    _singleton: Optional["SmartTurnDetector"] = None

    def __init__(self, model_path: Optional[str] = None) -> None:
        # Heavy imports deferred so package loads without them
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        self._np = np
        self._ort = ort

        if model_path is None:
            cache_dir = os.environ.get(
                "SMART_TURN_MODEL_DIR",
                os.path.join(os.getcwd(), "data", "models"),
            )
            model_path = hf_hub_download(
                repo_id=_MODEL_REPO,
                filename=_MODEL_FILE,
                cache_dir=cache_dir,
            )
        self._model_path = model_path

        # CPU-only.  CoreML backend fails to compile some Whisper-Tiny
        # ops (2026-08-06 test on M1 Pro: MLModel build error -14 on
        # smart-turn-v3.2-cpu.onnx).  CPU inference is ~12ms — well
        # within our budget.  Reconsider if we hit throughput issues.
        providers = ["CPUExecutionProvider"]
        self._session = ort.InferenceSession(model_path, providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        log.info(
            "smart-turn-v3 loaded model=%s providers=%s (input=%s)",
            os.path.basename(model_path),
            [p if isinstance(p, str) else p[0] for p in providers],
            self._input_name,
        )

        # Pre-build mel filterbank for reuse
        import librosa
        self._mel_basis = librosa.filters.mel(
            sr=_SAMPLE_RATE, n_fft=_N_FFT, n_mels=_N_MELS,
        ).astype(np.float32)

    @classmethod
    def get(cls) -> "SmartTurnDetector":
        """Return the process-wide singleton, constructing on first call."""
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def _pcm16_to_mel(self, pcm16: bytes) -> "np.ndarray":
        """Convert int16-LE PCM bytes → 80x800 log-mel spectrogram.

        Truncates or pads to exactly 8 seconds of audio."""
        np = self._np
        # Bytes → int16 → float32 in [-1, 1]
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0

        # Clip / left-pad to _MAX_SAMPLES (keep the MOST RECENT 8 sec —
        # that's where end-of-turn signal lives).
        if len(samples) > _MAX_SAMPLES:
            samples = samples[-_MAX_SAMPLES:]
        elif len(samples) < _MAX_SAMPLES:
            pad = _MAX_SAMPLES - len(samples)
            samples = np.pad(samples, (pad, 0), mode="constant")

        # STFT magnitude → mel → log
        # Use manual STFT via np.fft to avoid an extra scipy dep.
        # Frame the signal (n_frames = _N_FRAMES + 1; drop last for exact 800).
        # Simpler: librosa.stft
        import librosa
        stft = librosa.stft(
            samples, n_fft=_N_FFT, hop_length=_HOP_LENGTH,
            win_length=_N_FFT, center=True, pad_mode="reflect",
        )
        power = np.abs(stft) ** 2  # shape (n_fft/2+1, n_frames)
        mel = self._mel_basis @ power  # (80, n_frames)
        # Trim / pad to exactly _N_FRAMES
        if mel.shape[1] > _N_FRAMES:
            mel = mel[:, -_N_FRAMES:]
        elif mel.shape[1] < _N_FRAMES:
            mel = np.pad(mel, ((0, 0), (0, _N_FRAMES - mel.shape[1])),
                         mode="constant")
        log_mel = np.log(np.clip(mel, 1e-10, None)).astype(np.float32)
        # Shape (1, 80, 800) — batch dim
        return log_mel[np.newaxis, :, :]

    def predict(self, pcm16: bytes, sample_rate: int = _SAMPLE_RATE) -> float:
        """Return P(end_of_turn) ∈ [0, 1] for the given PCM audio.

        pcm16: int16 little-endian mono at `sample_rate` Hz.
               If sample_rate != 16000, caller must resample first —
               we don't do it here to avoid a hot-path scipy import."""
        if sample_rate != _SAMPLE_RATE:
            raise ValueError(
                f"smart-turn expects {_SAMPLE_RATE}Hz PCM, got {sample_rate}Hz. "
                f"Resample before calling."
            )
        if not pcm16 or len(pcm16) < 2:
            return 0.0
        mel = self._pcm16_to_mel(pcm16)
        outputs = self._session.run(None, {self._input_name: mel})
        logit = float(outputs[0][0][0])
        # sigmoid
        import math
        return 1.0 / (1.0 + math.exp(-logit))


# Convenience free-function so callers don't need to import the class
def predict_end_of_turn(pcm16: bytes, sample_rate: int = _SAMPLE_RATE) -> float:
    """Shorthand: SmartTurnDetector.get().predict(...)."""
    return SmartTurnDetector.get().predict(pcm16, sample_rate)
