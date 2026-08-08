"""Sprint 9c: VPL schema + validator + defaults tests.

Coverage:
  * Schema rejects out-of-range values at construction (Pydantic).
  * Emphasis/pause offsets bounded to text length.
  * Validator raises on cross-field policy violation.
  * validate_vpl_and_repair clamps to safety without raising.
  * Speech-act policy enforced (no laughter in emergencies, etc).
  * default_delivery_for stays under policy caps for restricted acts.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.voice.vpl import (
    Delivery,
    DeliveryStyle,
    Emphasis,
    Pause,
    SafetyPolicy,
    SpeechAct,
    VPLUtterance,
    default_delivery_for,
    validate_vpl,
    VPLValidationError,
)
from packages.voice.vpl.validator import validate_vpl_and_repair


# ── schema construction bounds ──────────────────────────────────────

def test_rate_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Delivery(rate=2.5)  # ceiling is 1.4


def test_intensity_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Delivery(intensity=1.5)


def test_pitch_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Delivery(pitch_semitones=10.0)  # max +6


def test_pause_duration_bounded():
    with pytest.raises(ValidationError):
        Pause(after_character=5, duration_ms=5)   # min 50
    with pytest.raises(ValidationError):
        Pause(after_character=5, duration_ms=3000)  # max 2000


def test_emphasis_end_after_start():
    with pytest.raises(ValidationError):
        Emphasis(start=10, end=5)


def test_utterance_text_length_capped():
    with pytest.raises(ValidationError):
        VPLUtterance(text="x" * 700)  # 600 max


def test_utterance_empty_text_rejected():
    with pytest.raises(ValidationError):
        VPLUtterance(text="")


def test_emphasis_offset_within_text():
    """Emphasis end past text length must fail construction."""
    with pytest.raises(ValidationError):
        VPLUtterance(
            text="Hello",
            emphasis=[Emphasis(start=0, end=100)],
        )


def test_pause_offset_within_text():
    with pytest.raises(ValidationError):
        VPLUtterance(
            text="Hello",
            pauses=[Pause(after_character=50, duration_ms=100)],
        )


def test_locale_pattern_validated():
    """Locale must match `xx` or `xx-XX` (BCP-47 subset)."""
    VPLUtterance(text="hi", locale="en-US")
    VPLUtterance(text="hi", locale="fr")
    with pytest.raises(ValidationError):
        VPLUtterance(text="hi", locale="ENGLISH")


# ── cross-field validator ───────────────────────────────────────────

def test_validator_accepts_safe_utterance():
    u = VPLUtterance(
        text="How can I help you today?",
        speech_act=SpeechAct.GREETING,
        delivery=Delivery(intensity=0.4),
    )
    validate_vpl(u)  # no raise


def test_intensity_above_safety_ceiling_rejected():
    u = VPLUtterance(
        text="hi",
        delivery=Delivery(intensity=0.9),
        safety=SafetyPolicy(maximum_emotional_intensity=0.5),
    )
    with pytest.raises(VPLValidationError, match="safety.maximum"):
        validate_vpl(u)


def test_breaths_without_safety_permission_rejected():
    u = VPLUtterance(
        text="hi",
        delivery=Delivery(breaths="light"),
        safety=SafetyPolicy(allow_nonverbal_vocalisation=False),
    )
    with pytest.raises(VPLValidationError, match="allow_nonverbal"):
        validate_vpl(u)


# ── speech-act policy ───────────────────────────────────────────────

def test_emergency_forbids_high_intensity():
    """Emergency speech-act caps intensity at 0.4."""
    u = VPLUtterance(
        text="Please call 911 immediately.",
        speech_act=SpeechAct.EMERGENCY,
        delivery=Delivery(intensity=0.7),
    )
    with pytest.raises(VPLValidationError, match="emergency"):
        validate_vpl(u)


def test_emergency_forbids_nonverbal():
    u = VPLUtterance(
        text="Please stay on the line.",
        speech_act=SpeechAct.EMERGENCY,
        delivery=Delivery(intensity=0.3, breaths="light"),
        safety=SafetyPolicy(allow_nonverbal_vocalisation=True),
    )
    with pytest.raises(VPLValidationError, match="emergency"):
        validate_vpl(u)


def test_payment_intensity_cap_stricter_than_emergency():
    """Payment collection needs even calmer delivery than emergency."""
    u = VPLUtterance(
        text="Please read me your card number.",
        speech_act=SpeechAct.PAYMENT,
        delivery=Delivery(intensity=0.4),  # ok for emergency, not payment
    )
    with pytest.raises(VPLValidationError, match="payment"):
        validate_vpl(u)


def test_health_speech_act_capped_at_0_5():
    u = VPLUtterance(
        text="Are you experiencing chest pain right now?",
        speech_act=SpeechAct.HEALTH,
        delivery=Delivery(intensity=0.6),
    )
    with pytest.raises(VPLValidationError, match="health"):
        validate_vpl(u)


# ── unknown pronunciation refs ──────────────────────────────────────

def test_unknown_pronunciation_ref_rejected():
    u = VPLUtterance(
        text="See you at Osteria Verde tomorrow.",
        pronunciation_refs=["osteria_verde_v1", "unknown_ref"],
    )
    with pytest.raises(VPLValidationError, match="unknown"):
        validate_vpl(u, known_pronunciation_refs={"osteria_verde_v1"})


def test_pronunciation_refs_skipped_when_registry_missing():
    """No registry passed = skip the check (dev mode)."""
    u = VPLUtterance(
        text="See you at Osteria Verde tomorrow.",
        pronunciation_refs=["anything"],
    )
    validate_vpl(u)  # no raise


# ── repair mode ─────────────────────────────────────────────────────

def test_repair_clamps_intensity_to_safety():
    u = VPLUtterance(
        text="hi",
        delivery=Delivery(intensity=0.9),
        safety=SafetyPolicy(maximum_emotional_intensity=0.5),
    )
    repaired, repairs = validate_vpl_and_repair(u)
    assert repaired.delivery.intensity == 0.5
    assert any("intensity" in r for r in repairs)


def test_repair_clamps_intensity_for_emergency():
    u = VPLUtterance(
        text="Please stay on the line.",
        speech_act=SpeechAct.EMERGENCY,
        delivery=Delivery(intensity=0.8),
    )
    repaired, repairs = validate_vpl_and_repair(u)
    assert repaired.delivery.intensity <= 0.4
    assert any("emergency" in r or "intensity" in r for r in repairs)


def test_repair_disables_breaths_when_forbidden():
    u = VPLUtterance(
        text="Card number please.",
        speech_act=SpeechAct.PAYMENT,
        delivery=Delivery(intensity=0.2, breaths="light"),
        safety=SafetyPolicy(allow_nonverbal_vocalisation=True),
    )
    repaired, repairs = validate_vpl_and_repair(u)
    assert repaired.delivery.breaths == "none"
    assert repaired.safety.allow_nonverbal_vocalisation is False
    assert any("breaths" in r or "nonverbal" in r for r in repairs)


def test_repair_drops_unknown_pronunciation_refs():
    u = VPLUtterance(
        text="hi",
        pronunciation_refs=["known", "unknown"],
    )
    repaired, repairs = validate_vpl_and_repair(
        u, known_pronunciation_refs={"known"},
    )
    assert repaired.pronunciation_refs == ["known"]
    assert any("pronunciation" in r for r in repairs)


def test_repair_preserves_safe_input():
    u = VPLUtterance(
        text="How can I help you?",
        speech_act=SpeechAct.GREETING,
        delivery=Delivery(intensity=0.4),
    )
    repaired, repairs = validate_vpl_and_repair(u)
    assert repairs == [], "safe input needed no repairs"
    assert repaired == u


# ── defaults ────────────────────────────────────────────────────────

def test_default_delivery_covers_all_speech_acts():
    """Every enum member must have a default entry so the fail-open
    path can never crash on an unknown act."""
    for act in SpeechAct:
        d = default_delivery_for(act)
        assert isinstance(d, Delivery)


@pytest.mark.parametrize("act,cap", [
    (SpeechAct.EMERGENCY, 0.4),
    (SpeechAct.PAYMENT, 0.3),
    (SpeechAct.HEALTH, 0.5),
])
def test_default_delivery_respects_speech_act_cap(act: SpeechAct, cap: float):
    """The default table must never generate a Delivery that fails
    validation for the same speech_act it targets."""
    d = default_delivery_for(act)
    assert d.intensity <= cap, \
        f"default {act.value} intensity={d.intensity} exceeds cap {cap}"


def test_default_delivery_passes_validator():
    """Every (act, default_delivery(act)) pair must validate."""
    for act in SpeechAct:
        d = default_delivery_for(act)
        u = VPLUtterance(text="test", speech_act=act, delivery=d)
        validate_vpl(u)


def test_greeting_default_is_warm_and_slower():
    """Sanity: greeting shouldn't be neutral+fast."""
    d = default_delivery_for(SpeechAct.GREETING)
    assert d.style == DeliveryStyle.WARM
    assert d.rate < 1.0
    assert d.pause_after_ms > 0


def test_bad_news_default_is_reassuring():
    d = default_delivery_for(SpeechAct.DELIVER_BAD_NEWS)
    assert d.style == DeliveryStyle.REASSURING
    assert d.rate < 1.0
    assert d.pitch_semitones < 0
