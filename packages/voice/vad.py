"""Voice Activity Detection.

The Twilio route used to gate speech on `audioop.rms > 500`, which:
- fires on breaths, mouse clicks, and line hum
- misses quiet speakers entirely
- can't distinguish a "thinking pause" from "end of turn"

Silero VAD v5 is a 2MB ONNX model that classifies 30ms audio frames as
speech vs silence in < 1ms per frame. It's the industry default for
open-source voice agents (LiveKit, Pipecat, Vocode all ship it as the
default). MIT licensed.

We keep the RMS fallback so the repo boots even without onnxruntime.
Both implement the same VoiceActivityDetector interface so the Twilio
route doesn't care which one is loaded.

Usage:
    vad = build_vad(kind="silero")   # or "rms" for fallback
    is_speech = vad.is_speech(frame_bytes_mulaw, sample_rate=8000, mime="mulaw")
"""
from __future__ import annotations

import audioop
import io
import logging
import wave
from abc import ABC, abstractmethod
from typing import Optional


log = logging.getLogger(__name__)


class VoiceActivityDetector(ABC):
    """Shared interface for VAD backends."""

    name: str = "base"

    @abstractmethod
    def is_speech(
        self,
        frame_bytes: bytes,
        sample_rate: int = 8000,
        mime: str = "audio/mulaw",
    ) -> bool:
        """Return True if the frame contains speech.

        `frame_bytes` should be one atomic frame (10-30ms at the sample_rate).
        Longer frames are OK — SileroVAD averages over the window.
        `mime` accepts 'audio/mulaw', 'audio/pcm', 'audio/wav'. µ-law is
        converted to PCM16 inside the detector.
        """


class RmsVAD(VoiceActivityDetector):
    """Zero-dep fallback. RMS-threshold energy detector — same behavior as
    the original Twilio route. Kept so the app still works without
    onnxruntime installed."""

    name = "rms"

    def __init__(self, threshold: int = 500) -> None:
        self.threshold = threshold

    def is_speech(
        self,
        frame_bytes: bytes,
        sample_rate: int = 8000,
        mime: str = "audio/mulaw",
    ) -> bool:
        if not frame_bytes:
            return False
        pcm = _to_pcm16(frame_bytes, mime)
        rms = audioop.rms(pcm, 2)
        return rms > self.threshold


class SileroVAD(VoiceActivityDetector):
    """Silero VAD v5 via torch.hub or onnxruntime.

    Lazy-loads on first call so the module can be imported even when
    the deps aren't installed. If load fails (no onnxruntime, no network
    for torch.hub), the caller should fall back to RmsVAD via build_vad().
    """

    name = "silero"

    # Silero's model is trained at 8kHz and 16kHz. We upsample any other rate
    # to 16kHz (better accuracy on modern models even though 8kHz is supported).
    _TARGET_SAMPLE_RATE = 16000

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold
        self._model = None
        self._get_speech_prob = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
        except ImportError as e:
            raise RuntimeError(
                "SileroVAD needs torch. Install with `pip install torch` "
                "or fall back to RmsVAD via build_vad(kind='rms')."
            ) from e

        try:
            model, _utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
                verbose=False,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to load Silero VAD from torch.hub: {e}. "
                "Check network or fall back to RmsVAD."
            ) from e

        self._model = model
        # v5 API: model(audio_tensor, sample_rate) → speech probability (0-1)
        self._torch = torch

    def is_speech(
        self,
        frame_bytes: bytes,
        sample_rate: int = 8000,
        mime: str = "audio/mulaw",
    ) -> bool:
        if not frame_bytes:
            return False
        self._load()

        pcm = _to_pcm16(frame_bytes, mime)
        # Silero needs at least 512 samples at 16kHz (32ms) — pad with zeros
        # if the caller sent a shorter frame.
        target_min_bytes = 512 * 2  # 16-bit samples
        if len(pcm) < target_min_bytes:
            pcm = pcm + b"\x00" * (target_min_bytes - len(pcm))

        if sample_rate != self._TARGET_SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sample_rate, self._TARGET_SAMPLE_RATE, None)

        torch = self._torch
        # Convert PCM16 → normalized float32 tensor
        import numpy as np
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)

        # Silero v5 works in 512-sample chunks at 16kHz. Average speech prob
        # across all complete chunks in the frame.
        chunk_size = 512
        probs = []
        for start in range(0, len(tensor) - chunk_size + 1, chunk_size):
            chunk = tensor[start:start + chunk_size]
            with torch.no_grad():
                p = self._model(chunk, self._TARGET_SAMPLE_RATE).item()
            probs.append(p)
        if not probs:
            return False
        avg_prob = sum(probs) / len(probs)
        return avg_prob >= self.threshold


def build_vad(kind: str = "auto", **kwargs) -> VoiceActivityDetector:
    """Factory: 'silero', 'rms', or 'auto' (try silero, fall back to rms).

    Called from the Twilio route (and eventually the WhatsApp/browser
    routes) so the choice can be flipped via env without editing the
    call sites."""
    kind = (kind or "auto").lower()
    if kind == "rms":
        return RmsVAD(**kwargs)
    if kind == "silero":
        vad = SileroVAD(**kwargs)
        # Force load now so we fail fast if deps are missing
        vad._load()
        return vad
    if kind == "auto":
        try:
            vad = SileroVAD(**kwargs)
            vad._load()
            return vad
        except Exception as e:
            log.warning("Silero VAD unavailable (%s), falling back to RMS", e)
            return RmsVAD()
    raise ValueError(f"unknown VAD kind: {kind!r}")


# ---------------------------------------------------------------------------
# Audio format conversion helpers
# ---------------------------------------------------------------------------

def _to_pcm16(frame_bytes: bytes, mime: str) -> bytes:
    """Convert an incoming frame to 16-bit signed PCM.

    Handles µ-law (Twilio phone), raw PCM, and WAV-wrapped PCM. Anything
    else falls through as-is and the caller gets whatever behavior torch
    or audioop produces (usually garbage but doesn't crash)."""
    m = (mime or "").lower()
    if "mulaw" in m or "ulaw" in m or "g711" in m:
        return audioop.ulaw2lin(frame_bytes, 2)
    if "alaw" in m:
        return audioop.alaw2lin(frame_bytes, 2)
    if "wav" in m:
        try:
            with wave.open(io.BytesIO(frame_bytes), "rb") as w:
                pcm = w.readframes(w.getnframes())
                if w.getsampwidth() == 1:
                    pcm = audioop.lin2lin(pcm, 1, 2)
                elif w.getsampwidth() == 4:
                    pcm = audioop.lin2lin(pcm, 4, 2)
                if w.getnchannels() == 2:
                    pcm = audioop.tomono(pcm, 2, 1, 1)
                return pcm
        except (wave.Error, EOFError):
            return frame_bytes  # not really WAV — pass through
    # Assume already PCM16 mono
    return frame_bytes
