"""Voice Performance Language (VPL) — Sprint 9c/9d.

VPL separates *what* the agent says (semantic content) from *how* it
says it (delivery — pace, pauses, emphasis, style, pronunciation).  A
single VPLUtterance object is provider-agnostic; per-provider
compilers translate it to ElevenLabs / Cartesia / Qwen payloads and
report any fields the target couldn't honour.

Public API:

    from packages.voice.vpl import (
        VPLUtterance,          # the schema
        SpeechAct,             # enum for planner/policy hooks
        DeliveryStyle,         # enum for style tags
        SafetyPolicy,          # per-tenant safety rules
        CompiledSpeechPlan,    # compiler output + degradation report
        validate_vpl,          # deterministic validator (raises on hard fails)
        default_delivery_for,  # speech-act -> Delivery fallback
    )

The schema follows §Voice DNA + VPL in
`docs/rnd-2026-08/37-voiceops-moat-blueprint.md` lines 549-604.
"""
from .schema import (
    VPLUtterance,
    Delivery,
    Emphasis,
    Pause,
    SpeechAct,
    DeliveryStyle,
    PhraseFinality,
    Interruptibility,
    PitchRange,
    SafetyPolicy,
    CompiledSpeechPlan,
)
from .validator import validate_vpl, VPLValidationError
from .defaults import default_delivery_for

__all__ = [
    "VPLUtterance",
    "Delivery",
    "Emphasis",
    "Pause",
    "SpeechAct",
    "DeliveryStyle",
    "PhraseFinality",
    "Interruptibility",
    "PitchRange",
    "SafetyPolicy",
    "CompiledSpeechPlan",
    "validate_vpl",
    "VPLValidationError",
    "default_delivery_for",
]
