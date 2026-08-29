"""Structured pydantic events for humanness/behavior traceability.

2026-08-29 (LiveKit steal #8 + debugging infra): the previous story was
"log a string, hope grep finds it."  This module gives every humanness-
relevant runtime moment a typed pydantic event with stable field names
so downstream tools (networking's incident.py, evals harness, dashboard)
can consume + filter without regex.

Events are written to the durable call_event_log with kind = the class's
`event_kind` string.  The `payload` dict is the .model_dump() of the
event.  Reading them back is one call:

    events = event_log.query(call_id, kind="empty_llm_completion")
    for e in events:
        parsed = EmptyLlmCompletionEvent.model_validate(e.payload)
        # typed access to all fields

Every event carries:
- call_id, tenant_id, session_id — routing
- turn_generation — matches the twilio_actor's turn counter for
  correlation across STT/brain/TTS
- ts_ms — monotonic ms since call start (not wall-clock — replayable)
- event_kind — literal string identifying the event class

Never raises.  Malformed input → validation error caught at emit time
and swallowed with a warning log line — humanness events must never
crash the call path.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class _BaseHumannessEvent(BaseModel):
    """Common fields every humanness event carries."""
    call_id: str
    tenant_id: str
    session_id: str
    turn_generation: int = 0
    ts_ms: int = 0    # monotonic ms since call start
    event_kind: str   # subclasses override the default

    model_config = {"frozen": True, "extra": "forbid"}


# ── LLM-lifecycle events ──────────────────────────────────────


class EmptyLlmCompletionEvent(_BaseHumannessEvent):
    """LLM returned chars=0 tools=0.  Christiaan-class failure mode.

    Emitted when the empty-completion watchdog (brain.py BUG-CHR-01
    fix) first detects the failure, BEFORE the rescue retry.
    """
    event_kind: Literal["empty_llm_completion"] = "empty_llm_completion"
    user_text: str = ""
    site: str = ""    # "brain.reply" / "brain.rescue_empty" / etc
    provider: str = ""
    model: str = ""


class EmptyLlmRescueEvent(_BaseHumannessEvent):
    """Empty-completion watchdog fired the rescue retry.  Outcome
    tells us whether the retry recovered."""
    event_kind: Literal["empty_llm_rescue"] = "empty_llm_rescue"
    user_text: str = ""
    recovered_text: bool = False
    recovered_tools: bool = False
    rescue_prompt_chars: int = 0


class EmptyLlmDeterministicFallbackEvent(_BaseHumannessEvent):
    """Both original + rescue returned empty → deterministic canned
    fallback speaks.  This is the last-resort branch — every fire is
    a real trouble signal ops should alert on."""
    event_kind: Literal["empty_llm_deterministic_fallback"] = (
        "empty_llm_deterministic_fallback"
    )
    user_text: str = ""
    fallback_text: str = ""


# ── policy / ack events ──────────────────────────────────────


class PolicyDecisionEvent(_BaseHumannessEvent):
    """NextActionPolicy chose an action + ack + delivery intent for
    this turn.  Consumed by evals harness + humanness scoring."""
    event_kind: Literal["policy_decision"] = "policy_decision"
    action: str                    # ConversationAction.value
    acknowledgment: Optional[str] = None    # AcknowledgmentKind.value
    delivery_intent: str = "standard"       # DeliveryIntent.value
    max_tokens: Optional[int] = None
    requested_slot: Optional[str] = None
    tool_hint: Optional[str] = None
    must_include_facts_count: int = 0


class TurnSignalReducedEvent(_BaseHumannessEvent):
    """TurnSignalReducer read the caller utterance + derived signals.

    Reasons list surfaces WHY each signal fired — grep for
    'reasons contains empathy' etc. from a call log to bisect ACK
    failures."""
    event_kind: Literal["turn_signal_reduced"] = "turn_signal_reduced"
    last_caller_text: str = ""
    caller_shared_hardship: bool = False
    caller_corrected_us: bool = False
    caller_is_dictating: bool = False
    caller_asked_to_wait: bool = False
    reasons: list[str] = Field(default_factory=list)


# ── service resolution (BUG-CHR-03) ─────────────────────────


class ServiceResolutionEvent(_BaseHumannessEvent):
    """resolve_service canonicalized a caller-spoken service phrase.

    Every book_appointment / book_viewing / take_message call that
    receives a service argument emits this so we can bisect 'which
    caller phrase mapped to which tenant service' in prod."""
    event_kind: Literal["service_resolution"] = "service_resolution"
    spoken: str
    kind: str    # ServiceMatchKind.value: match_exact / match_fuzzy / ambiguous / unknown
    canonical_name: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""


