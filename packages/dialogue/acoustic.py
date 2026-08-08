"""Acoustic Interaction Features (Sprint 10 Track D1).

Cheap per-turn features derived from inbound audio + STT metadata.
NOT emotion classification — the audit warned against overclaiming
mood detection from text.  These are INTERACTION signals: how the
caller is speaking, not what they're feeling.

Features:
  * speech_rate_wpm — words per minute of the caller's turn
  * average_energy — mean absolute amplitude (0..1 normalized)
  * energy_variance — how much amplitude fluctuates
  * pause_count — number of silences > 200ms during the turn
  * longest_pause_ms — longest silence during the turn
  * interruption_count — how many times the caller cut in during the
    agent's prior speech (from ledger)
  * repeated_phrase_count — how many n-grams the caller repeated
    from their own recent turns (a "did you get that?" signal)
  * asr_mean_confidence — average per-word confidence (if the STT
    provider supplies it; None otherwise)
  * background_voice_probability — heuristic 0..1 estimate

Downstream uses:
  * Turn Manager: high energy + repeated phrase = caller frustrated →
    consider proactive escalation
  * Performance Planner: high speech_rate → agent should be concise,
    faster rate; low ASR confidence on names → agent should verify

Kept in packages/dialogue/ because Track A/B state consumes it —
not in packages/voice/ which is the outbound side.
"""
from __future__ import annotations

import audioop
import math
from dataclasses import dataclass, field
from typing import Optional


TWILIO_SAMPLE_RATE = 8000
TWILIO_FRAME_MS = 20
_FRAME_BYTES = int(TWILIO_SAMPLE_RATE * (TWILIO_FRAME_MS / 1000))
_SILENCE_RMS_THRESHOLD = 300
"""µ-law linear16 RMS below this counts as silence.  Empirical for
phone-bandwidth audio; higher would classify quiet speech as silence."""
_PAUSE_MIN_FRAMES = 10   # 200ms at 20ms/frame


@dataclass(frozen=True)
class AcousticTurnFeatures:
    """One turn's worth of interaction features.  All fields optional
    so a subset of feature sources (e.g. no STT confidence) still
    produces a usable object."""
    speech_rate_wpm: Optional[float] = None
    average_energy: Optional[float] = None
    energy_variance: Optional[float] = None
    pause_count: int = 0
    longest_pause_ms: int = 0
    interruption_count: int = 0
    repeated_phrase_count: int = 0
    asr_mean_confidence: Optional[float] = None
    background_voice_probability: Optional[float] = None

    # ── derived signals (interpretation, not raw measurement) ───────

    def urgency_score(self) -> float:
        """Heuristic 0..1: how urgent-sounding is this caller?

        Combines high speech rate + interruption count + high energy.
        NOT a psychological claim; used as a *hint* to the performance
        planner to consider shorter, more direct replies."""
        components: list[float] = []
        if self.speech_rate_wpm is not None:
            # Normal speech: ~150 wpm.  Fast: >180.
            r = self.speech_rate_wpm
            components.append(min(1.0, max(0.0, (r - 150) / 60)))
        if self.average_energy is not None:
            components.append(min(1.0, self.average_energy * 1.5))
        components.append(min(1.0, self.interruption_count / 3))
        return sum(components) / len(components) if components else 0.0

    def frustration_signal(self) -> float:
        """Heuristic 0..1: interruptions + repeated phrases + high
        energy variance suggest the caller is repeating themselves
        or getting agitated.  Same caveats as urgency_score."""
        components: list[float] = [
            min(1.0, self.interruption_count / 3),
            min(1.0, self.repeated_phrase_count / 4),
        ]
        if self.energy_variance is not None:
            components.append(min(1.0, self.energy_variance * 2))
        return sum(components) / len(components)

    def asr_uncertain(self) -> bool:
        """True when ASR confidence is low enough that the agent
        should VERIFY names/numbers instead of trusting them."""
        if self.asr_mean_confidence is None:
            return False
        return self.asr_mean_confidence < 0.75


# ── feature extractors ──────────────────────────────────────────────

def energy_from_mulaw(mulaw: bytes) -> tuple[float, float]:
    """Return (average_energy, energy_variance) for a µ-law audio
    buffer.  Normalizes to 0..1 by dividing RMS by int16 max.

    Uses audioop.rms on linear16 conversion.  Cheap.  Returns
    (0.0, 0.0) for empty input."""
    if not mulaw:
        return 0.0, 0.0
    try:
        linear = audioop.ulaw2lin(mulaw, 2)
    except Exception:
        return 0.0, 0.0
    if not linear:
        return 0.0, 0.0

    frame_bytes_lin = _FRAME_BYTES * 2   # int16 = 2 bytes/sample
    rms_values: list[float] = []
    for i in range(0, len(linear), frame_bytes_lin):
        frame = linear[i:i + frame_bytes_lin]
        if len(frame) < 2:
            continue
        try:
            rms = audioop.rms(frame, 2)
        except audioop.error:
            continue
        rms_values.append(rms / 32768.0)   # normalize by int16 max

    if not rms_values:
        return 0.0, 0.0
    avg = sum(rms_values) / len(rms_values)
    variance = sum((r - avg) ** 2 for r in rms_values) / len(rms_values)
    return avg, math.sqrt(variance)   # std-dev, semantically clearer


