"""Per-speech-act default delivery profiles.

Used as the fall-open path for the two-planner LLM (Sprint 9e): if the
performance planner fails or returns invalid VPL, we ship the semantic
text with a deterministic delivery keyed by speech_act.

Numbers here are engineering priors — not tuned by A/B yet.  Weeks 4+
Voice DNA + experimental programme (moat doc §811) replaces this table
with per-tenant learned defaults.
"""
from __future__ import annotations

from .schema import (
    Delivery,
    DeliveryStyle,
    Interruptibility,
    PhraseFinality,
    PitchRange,
    SpeechAct,
)


_DEFAULTS: dict[SpeechAct, Delivery] = {
    SpeechAct.GREETING: Delivery(
        style=DeliveryStyle.WARM,
        intensity=0.4,
        rate=0.95,
        energy=0.5,
        pitch_semitones=0.5,
        pitch_range=PitchRange.MEDIUM,
        stability=0.55,
        identity_strength=0.85,
        phrase_finality=PhraseFinality.FINAL,
        interruptibility=Interruptibility.HIGH,
        pause_after_ms=200,
        # breaths="none" by default — SafetyPolicy.allow_nonverbal_vocalisation
        # is False by default too, so any "light" here would fail validation.
        # Warmth without breath sounds is safer for SMB defaults.
        breaths="none",
    ),
    SpeechAct.ACKNOWLEDGE: Delivery(
        style=DeliveryStyle.WARM,
        intensity=0.25,
        rate=1.05,
        energy=0.35,
        pitch_semitones=0.0,
        interruptibility=Interruptibility.HIGH,
        pause_after_ms=100,
    ),
    SpeechAct.ACKNOWLEDGE_THEN_TOOL: Delivery(
        style=DeliveryStyle.WARM,
        intensity=0.3,
        rate=1.0,
        energy=0.4,
        pitch_semitones=0.0,
        phrase_finality=PhraseFinality.CONTINUING,
        pause_after_ms=150,
    ),
    SpeechAct.SLOT_OFFER: Delivery(
        style=DeliveryStyle.NEUTRAL,
        intensity=0.2,
        rate=0.95,
        energy=0.4,
        pitch_range=PitchRange.MEDIUM,
        # Slot listings need a bit of "let me finish" — MEDIUM interrupt
        interruptibility=Interruptibility.MEDIUM,
        pause_after_ms=250,
    ),
    SpeechAct.CONFIRM: Delivery(
        style=DeliveryStyle.PROFESSIONAL,
        intensity=0.3,
        rate=0.95,
        energy=0.45,
        stability=0.65,
        pause_after_ms=200,
    ),
    SpeechAct.CLARIFY: Delivery(
        style=DeliveryStyle.NEUTRAL,
        intensity=0.35,
        rate=0.95,
        energy=0.4,
        pitch_semitones=1.0,          # slight rising for clarification question
        phrase_finality=PhraseFinality.CONTINUING,
        interruptibility=Interruptibility.HIGH,
    ),
    SpeechAct.DELIVER_BAD_NEWS: Delivery(
        style=DeliveryStyle.REASSURING,
        intensity=0.35,
        rate=0.9,
        energy=0.35,
        pitch_semitones=-0.5,
        stability=0.7,
        pause_before_ms=150,
        pause_after_ms=200,
    ),
    SpeechAct.APOLOGY: Delivery(
        style=DeliveryStyle.APOLOGETIC,
        intensity=0.35,
        rate=0.9,
        energy=0.3,
        pitch_semitones=-1.0,
        pause_after_ms=150,
    ),
    SpeechAct.HANDOFF: Delivery(
        style=DeliveryStyle.PROFESSIONAL,
        intensity=0.3,
        rate=1.0,
        energy=0.4,
        interruptibility=Interruptibility.MEDIUM,
        pause_after_ms=100,
    ),
    # Restricted acts — validator caps intensity, so these defaults
    # stay under cap.  Low interruptibility because we need the caller
    # to hear the whole thing.
    SpeechAct.EMERGENCY: Delivery(
        style=DeliveryStyle.URGENT,
        intensity=0.35,               # under 0.4 policy cap
        rate=1.1,
        energy=0.6,
        stability=0.75,
        interruptibility=Interruptibility.LOW,
        pause_after_ms=100,
    ),
    SpeechAct.PAYMENT: Delivery(
        style=DeliveryStyle.PROFESSIONAL,
        intensity=0.2,                # well under 0.3 cap
        rate=0.9,
        energy=0.35,
        stability=0.8,
        interruptibility=Interruptibility.LOW,
        pause_after_ms=150,
    ),
    SpeechAct.HEALTH: Delivery(
        style=DeliveryStyle.PROFESSIONAL,
        intensity=0.3,                # under 0.5 cap
        rate=0.95,
        energy=0.4,
        stability=0.7,
        interruptibility=Interruptibility.MEDIUM,
    ),
    SpeechAct.FAREWELL: Delivery(
        style=DeliveryStyle.WARM,
        intensity=0.35,
        rate=0.95,
        energy=0.45,
        pitch_semitones=0.5,
        pause_after_ms=250,
    ),
    SpeechAct.NEUTRAL: Delivery(),   # all Pydantic defaults
}


def default_delivery_for(act: SpeechAct) -> Delivery:
    """Return the default Delivery for a given speech act.

    Never returns None — falls back to NEUTRAL if the act isn't in
    the table (shouldn't happen; enum is closed)."""
    return _DEFAULTS.get(act, _DEFAULTS[SpeechAct.NEUTRAL]).model_copy()
