from __future__ import annotations

import io
import logging
import wave
from typing import Optional

from app.core.config import settings

from ..base import STTProvider


log = logging.getLogger(__name__)


# Same filter as GroqSTT — Whisper training-set filler that shows up when the
# audio is near-silent. Kept in sync between adapters on purpose so switching
# STT_PROVIDER doesn't change filter behavior.
_WHISPER_HALLUCINATIONS = frozenset({
    "you", "you.", "bye", "bye.", "thanks.", "thank you.", "thank you",
    "thanks for watching!", "thanks for watching.",
    "please subscribe.", "subscribe.",
    "oh my god", "oh my god.", "oh, my god", "oh my god!",
    ".", "!", "?", "-",
})


class LocalWhisperSTT(STTProvider):
    """faster-whisper for fully local transcription.

    Runs on M1 Pro at ~5-10x realtime for the `small.en` model (int8), meaning
    a 3-second clip transcribes in ~0.3s. That's comfortable for a live demo.

    Config:
        LOCAL_WHISPER_MODEL     tiny.en | base.en | small.en | medium.en |
                                large-v3 | distil-large-v3 (default: base.en)
        LOCAL_WHISPER_COMPUTE   int8 | int8_float16 | float16 | float32
                                (default: int8 — best speed/quality on CPU/MPS)

    Recommended for M1 Pro / 16 GB RAM: LOCAL_WHISPER_MODEL=small.en,
    LOCAL_WHISPER_COMPUTE=int8. Loads in ~5s, transcribes in ~0.5s per turn,
    accuracy indistinguishable from Groq's whisper-large-v3-turbo for
    conversational English.

    Streaming (transcribe_stream): approximates streaming by transcribing a
    rolling window every ~600ms and emitting STTEvent(kind='partial'). On
    ~700ms of silence (Silero VAD), transcribes the full utterance one more
    time and emits STTEvent(kind='final'). Good enough for barge-in +
    turn-taking without needing a cloud STT.
    """

    name = "local"
    supports_streaming = True

    _MIN_AUDIO_BYTES = 4000  # ~0.25s at 16kHz mono — reject tap-and-release blips

    def __init__(self) -> None:
        self.model_name = settings.local_whisper_model or "base.en"
        self.compute_type = settings.local_whisper_compute or "int8"
        self._model: Optional[object] = None

    def _load(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info(
                "loading faster-whisper model=%s compute=%s (first run downloads to HF cache)",
                self.model_name, self.compute_type,
            )
            # device="cpu" — faster-whisper does not support MPS today; it uses
            # OpenMP-parallel int8 GEMMs on the M1's perf cores instead. That's
            # actually faster than MPS float16 for the small models we use.
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type,
            )
        return self._model

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        if not audio_bytes or len(audio_bytes) < self._MIN_AUDIO_BYTES:
            return ""

        model = self._load()

        # DEBUG: dump the raw browser audio so we can hear what the server actually
        # received when transcription returns empty. Toggle off in prod.
        import time
        dbg_path = f"/tmp/stt_debug_{int(time.time())}.bin"
        try:
            with open(dbg_path, "wb") as f:
                f.write(audio_bytes)
            log.info("STT input: %d bytes, mime=%s -> saved %s", len(audio_bytes), mime, dbg_path)
        except Exception:
            pass

        # PyAV (faster-whisper's decoder) chokes on some browser-produced WebM
        # streams — MediaRecorder can emit fragmented chunks without a proper
        # container header on chunked timeslice output. Fall back to ffmpeg
        # subprocess conversion to raw PCM when direct decode fails.
        try:
            buf = io.BytesIO(audio_bytes)
            segments, _ = model.transcribe(
                buf,
                language="en",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.3},
                temperature=0,
                no_speech_threshold=0.4,  # was 0.6 - too aggressive
                condition_on_previous_text=False,
            )
        except Exception as e:
            log.warning("faster-whisper direct decode failed (%s); trying ffmpeg fallback", e.__class__.__name__)
            pcm = self._ffmpeg_to_pcm(audio_bytes, mime)
            if pcm is None:
                log.error("ffmpeg fallback also failed; giving up on this chunk")
                return ""
            segments, _ = model.transcribe(
                pcm,
                language="en",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.3},
                temperature=0,
                no_speech_threshold=0.4,
                condition_on_previous_text=False,
            )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info("STT transcript: %r", text)
        if text.lower() in _WHISPER_HALLUCINATIONS:
            return ""
        return text

    def _ffmpeg_to_pcm(self, audio_bytes: bytes, mime: str):
        """Decode arbitrary browser audio to float32 16 kHz mono PCM via
        ffmpeg subprocess. Returns numpy array or None on failure."""
        import subprocess
        import numpy as np
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", "pipe:0",
                    "-f", "f32le", "-ac", "1", "-ar", "16000",
                    "pipe:1",
                ],
                input=audio_bytes,
                capture_output=True,
                timeout=30,
            )
            if proc.returncode != 0:
                log.error("ffmpeg decode failed: %s", proc.stderr.decode("utf-8", errors="ignore")[:500])
                return None
            return np.frombuffer(proc.stdout, dtype=np.float32)
        except FileNotFoundError:
            log.error("ffmpeg not on PATH — install with: brew install ffmpeg")
            return None
        except Exception as e:
            log.error("ffmpeg subprocess error: %s", e)
            return None

    @staticmethod
    def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        return buf.getvalue()

    async def transcribe_stream(
        self,
        audio_chunks,
        sample_rate: int = 16000,
        encoding: str = "linear16",
    ):
        """Rolling-window streaming approximation for faster-whisper.

        Since faster-whisper is batch-only, we buffer chunks and re-transcribe
        every ~600ms of accumulated audio, emitting the growing text as
        STTEvent(kind='partial'). When the caller has been silent for ~700ms
        (Silero VAD), we run one last transcription on the full buffer, emit
        STTEvent(kind='final'), and reset the buffer.

        `audio_chunks` yields raw PCM16 mono at `sample_rate` (or µ-law if
        encoding='mulaw'; we convert to PCM16 internally).
        """
        from ..base import STTEvent
        import audioop as _audioop
        from packages.voice import build_vad

        vad = build_vad(kind="auto")

        buffer = bytearray()
        last_partial_at = 0.0
        last_voice_at = None
        partial_interval_ms = 600
        silence_hang_ms = 700
        import time as _time

        def _now_ms() -> float:
            return _time.time() * 1000

        def _to_pcm16(chunk: bytes) -> bytes:
            if encoding == "mulaw":
                return _audioop.ulaw2lin(chunk, 2)
            return chunk

        async for chunk in audio_chunks:
            if not chunk:
                continue
            pcm = _to_pcm16(chunk)
            buffer.extend(pcm)

            # Track voice activity to fire final
            is_speech = vad.is_speech(
                chunk, sample_rate=sample_rate,
                mime=("audio/mulaw" if encoding == "mulaw" else "audio/pcm"),
            )
            now = _now_ms()
            if is_speech:
                last_voice_at = now

            # Emit rolling partial every partial_interval_ms
            if (now - last_partial_at) >= partial_interval_ms and len(buffer) >= self._MIN_AUDIO_BYTES:
                wav = self._pcm_to_wav(bytes(buffer), sample_rate)
                text = await self.transcribe(wav, sample_rate=sample_rate, mime="audio/wav")
                if text:
                    yield STTEvent(kind="partial", text=text, is_final=False)
                last_partial_at = now

            # Fire final on silence-hang
            if last_voice_at is not None and (now - last_voice_at) >= silence_hang_ms and len(buffer) >= self._MIN_AUDIO_BYTES:
                wav = self._pcm_to_wav(bytes(buffer), sample_rate)
                text = await self.transcribe(wav, sample_rate=sample_rate, mime="audio/wav")
                if text:
                    yield STTEvent(kind="final", text=text, is_final=True)
                buffer.clear()
                last_voice_at = None
                last_partial_at = 0.0