def count_pauses(mulaw: bytes) -> tuple[int, int]:
    """Count pauses (silences > 200ms) and the longest.  Returns
    (count, longest_ms).  Silence = RMS below _SILENCE_RMS_THRESHOLD."""
    if not mulaw:
        return 0, 0
    try:
        linear = audioop.ulaw2lin(mulaw, 2)
    except Exception:
        return 0, 0

    frame_bytes_lin = _FRAME_BYTES * 2
    silent_frames_in_row = 0
    pause_count = 0
    longest_ms = 0
    in_pause = False
    for i in range(0, len(linear), frame_bytes_lin):
        frame = linear[i:i + frame_bytes_lin]
        if len(frame) < 2:
            continue
        try:
            rms = audioop.rms(frame, 2)
        except audioop.error:
            rms = 0
        if rms < _SILENCE_RMS_THRESHOLD:
            silent_frames_in_row += 1
            if silent_frames_in_row == _PAUSE_MIN_FRAMES:
                in_pause = True
                pause_count += 1
            if in_pause:
                current_pause_ms = silent_frames_in_row * TWILIO_FRAME_MS
                if current_pause_ms > longest_ms:
                    longest_ms = current_pause_ms
        else:
            silent_frames_in_row = 0
            in_pause = False
    return pause_count, longest_ms


def speech_rate_wpm(transcript: str, duration_ms: float) -> Optional[float]:
    """Rough words-per-minute.  Returns None if duration is too short
    to be meaningful (< 500ms) — a single word doesn't have a rate."""
    if duration_ms < 500 or not transcript.strip():
        return None
    word_count = len(transcript.split())
    minutes = duration_ms / 60000.0
    if minutes <= 0:
        return None
    return word_count / minutes


def repeated_phrase_count(current: str, prior_turns: list[str], n: int = 3) -> int:
    """Count how many n-grams the caller repeated from their prior
    turns.  Signal that they're rephrasing to be understood."""
    if not current.strip() or not prior_turns:
        return 0
    current_tokens = current.lower().split()
    if len(current_tokens) < n:
        return 0
    current_ngrams = {
        " ".join(current_tokens[i:i + n])
        for i in range(len(current_tokens) - n + 1)
    }
    prior_ngrams: set[str] = set()
    for turn in prior_turns[-3:]:   # look back 3 turns max
        toks = turn.lower().split()
        if len(toks) < n:
            continue
        prior_ngrams.update(
            " ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)
        )
    return len(current_ngrams & prior_ngrams)


# ── the extractor ───────────────────────────────────────────────────

def extract_features(
    *,
    mulaw: bytes = b"",
    transcript: str = "",
    duration_ms: float = 0.0,
    prior_transcripts: Optional[list[str]] = None,
    interruption_count: int = 0,
    asr_confidence: Optional[float] = None,
    background_voice_probability: Optional[float] = None,
) -> AcousticTurnFeatures:
    """Build an AcousticTurnFeatures from raw signals.

    All args optional — pass only what you have.  A turn from browser
    text input has no `mulaw`; a turn from a batch STT has no
    per-word confidence.  The result carries None for what wasn't
    measured, and downstream code uses `is None` checks.

    Kept as a plain function (not a class) so it can be called from
    the actor without capturing state — pure computation."""
    avg_energy = variance = None
    pause_count = 0
    longest_pause_ms = 0
    if mulaw:
        avg_energy, variance = energy_from_mulaw(mulaw)
        pause_count, longest_pause_ms = count_pauses(mulaw)

    wpm = speech_rate_wpm(transcript, duration_ms) if transcript else None

    rep = 0
    if transcript and prior_transcripts:
        rep = repeated_phrase_count(transcript, prior_transcripts)

    return AcousticTurnFeatures(
        speech_rate_wpm=wpm,
        average_energy=avg_energy,
        energy_variance=variance,
        pause_count=pause_count,
        longest_pause_ms=longest_pause_ms,
        interruption_count=interruption_count,
        repeated_phrase_count=rep,
        asr_mean_confidence=asr_confidence,
        background_voice_probability=background_voice_probability,
    )
