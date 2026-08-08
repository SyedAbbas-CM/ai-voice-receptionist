"""VPL v0 schema — Pydantic models matching moat doc §VPL lines 549-604.

Design principles:

  * All numeric knobs are bounded ranges, not free floats.  A caller
    that tries `intensity=99` gets rejected at construction, not at
    the provider.
  * `speech_act` is a closed enum, not a string.  The validator uses it
    to enforce policy (no laughter during emergencies, no exaggerated
    style during payment).
  * `safety` is a per-utterance policy envelope that overrides tenant
    defaults.  Compilers must respect it.
  * The schema is versioned so we can grow without breaking clients.

Providers see a `CompiledSpeechPlan`, not the raw VPL.  That plan
carries both the wire payload AND a degradation report (which VPL
fields the target provider could not honour).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


VPL_VERSION = "1.0"


# ── enums ────────────────────────────────────────────────────────────

class SpeechAct(str, Enum):
    """What kind of utterance this is.  Drives policy + default
    delivery + reference-bank retrieval.

    Kept intentionally short.  New acts added only when a policy
    distinction actually matters — resist inflation."""
    GREETING = "greeting"
    ACKNOWLEDGE = "acknowledge"
    ACKNOWLEDGE_THEN_TOOL = "acknowledge_then_tool_transition"
    SLOT_OFFER = "slot_offer"                # "I have 9am, 10:30, or 2pm"
    CONFIRM = "confirm"                       # "Booked for 3pm tomorrow"
    CLARIFY = "clarify"                       # "Sorry, did you say Tuesday?"
    DELIVER_BAD_NEWS = "deliver_bad_news"     # "We don't have that available"
    APOLOGY = "apology"
    HANDOFF = "handoff"                       # "Let me get a teammate"
    EMERGENCY = "emergency"                   # medical / safety escalation
    PAYMENT = "payment"                       # card/PII collection
    HEALTH = "health"                         # symptoms / medications
    FAREWELL = "farewell"
    NEUTRAL = "neutral"                       # default when unclear


class DeliveryStyle(str, Enum):
    NEUTRAL = "neutral"
    WARM = "warm"
    REASSURING = "reassuring"
    URGENT = "urgent"
    APOLOGETIC = "apologetic"
    PROFESSIONAL = "professional"


class PhraseFinality(str, Enum):
    """Whether this phrase ends the turn (falling contour) or continues
    (rising / level).  Compilers may hint providers via punctuation."""
    CONTINUING = "continuing"
    FINAL = "final"


class Interruptibility(str, Enum):
    """How much of this phrase the caller is allowed to interrupt.
    LOW = important disclaimer / consent, don't yield mid-sentence."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PitchRange(str, Enum):
    NARROW = "narrow"
    MEDIUM = "medium"
    WIDE = "wide"


# ── nested models ────────────────────────────────────────────────────

