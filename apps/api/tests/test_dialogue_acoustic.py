"""Sprint 10 Track D1 tests: Acoustic Interaction Features.

Coverage:
  * energy_from_mulaw: silent audio → low, loud audio → high
  * count_pauses: detects long silences in mixed audio
  * speech_rate_wpm: normal / too-short / no-transcript
  * repeated_phrase_count: n-gram overlap with prior turns
  * extract_features composes everything correctly
  * urgency_score / frustration_signal derived signals reasonable
  * asr_uncertain threshold
"""
from __future__ import annotations

import audioop

import pytest

from packages.dialogue import (
    AcousticTurnFeatures,
    count_pauses,
    energy_from_mulaw,
    extract_features,
    repeated_phrase_count,
    speech_rate_wpm,
)


def _silence_mulaw(duration_ms: int) -> bytes:
    """Generate `duration_ms` of µ-law silence (0xFF)."""
    frames = duration_ms // 20
    return b"\xff" * (frames * 160)  # 160 bytes/frame at 8kHz mulaw


def _loud_mulaw(duration_ms: int) -> bytes:
    """Generate `duration_ms` of loud µ-law tone (0x00 = max amplitude)."""
    frames = duration_ms // 20
    return b"\x00" * (frames * 160)


# ── energy ──────────────────────────────────────────────────────────

def test_silence_has_low_energy():
    avg, var = energy_from_mulaw(_silence_mulaw(200))
    # µ-law silence (0xFF) decodes to near-zero linear samples
    assert avg < 0.05


def test_loud_audio_has_high_energy():
    avg, var = energy_from_mulaw(_loud_mulaw(200))
    assert avg > 0.5


def test_empty_returns_zero():
    avg, var = energy_from_mulaw(b"")
    assert avg == 0.0
    assert var == 0.0


def test_variance_higher_for_mixed_amplitude():
    # 200ms silence + 200ms loud → high variance
    mixed = _silence_mulaw(200) + _loud_mulaw(200)
    _, var = energy_from_mulaw(mixed)
    _, quiet_var = energy_from_mulaw(_silence_mulaw(400))
    assert var > quiet_var


# ── pauses ──────────────────────────────────────────────────────────

def test_count_pauses_detects_long_silence():
    # 500ms loud + 400ms silence + 500ms loud = one pause of 400ms
    audio = _loud_mulaw(500) + _silence_mulaw(400) + _loud_mulaw(500)
    count, longest = count_pauses(audio)
    assert count == 1
    assert 380 <= longest <= 420   # allow small frame-boundary drift


def test_short_silences_not_counted_as_pauses():
    # 100ms silence < 200ms threshold — not a pause
    audio = _loud_mulaw(500) + _silence_mulaw(100) + _loud_mulaw(500)
    count, longest = count_pauses(audio)
    assert count == 0


def test_all_silence_is_one_long_pause():
    count, longest = count_pauses(_silence_mulaw(1000))
    assert count == 1
    assert 980 <= longest <= 1020


def test_no_silence_no_pauses():
    count, longest = count_pauses(_loud_mulaw(1000))
    assert count == 0
    assert longest == 0


# ── speech rate ────────────────────────────────────────────────────

def test_speech_rate_normal():
    # "hello there how are you doing" = 6 words in 2 seconds = 180 wpm
    rate = speech_rate_wpm("hello there how are you doing", 2000)
    assert rate is not None
    assert 175 < rate < 185


def test_speech_rate_none_for_short_duration():
    # < 500ms → None (single word doesn't have a rate)
    assert speech_rate_wpm("yes", 300) is None


def test_speech_rate_none_for_empty_transcript():
    assert speech_rate_wpm("", 2000) is None


# ── repeated phrases ──────────────────────────────────────────────

def test_repeated_ngram_counted():
    prior = ["I need to book a cleaning next Thursday"]
    current = "I need to book a cleaning at ten"
    # Overlapping 3-grams: "i need to", "need to book", "to book a", "book a cleaning"
    count = repeated_phrase_count(current, prior, n=3)
    assert count >= 3


def test_no_repetition_when_disjoint():
    prior = ["one two three"]
    current = "four five six seven"
    assert repeated_phrase_count(current, prior, n=3) == 0


def test_short_utterance_no_ngrams():
    """< n tokens can't form an n-gram — return 0."""
    prior = ["one two three four five"]
    current = "yes no"
    assert repeated_phrase_count(current, prior, n=3) == 0


def test_prior_turns_beyond_lookback_ignored():
    """Only last 3 turns count as prior."""
    prior = [
        "phrase one two",
        "phrase one two",
        "phrase one two",
        "phrase one two",   # 4th back — should still count (last 3 of these plus current)
        "unrelated content here",
        "different words entirely",
    ]
    # We look back last 3 which is "phrase one two", "unrelated content here",
    # "different words entirely" — one has the repeated 3-gram
    count = repeated_phrase_count("phrase one two", prior)
    assert count == 1


# ── extract_features composition ───────────────────────────────────

def test_extract_features_full():
    audio = _loud_mulaw(500) + _silence_mulaw(400) + _loud_mulaw(500)
    features = extract_features(
        mulaw=audio,
        transcript="I need to book an appointment next week",
        duration_ms=1400,
        prior_transcripts=["I need to book something now"],
        interruption_count=1,
        asr_confidence=0.88,
    )
    assert features.pause_count == 1
    assert features.longest_pause_ms >= 380
    assert features.speech_rate_wpm is not None
    assert features.average_energy is not None
    assert features.interruption_count == 1
    assert features.repeated_phrase_count >= 1
    assert features.asr_mean_confidence == 0.88


def test_extract_features_text_only():
    """Callers with no audio (browser text input) still get a valid
    result — just with acoustic fields None."""
    features = extract_features(
        transcript="hello", duration_ms=1000, prior_transcripts=[],
    )
    assert features.average_energy is None
    assert features.pause_count == 0
    assert features.asr_mean_confidence is None


def test_extract_features_all_defaults():
    features = extract_features()
    assert features.speech_rate_wpm is None
    assert features.pause_count == 0
    assert features.repeated_phrase_count == 0


# ── derived signals ────────────────────────────────────────────────

def test_urgency_score_high_when_fast_and_loud():
    f = AcousticTurnFeatures(
        speech_rate_wpm=210, average_energy=0.7, interruption_count=2,
    )
    assert f.urgency_score() > 0.6


def test_urgency_score_low_when_normal():
    f = AcousticTurnFeatures(
        speech_rate_wpm=140, average_energy=0.3, interruption_count=0,
    )
    assert f.urgency_score() < 0.3


def test_urgency_score_zero_with_no_data():
    f = AcousticTurnFeatures()
    assert f.urgency_score() == 0.0


def test_frustration_signal_high_when_repeats_and_interrupts():
    f = AcousticTurnFeatures(
        interruption_count=3, repeated_phrase_count=3, energy_variance=0.4,
    )
    assert f.frustration_signal() > 0.6


def test_asr_uncertain_true_below_threshold():
    assert AcousticTurnFeatures(asr_mean_confidence=0.5).asr_uncertain() is True
    assert AcousticTurnFeatures(asr_mean_confidence=0.9).asr_uncertain() is False


def test_asr_uncertain_false_when_no_confidence_reported():
    """Absence of confidence != low confidence — return False so the
    agent doesn't over-verify batch-STT inputs that don't expose it."""
    assert AcousticTurnFeatures(asr_mean_confidence=None).asr_uncertain() is False