# ── barge-in / interruption events (LiveKit steal #5) ───


class BargeInDetectedEvent(_BaseHumannessEvent):
    """VAD tripped mid-TTS.  Kind distinguishes real interruption
    from false-positive (backchannel / cough / min-words-not-met)."""
    event_kind: Literal["barge_in_detected"] = "barge_in_detected"
    kind: Literal["real", "false_positive", "backchannel", "min_words_not_met"] = "real"
    speech_duration_ms: int = 0
    word_count: int = 0
    min_words_required: int = 2
    min_duration_ms_required: int = 500


class SpeechGateDroppedEvent(_BaseHumannessEvent):
    """SpeechCommitGate blocked a queued sentence.  Category tells us
    which safety triggered (safe / wait_promise / action_confirmation)."""
    event_kind: Literal["speech_gate_dropped"] = "speech_gate_dropped"
    category: str = "safe"   # safe / wait_promise / action_confirmation
    sentence_preview: str = ""


# ── transfer events (task #139) ─────────────────────────────


class TransferAttemptEvent(_BaseHumannessEvent):
    """TransferCoordinator initiated a transfer.  Outcome tells us
    whether it bridged / took a message / failed."""
    event_kind: Literal["transfer_attempt"] = "transfer_attempt"
    mode: str                  # TransferMode.value
    destination_id: Optional[str] = None
    destination_label: Optional[str] = None
    reason: str = ""
    outcome: str = "in_progress"  # TransferOutcome.value
    failure_detail: Optional[str] = None
    fallback_message_id: Optional[str] = None


# ── tool result truthfulness (H-P1.8) ────────────────────


class LlmClaimGuardEvent(_BaseHumannessEvent):
    """Booking-truth / transfer-truth guard fired.  Reason tells us
    whether the LLM tried to claim a booking or transfer without
    the receipt."""
    event_kind: Literal["llm_claim_guard"] = "llm_claim_guard"
    guard: str    # booking / transfer / message_taken
    claim_text_preview: str = ""
    receipt_present: bool = False
    action_taken: str = "rewrote"    # rewrote / blocked / warned


# ── convenience emit helper ───────────────────────────────────


def emit_humanness_event(event: _BaseHumannessEvent) -> None:
    """Write the event to the durable call_event_log.  Never raises.

    Consumers use this instead of directly touching call_event_log so
    the payload shape matches the event class's field contract.  If
    the log writer is unavailable, the event is dropped with a warning
    (humanness observability must never crash the call path).
    """
    try:
        from packages.observability.call_event_log import (
            get_call_event_log, CallEvent as _CE,
            EventSourceKind as _SK,
        )
        log = get_call_event_log()
        if log is None:
            return
        log.write(_CE(
            call_id=event.call_id or "?",
            tenant_id=event.tenant_id or "default",
            source=_SK.LLM,
            kind=event.event_kind,
            payload=event.model_dump(),
            turn_generation=event.turn_generation,
        ))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "emit_humanness_event(%s) failed: %s",
            type(event).__name__, e,
        )


__all__ = [
    "EmptyLlmCompletionEvent",
    "EmptyLlmRescueEvent",
    "EmptyLlmDeterministicFallbackEvent",
    "PolicyDecisionEvent",
    "TurnSignalReducedEvent",
    "ServiceResolutionEvent",
    "BargeInDetectedEvent",
    "SpeechGateDroppedEvent",
    "TransferAttemptEvent",
    "LlmClaimGuardEvent",
    "emit_humanness_event",
]
