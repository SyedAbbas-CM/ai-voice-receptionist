"""Generic structured-input session state machine.

R3 P0 (2026-08-13): the actor doesn't own slot-type-specific parsing.
The actor sees `session.feed(event)` and gets back a `SlotStatus`.
Everything else (spoken-digit normalization, libphonenumber
validation, DTMF handling, ANI lookup, retry counting) is inside
here or in the pluggable per-slot-type validator.

Slot types today:
  "phone" — Layer A (normalize_spoken_digits) + Layer B (phonenumbers)

Adding a new slot type later means:
  1. Write a validator function that takes a canonical string +
     tenant config and returns (status, canonical_value, reason).
  2. Register it in `packages/slot_parsers/registry.py`.
  No changes to the actor / dialogue kernel.

Design guardrails from doc #57:
  - No LLM counts or validates.
  - DTMF and speech feed the SAME session.
  - When capture is active, actor DISABLES speculative brain dispatch
    for that slot.
  - "Use the number I'm calling from" (ANI) is a first-class alternative,
    not a corner case.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class SlotStatus(str, Enum):
    INCOMPLETE = "incomplete"    # nothing / not enough yet, keep listening
    POSSIBLE = "possible"        # right shape but not fully valid; may accept with confirmation
    VALID = "valid"              # canonical value ready
    AMBIGUOUS = "ambiguous"      # multiple candidates; needs disambiguation prompt
    INVALID = "invalid"          # cannot be salvaged; ask caller to restart


class SlotSource(str, Enum):
    SPEECH = "speech"
    DTMF = "dtmf"
    ANI = "ani"      # caller ID / phone metadata lookup
    TEXT = "text"    # SMS / chat channel (future)


@dataclass
class SlotFragment:
    """One piece of input a session absorbed."""
    text: str
    source: SlotSource
    canonical: str = ""      # after Layer A normalization
    at: float = field(default_factory=time.monotonic)


@dataclass
class SlotResult:
    status: SlotStatus
    value: Optional[str] = None          # canonical form on VALID (E.164 for phone)
    reason: str = ""
    matched_region: Optional[str] = None
    raw_digits: str = ""                 # what we tried
    retries: int = 0


# Validator signature.  Each slot type registers one.
#   canonical_buffer: accumulated Layer-A output (digits + optional +)
#   config:           free-form dict from tenant / session config
# Returns SlotResult.
Validator = Callable[[str, dict], SlotResult]


@dataclass
class StructuredInputSession:
    """One live slot-capture session.  Actor creates one when it starts
    asking for a slot; feeds every relevant STT final / DTMF batch into
    it; reads `.result()` after each feed to decide what to say next.
    """
    slot_type: str                           # "phone", "email", "date", ...
    validator: Validator                     # from registry lookup
    config: dict = field(default_factory=dict)
    fragments: list[SlotFragment] = field(default_factory=list)

    # Buffer of Layer-A canonical strings accumulated across fragments.
    # For speech + DTMF + ANI all going through the same slot.
    buffer: str = ""

    # How many times we've completed a validation pass (for retry
    # backoff / max-retry escape).  Incremented on every feed().
    retries: int = 0

    # Timestamps for stall detection.
    created_at: float = field(default_factory=time.monotonic)
    last_activity_at: float = field(default_factory=time.monotonic)

    # Committed = actor accepted the current VALID result and moved on.
    # Session is inert after commit.
    committed: bool = False
    committed_value: Optional[str] = None

    def feed(
        self,
        raw_text: str,
        source: SlotSource = SlotSource.SPEECH,
        normalize: Optional[Callable[[str], str]] = None,
    ) -> SlotResult:
        """Absorb one STT final / DTMF batch / ANI lookup.

        normalize: Layer-A function.  For phone-shaped slots this is
        `normalize_spoken_digits`.  For other slot types (email, name)
        it may be identity or a slot-specific normalizer.  Registry
        wires the right one.

        Returns current SlotResult (POST-feed).
        """
        if self.committed:
            return SlotResult(
                status=SlotStatus.VALID,
                value=self.committed_value,
                reason="already committed",
            )

        canonical = normalize(raw_text) if normalize else raw_text.strip()
        frag = SlotFragment(
            text=raw_text, source=source, canonical=canonical,
        )
        self.fragments.append(frag)
        self.last_activity_at = frag.at

        if canonical:
            # First fragment can bring an E.164 `+`.  Later fragments'
            # `+` is treated as filler and stripped so we don't double-plus.
            if canonical.startswith("+") and not self.buffer:
                self.buffer = canonical
            else:
                self.buffer += canonical.lstrip("+")

        self.retries += 1
        return self._validate()

    def result(self) -> SlotResult:
        """Get the current validation result without adding a fragment."""
        if self.committed:
            return SlotResult(
                status=SlotStatus.VALID,
                value=self.committed_value,
                reason="already committed",
            )
        return self._validate()

    def _validate(self) -> SlotResult:
        r = self.validator(self.buffer, self.config)
        # attach retry count for observability
        return SlotResult(
            status=r.status,
            value=r.value,
            reason=r.reason,
            matched_region=r.matched_region,
            raw_digits=r.raw_digits or self.buffer,
            retries=self.retries,
        )

    def reset(self, reason: str = "") -> None:
        """Wipe the buffer.  Actor calls this on caller correction
        ("no wait, different number") or when switching input source
        (speech ↔ DTMF).  Fragments preserved for audit."""
        self.buffer = ""
        self.last_activity_at = time.monotonic()

    def commit(self, value: str) -> None:
        """Actor accepted the value.  Session becomes inert; further
        feed() calls no-op and return VALID."""
        self.committed = True
        self.committed_value = value

    def is_stale(self, timeout_s: float = 8.0) -> bool:
        """No activity for `timeout_s` since last fragment.  Actor
        uses this to decide when to prompt / give up / hand off."""
        if self.committed:
            return False
        return (time.monotonic() - self.last_activity_at) > timeout_s

    def audit_trail(self) -> list[dict]:
        """For per-call log / debugging."""
        return [
            {
                "text": f.text,
                "source": f.source.value,
                "canonical": f.canonical,
                "at": f.at,
            }
            for f in self.fragments
        ]