class Emphasis(BaseModel):
    """A byte-offset span inside the utterance text that should be
    emphasized.  Compilers translate to provider markup or phrase
    chunking."""
    start: int = Field(..., ge=0, description="Byte offset into text")
    end: int = Field(..., ge=0)
    strength: float = Field(0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _end_after_start(self) -> "Emphasis":
        if self.end <= self.start:
            raise ValueError("emphasis end must be > start")
        return self


class Pause(BaseModel):
    """Insert a pause after the character at `after_character` (byte
    offset in text)."""
    after_character: int = Field(..., ge=0)
    duration_ms: int = Field(..., ge=50, le=2000)


class Delivery(BaseModel):
    """How the text should be spoken.  Every field bounded."""
    style: DeliveryStyle = DeliveryStyle.NEUTRAL
    intensity: float = Field(0.3, ge=0.0, le=1.0,
                             description="Emotional intensity 0..1")
    rate: float = Field(1.0, ge=0.6, le=1.4,
                        description="Speaking rate multiplier")
    energy: float = Field(0.4, ge=0.0, le=1.0)
    pitch_semitones: float = Field(0.0, ge=-6.0, le=6.0)
    pitch_range: PitchRange = PitchRange.MEDIUM
    # ElevenLabs-style stability / identity strength — bounded so a
    # runaway LLM can't destabilise the voice.
    stability: float = Field(0.5, ge=0.0, le=1.0)
    identity_strength: float = Field(0.75, ge=0.0, le=1.0)
    phrase_finality: PhraseFinality = PhraseFinality.FINAL
    interruptibility: Interruptibility = Interruptibility.HIGH
    pause_before_ms: int = Field(0, ge=0, le=1500)
    pause_after_ms: int = Field(0, ge=0, le=1500)
    breaths: str = Field(
        "none",
        pattern="^(none|light|natural)$",
        description="Non-verbal breath insertion policy",
    )


class SafetyPolicy(BaseModel):
    """Per-utterance safety envelope.  Compilers MUST enforce these
    (they cap tenant defaults, they don't relax them)."""
    allow_nonverbal_vocalisation: bool = False
    """Laughter, sighing, whispering.  Off by default."""
    maximum_emotional_intensity: float = Field(0.7, ge=0.0, le=1.0)
    """Hard ceiling on `delivery.intensity`.  Emergencies clamp to
    ≤0.4 in policy; payments to ≤0.3."""


class VPLContext(BaseModel):
    """Optional conversational context providers can use for prosody
    continuity.  ElevenLabs previous_text/next_text lives here."""
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    prior_spoken_text: Optional[str] = None
    """The last thing the agent actually said (heard-text, not
    generated-text).  Ledger-sourced."""
    next_intent: Optional[str] = None


# ── top-level utterance ──────────────────────────────────────────────

class VPLUtterance(BaseModel):
    """One thing the agent is about to say, fully specified.

    Compilers translate this to provider payloads via
    packages.voice.vpl.compilers.  The `validate_vpl` function enforces
    cross-field rules (e.g. laughter forbidden in emergencies) that
    Pydantic can't check field-by-field."""
    version: str = VPL_VERSION
    utterance_id: Optional[str] = None
    locale: str = Field("en-US", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    text: str = Field(..., min_length=1, max_length=600)
    speech_act: SpeechAct = SpeechAct.NEUTRAL

    delivery: Delivery = Field(default_factory=Delivery)
    emphasis: list[Emphasis] = Field(default_factory=list)
    pauses: list[Pause] = Field(default_factory=list)
    pronunciation_refs: list[str] = Field(default_factory=list)
    context: VPLContext = Field(default_factory=VPLContext)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)

    @model_validator(mode="after")
    def _emphasis_within_text(self) -> "VPLUtterance":
        n = len(self.text)
        for e in self.emphasis:
            if e.end > n:
                raise ValueError(
                    f"emphasis end={e.end} exceeds text length {n}",
                )
        for p in self.pauses:
            if p.after_character > n:
                raise ValueError(
                    f"pause after_character={p.after_character} exceeds text length {n}",
                )
        return self


# ── compiled output ─────────────────────────────────────────────────

class CompiledSpeechPlan(BaseModel):
    """What a compiler returns: the wire payload plus what got lost in
    translation.

    `unsupported_fields` — VPL fields this provider cannot express AT
    ALL.  E.g. Qwen doesn't have a stability knob; that ends up here.

    `approximations` — fields where we did the best we could but the
    result won't be an exact match.  E.g. ElevenLabs Flash ignores
    inline emphasis tags, so we split the text into phrases and hope
    the model finds the right emphasis; that's an approximation.

    `references` — voice reference asset IDs used for this synthesis
    (Voice DNA reference-bank, Sprint 10+).  Empty for now.

    All frozen — compilers construct once, everyone else reads."""
    provider: str
    model: str
    request_payload: dict[str, Any]
    output_format: str
    references: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()
    approximations: tuple[str, ...] = ()
    compiler_version: str = "0.1.0"

    model_config = {"frozen": True, "arbitrary_types_allowed": True}
