"""VPL validator — enforces cross-field policy Pydantic can't express.

Pydantic handles per-field ranges (rate ∈ [0.6, 1.4]) at construction.
This module handles:

  * Safety envelope: intensity/breaths must obey `safety.*`
  * Speech-act policy: no laughter in emergencies, no exaggeration
    during payment/health/PII collection
  * Pronunciation entries: refs must resolve in the active version
  * Non-verbal tags: `breaths != none` requires
    `safety.allow_nonverbal_vocalisation=True`

Two entry points:

  * `validate_vpl(u)` — raises VPLValidationError on hard failure.
    Called before compilation.  Use for guaranteed-safe inputs.

  * `validate_vpl_and_repair(u)` — returns a *repaired* copy that
    clamps values into safety.  For LLM output, where you'd rather
    speak a slightly-toned-down version than error out mid-call.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Callable, Optional

from .schema import (
    Delivery,
    SafetyPolicy,
    SpeechAct,
    VPLUtterance,
)


class VPLValidationError(ValueError):
    """Raised when a VPL utterance violates policy that cannot be
    silently repaired."""


# ── speech-act policy table ──────────────────────────────────────────
#
# Per-speech-act hard rules.  Each rule returns an error message if
# violated, or None if ok.  Rules that CAN be repaired (clamp values)
# live in the repair map below.

def _forbid_nonverbal(u: VPLUtterance) -> Optional[str]:
    if u.delivery.breaths != "none":
        return f"breaths={u.delivery.breaths!r} forbidden for speech_act={u.speech_act.value}"
    if u.safety.allow_nonverbal_vocalisation:
        return f"non-verbal vocalisation forbidden for speech_act={u.speech_act.value}"
    return None


def _forbid_high_intensity(threshold: float) -> Callable[[VPLUtterance], Optional[str]]:
    def _rule(u: VPLUtterance) -> Optional[str]:
        if u.delivery.intensity > threshold:
            return (
                f"delivery.intensity={u.delivery.intensity} exceeds "
                f"policy cap {threshold} for speech_act={u.speech_act.value}"
            )
        return None
    return _rule


# Per-speech-act rules that fail HARD (validator raises).
_HARD_RULES: dict[SpeechAct, list[Callable[[VPLUtterance], Optional[str]]]] = {
    SpeechAct.EMERGENCY: [
        _forbid_nonverbal,
        _forbid_high_intensity(0.4),
    ],
    SpeechAct.PAYMENT: [
        _forbid_nonverbal,
        _forbid_high_intensity(0.3),
    ],
    SpeechAct.HEALTH: [
        _forbid_nonverbal,
        _forbid_high_intensity(0.5),
    ],
}


# ── main entry points ───────────────────────────────────────────────

def validate_vpl(
    u: VPLUtterance,
    *,
    known_pronunciation_refs: Optional[set[str]] = None,
) -> None:
    """Raise VPLValidationError if the utterance violates policy.

    Does NOT mutate.  Callers with LLM-generated VPL should use
    `validate_vpl_and_repair` instead so soft violations are clamped
    rather than aborting a live call."""
    # Safety envelope: intensity must not exceed per-utterance ceiling
    if u.delivery.intensity > u.safety.maximum_emotional_intensity:
        raise VPLValidationError(
            f"delivery.intensity={u.delivery.intensity} exceeds "
            f"safety.maximum_emotional_intensity={u.safety.maximum_emotional_intensity}"
        )

    # Non-verbal vocalisation gated by safety policy
    if u.delivery.breaths != "none" and not u.safety.allow_nonverbal_vocalisation:
        raise VPLValidationError(
            f"breaths={u.delivery.breaths!r} requires "
            f"safety.allow_nonverbal_vocalisation=True"
        )

    # Speech-act-specific hard rules
    for rule in _HARD_RULES.get(u.speech_act, []):
        err = rule(u)
        if err is not None:
            raise VPLValidationError(f"[{u.speech_act.value}] {err}")

    # Pronunciation entries must resolve if a set is provided.  When
    # `known_pronunciation_refs` is None (dev mode), we skip the check.
    if known_pronunciation_refs is not None:
        unknown = [r for r in u.pronunciation_refs
                   if r not in known_pronunciation_refs]
        if unknown:
            raise VPLValidationError(
                f"unknown pronunciation_refs: {unknown}"
            )


def validate_vpl_and_repair(
    u: VPLUtterance,
    *,
    known_pronunciation_refs: Optional[set[str]] = None,
) -> tuple[VPLUtterance, list[str]]:
    """Best-effort repair.  Returns (repaired_utterance, applied_repairs).

    Repairs applied silently:
      * intensity clamped to safety.maximum_emotional_intensity
      * breaths reset to "none" when safety forbids
      * intensity clamped to speech-act cap
      * unknown pronunciation_refs dropped

    Things that still raise:
      * text empty (schema-level)
      * emphasis / pause offsets out of range (schema-level)
      * — everything else the schema rejects at construction

    Call this on LLM-generated VPL where you'd rather speak a repaired
    version than fail the turn."""
    repairs: list[str] = []
    d = u.model_dump()

    # 1. Clamp intensity to safety ceiling
    if d["delivery"]["intensity"] > d["safety"]["maximum_emotional_intensity"]:
        old = d["delivery"]["intensity"]
        d["delivery"]["intensity"] = d["safety"]["maximum_emotional_intensity"]
        repairs.append(
            f"intensity {old} -> {d['delivery']['intensity']} (safety ceiling)"
        )

    # 2. Force breaths=none when safety forbids
    if d["delivery"]["breaths"] != "none" and not d["safety"]["allow_nonverbal_vocalisation"]:
        d["delivery"]["breaths"] = "none"
        repairs.append("breaths reset to 'none' (safety forbids)")

    # 3. Clamp intensity per speech-act policy
    act = d["speech_act"]
    caps = {
        SpeechAct.EMERGENCY.value: 0.4,
        SpeechAct.PAYMENT.value: 0.3,
        SpeechAct.HEALTH.value: 0.5,
    }
    if act in caps and d["delivery"]["intensity"] > caps[act]:
        old = d["delivery"]["intensity"]
        d["delivery"]["intensity"] = caps[act]
        repairs.append(
            f"intensity {old} -> {d['delivery']['intensity']} ({act} cap)"
        )

    # 4. Also disable non-verbal for restricted acts
    if act in {SpeechAct.EMERGENCY.value, SpeechAct.PAYMENT.value, SpeechAct.HEALTH.value}:
        if d["delivery"]["breaths"] != "none":
            d["delivery"]["breaths"] = "none"
            repairs.append(f"breaths reset to 'none' ({act} policy)")
        if d["safety"]["allow_nonverbal_vocalisation"]:
            d["safety"]["allow_nonverbal_vocalisation"] = False
            repairs.append(f"allow_nonverbal_vocalisation disabled ({act} policy)")

    # 5. Drop unknown pronunciation refs
    if known_pronunciation_refs is not None:
        kept = [r for r in d["pronunciation_refs"]
                if r in known_pronunciation_refs]
        dropped = [r for r in d["pronunciation_refs"]
                   if r not in known_pronunciation_refs]
        if dropped:
            d["pronunciation_refs"] = kept
            repairs.append(f"dropped unknown pronunciation_refs: {dropped}")

    repaired = VPLUtterance.model_validate(d)
    # Final hard validation — repair shouldn't leave anything invalid
    # but re-check as belt+braces.
    validate_vpl(repaired, known_pronunciation_refs=known_pronunciation_refs)
    return repaired, repairs
