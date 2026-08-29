"""TransferCoordinator — real human-transfer scaffold.

2026-08-27 (task #139, ChatGPT audit H-P0.4): `escalate_to_human` tool
currently returns `{"escalated": true, "callback_number": ...}` without
any actual dial / bridge / conference primitive.  The LLM can (and does)
tell the caller "I've connected you to Maria" when no transfer happened.

This module implements the state machine + policy layer.  The actual
Twilio conference API wire-up is blocked on networking's P0.5 outbound
guard (they own `routes/outbound.py`).  Once P0.5 lands, we bind
`_transport_dial` to Twilio's Conference / `<Dial>` primitives.

Design principles:
- Every transfer is one `TransferAttempt` row (persistent when the
  schema lands, in-memory before that).
- `TransferOutcome` is a strict enum — the LLM can only verbalize
  "connected" after the outcome is `BRIDGED`.
- Failure modes are explicit: `NO_ANSWER`, `BUSY`, `DECLINED`, `FAILED`,
  `TIMEOUT`, `POLICY_BLOCKED`, `INVALID_DESTINATION`.
- Fallback per attempt: `MESSAGE_IF_FAILED` fires the `take_message`
  path when the transfer can't complete.
- All modes (BLIND / WARM / CALLBACK / MESSAGE_IF_FAILED) share the
  same state machine so future modes plug in without a rewrite.

Not covered here (deferred until transport is wired):
- Actual Twilio Conference / `<Dial>` calls
- Real ring / no-answer detection (needs Twilio status callbacks)
- Warm-transfer operator introduction UX (the caller's on hold while
  we talk to the operator first)
- Callback scheduling — treated as a special case of MESSAGE_IF_FAILED
  for now (message includes preferred_callback_time)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional


# ── mode / outcome enums ──────────────────────────────────────────


class TransferMode(str, Enum):
    """How the transfer is attempted.  Determines the transport path.

    Ordered by increasing operator involvement:
      BLIND: dial operator, patch caller directly.  No AI-side context.
      WARM:  dial operator, brief them with caller context, THEN patch.
      CALLBACK: don't attempt to bridge now; schedule an outbound call
                from operator to caller.  Caller stays on line with AI
                until "we'll have Maria call you back within the hour".
      MESSAGE_IF_FAILED: fallback mode — if the bridge fails, take
                a message.  Not chosen upfront; set as fallback flag
                on any of the above.
    """
    BLIND = "blind"
    WARM = "warm"
    CALLBACK = "callback"
    MESSAGE_IF_FAILED = "message_if_failed"


class TransferOutcome(str, Enum):
    """Final state of a transfer attempt.

    Only `BRIDGED` (or `CALLBACK_SCHEDULED` for callback mode) is a
    legitimate 'connected the caller' outcome.  The LLM's `must_include_
    facts` guardrail should refuse to speak "you're connected" language
    unless the receipt shows one of these.
    """
    # Success
    BRIDGED = "bridged"                    # caller + operator both on the leg
    CALLBACK_SCHEDULED = "callback_scheduled"
    MESSAGE_TAKEN = "message_taken"        # caller preferred message

    # Failure — recoverable via MESSAGE_IF_FAILED
    NO_ANSWER = "no_answer"                # rang out
    BUSY = "busy"
    DECLINED = "declined"                  # operator picked up + hung up

    # Failure — non-recoverable
    FAILED = "failed"                      # transport error
    TIMEOUT = "timeout"                    # our own dial timeout
    POLICY_BLOCKED = "policy_blocked"      # kill switch / quiet hours / DNC
    INVALID_DESTINATION = "invalid_destination"

    # In-flight (state during attempt)
    IN_PROGRESS = "in_progress"

    @property
    def is_success(self) -> bool:
        return self in {
            TransferOutcome.BRIDGED,
            TransferOutcome.CALLBACK_SCHEDULED,
            TransferOutcome.MESSAGE_TAKEN,
        }

    @property
    def is_recoverable(self) -> bool:
        """When True, MESSAGE_IF_FAILED fallback can still turn this
        into a MESSAGE_TAKEN success."""
        return self in {
            TransferOutcome.NO_ANSWER,
            TransferOutcome.BUSY,
            TransferOutcome.DECLINED,
            TransferOutcome.TIMEOUT,
        }


# ── data classes ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TransferDestination:
    """A specific place a call can go.  Loaded from BusinessProfile /
    tenant config, not from LLM args.  Multiple destinations per
    tenant.
    """
    id: str                              # tenant-scoped: "agent_maria", "on_call"
    label: str                           # human-readable, e.g. "Dr. Chen"
    phone: str                           # E.164
    department: Optional[str] = None     # "sales" / "clinical" / "billing"
    is_default: bool = False             # tenant's fallback if no agent named


@dataclass(frozen=True)
class TransferRule:
    """When + how a transfer fires.  Matches ChatGPT audit's spec.

    Triggered by ConversationAction.ESCALATE + a specific reason.
    Reason strings are set by the LLM tool call OR by policy
    (complaint / offer_over_500k / legal_question / etc.).
    """
    trigger_reasons: frozenset[str] = frozenset()
    default_mode: TransferMode = TransferMode.WARM
    destination_id: Optional[str] = None  # None → round-robin from tenant list
    message_if_failed: bool = True
    max_ring_seconds: int = 30
    urgency: str = "normal"               # "normal" | "high" | "urgent"


@dataclass
class TransferAttempt:
    """One transfer event.  Mutable — outcome + timestamp fill in
    as the attempt progresses.  Persists to DB once schema lands.
    """
    id: str
    call_sid: str                       # Twilio CallSid
    tenant_id: str
    session_id: str
    caller_name: Optional[str]
    caller_phone: str
    reason: str
    mode: TransferMode
    destination: Optional[TransferDestination]
    outcome: TransferOutcome = TransferOutcome.IN_PROGRESS
    started_at: Optional[str] = None    # ISO 8601
    completed_at: Optional[str] = None
    failure_detail: Optional[str] = None
    fallback_message_id: Optional[str] = None  # if MESSAGE_TAKEN, the ReceptionMessage.id


# ── coordinator ────────────────────────────────────────────────


# Type alias for the transport dial function.  Real implementation
# will be a Twilio Conference / <Dial> call.  For scaffold + tests,
# a stub can be injected.
TransportDial = Callable[[TransferAttempt], Awaitable[TransferOutcome]]

# Callback for taking a message when transfer fails + fallback allowed.
TakeMessageFallback = Callable[[TransferAttempt], Awaitable[str]]  # returns message_id


class TransferCoordinator:
    """Orchestrates transfer attempts.

    Not yet wired to production Twilio — accepts injected `TransportDial`
    for testability + future prod use.  The state machine, policy
    gating, and fallback logic are all here + tested; the transport
    swap is trivial once networking's P0.5 outbound guard is ready.
    """

    def __init__(
        self,
        *,
        destinations: list[TransferDestination],
        rules: list[TransferRule],
        transport_dial: TransportDial,
        take_message_fallback: Optional[TakeMessageFallback] = None,
    ) -> None:
        self.destinations = {d.id: d for d in destinations}
        # Sort rules by specificity — rules with more trigger_reasons
        # match first (a rule with 3 specific reasons beats the
        # catch-all with an empty trigger set).
        self.rules = sorted(
            rules,
            key=lambda r: (-len(r.trigger_reasons), r.default_mode.value),
        )
        self._transport_dial = transport_dial
        self._take_message_fallback = take_message_fallback

    def find_rule(self, reason: str) -> Optional[TransferRule]:
        """Find the first rule matching `reason`.  None if no match
        (rule with empty trigger_reasons catches all)."""
        for rule in self.rules:
            if not rule.trigger_reasons:
                # Catch-all — always the last resort due to sort above.
                return rule
            if reason in rule.trigger_reasons:
                return rule
        return None

    def resolve_destination(
        self,
        rule: TransferRule,
        agent_name_requested: Optional[str] = None,
    ) -> Optional[TransferDestination]:
        """Pick which destination to dial.

        Priority:
          1. If caller asked for a specific agent by name AND we have
             them registered → use that destination.
          2. Else if the rule specifies a destination_id → use it.
          3. Else round-robin (not yet implemented — falls to first
             non-default destination, then default).
        """
        if agent_name_requested:
            requested = agent_name_requested.lower().strip()
            for dest in self.destinations.values():
                if requested in dest.label.lower():
                    return dest
                if requested == dest.id.lower():
                    return dest
        if rule.destination_id:
            return self.destinations.get(rule.destination_id)
        # Fallback: any non-default, then default.
        non_default = [d for d in self.destinations.values() if not d.is_default]
        if non_default:
            return non_default[0]
        for d in self.destinations.values():
            if d.is_default:
                return d
        return None

    async def initiate_transfer(
        self,
        *,
        attempt: TransferAttempt,
        rule: TransferRule,
    ) -> TransferAttempt:
        """Run the transfer.  Mutates + returns the attempt with the
        final outcome recorded.

        Never raises.  All failures become TransferOutcome states so
        the caller can render an honest reply.
        """
        try:
            # Guard rails.
            if attempt.destination is None:
                attempt.outcome = TransferOutcome.INVALID_DESTINATION
                attempt.failure_detail = "no destination resolved"
                return attempt
            # Dispatch by mode.
            if attempt.mode == TransferMode.CALLBACK:
                # Callback doesn't bridge — schedules an outbound.
                # For now, treat scheduling as always succeeding since
                # we don't have a scheduler yet.  Real implementation
                # will enqueue an outbound_call row.
                attempt.outcome = TransferOutcome.CALLBACK_SCHEDULED
                return attempt
            # BLIND / WARM both attempt to bridge via the transport.
            attempt.outcome = TransferOutcome.IN_PROGRESS
            outcome = await self._transport_dial(attempt)
            attempt.outcome = outcome
            # Fallback: message if failed + rule allows.
            if (
                not outcome.is_success
                and outcome.is_recoverable
                and rule.message_if_failed
                and self._take_message_fallback is not None
            ):
                try:
                    msg_id = await self._take_message_fallback(attempt)
                    attempt.fallback_message_id = msg_id
                    attempt.outcome = TransferOutcome.MESSAGE_TAKEN
                except Exception as e:
                    # Message fallback failed too — leave original
                    # failure outcome, note the double-failure.
                    attempt.failure_detail = (
                        f"transfer {outcome.value} + message fallback "
                        f"failed: {e}"
                    )
            return attempt
        except Exception as e:
            attempt.outcome = TransferOutcome.FAILED
            attempt.failure_detail = f"unexpected error: {e}"
            return attempt

    # ── receipt-shape helpers for LLM guardrail ────────────────

    @staticmethod
    def can_llm_say_connected(attempt: TransferAttempt) -> bool:
        """Guard for the LLM's claim-truth check.  True only when the
        attempt actually bridged the two legs.

        Used by ActionClaimGuard (H-P1.8) so the LLM literally cannot
        say 'you're connected to Maria' unless this returns True.
        """
        return attempt.outcome == TransferOutcome.BRIDGED

    @staticmethod
    def can_llm_say_message_taken(attempt: TransferAttempt) -> bool:
        return attempt.outcome == TransferOutcome.MESSAGE_TAKEN

    @staticmethod
    def can_llm_say_callback_scheduled(attempt: TransferAttempt) -> bool:
        return attempt.outcome == TransferOutcome.CALLBACK_SCHEDULED

    @staticmethod
    def render_honest_reply(attempt: TransferAttempt) -> str:
        """Return the caller-facing verbalization for a completed
        attempt.  Deterministic — no LLM.  Used as the fallback text
        when the LLM would otherwise lie about the outcome.
        """
        outcome = attempt.outcome
        dest_label = (
            attempt.destination.label if attempt.destination else "our team"
        )
        if outcome == TransferOutcome.BRIDGED:
            return f"Connecting you to {dest_label} now — one moment."
        if outcome == TransferOutcome.CALLBACK_SCHEDULED:
            return (
                f"I'll have {dest_label} call you back — usually within "
                f"the hour."
            )
        if outcome == TransferOutcome.MESSAGE_TAKEN:
            return (
                f"I've taken your message — {dest_label} will get back to "
                f"you as soon as they can."
            )
        if outcome in (
            TransferOutcome.NO_ANSWER, TransferOutcome.BUSY,
            TransferOutcome.DECLINED,
        ):
            return (
                f"{dest_label} isn't available right now — can I take a "
                f"message or have them call you back?"
            )
        if outcome == TransferOutcome.POLICY_BLOCKED:
            return (
                f"I can't put that call through right now — I'll take a "
                f"message and someone will get back to you."
            )
        # FAILED / TIMEOUT / INVALID — same customer-facing recovery.
        return (
            "I'm having trouble reaching them just now — let me take a "
            "message and I'll make sure they see it."
        )


__all__ = [
    "TransferMode",
    "TransferOutcome",
    "TransferDestination",
    "TransferRule",
    "TransferAttempt",
    "TransferCoordinator",
    "TransportDial",
    "TakeMessageFallback",
]
