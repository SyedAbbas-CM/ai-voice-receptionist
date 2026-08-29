"""Compute `missing_slots` for NextActionPolicy from conversation state.

2026-08-29 (bug diagnosis from CA3dac680ae8661459bc74735603f2cbc9):
brain.py was calling `build_decision_state_with_signals(...)` without
passing `missing=`, so `state.missing` was ALWAYS empty, so
`NextActionPolicy.decide()` always fell through to `action=ANSWER`,
so the ASK_SLOT branch (and every downstream: enter_slot_capture, LK
sub-agent, structured phone-capture) was completely dormant on live
calls.

This module supplies the missing input: given the current conversation
state, detect booking intent + compute which required slots we still
need.

Design:
  * Booking-intent detection is keyword-based over the caller's recent
    utterances.  Cheap, deterministic, no LLM.
  * "Required for booking" = book_appointment / book_reservation /
    book_viewing tool schema `required` list, minus fields already in
    `known_slots`.
  * Returned in a stable, human-natural order: service first (need to
    know WHAT), then when (date, time), then contact (caller_name,
    phone).  Phone LAST — matches how real receptionists work; asking
    for phone before understanding the intent feels transactional.
  * Non-booking turns return `[]` — policy falls through to ANSWER as
    intended.

Never raises.  Import guard around every reader so a state shape drift
downgrades to "no intent detected" instead of crashing the turn.
"""
from __future__ import annotations

import re
from typing import Optional


# ── booking-intent keyword patterns ─────────────────────────────

# Any of these in the caller's recent utterances signals booking-intent.
# Kept intentionally BROAD — false positive (fires slot-collection when
# caller just asked a question) is worse than false negative (fires
# ANSWER when caller wanted to book).  We can tighten with a real
# classifier later; for now the pattern is precision-tolerant because
# ASK_SLOT is a light-touch action (asks ONE question, moves on).
_BOOKING_INTENT_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bbook\b",
        r"\bschedule\b",
        r"\bappointment\b",
        r"\bset (?:up|me up|it up)\b",
        r"\bget (?:me )?in\b",
        r"\bcome in\b",
        r"\bcheck ?up\b",
        r"\bcheck-up\b",
        r"\bexam\b",
        r"\bcleaning\b",
        r"\bfilling\b",
        r"\bcavity\b",
        r"\bconsult(?:ation)?\b",
        r"\bfollow[- ]?up\b",
        r"\brecall\b",
        r"\bviewing\b",
        r"\bshow ?ing\b",
        r"\btour\b",
        r"\breservation\b",
        r"\btable\b",
        r"\bavailab(?:le|ility)\b",
        r"\bopen(?:ing)?s?\b(?=\s+(?:for|on|next|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|this|that))",
        r"\btomorrow\b",
        r"\bnext week\b",
        r"\bthis (?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening)\b",
    )
)


def _has_booking_intent(text: str) -> bool:
    """True if the caller's utterance looks like booking-intent."""
    if not text:
        return False
    for pat in _BOOKING_INTENT_PATTERNS:
        if pat.search(text):
            return True
    return False


# ── canonical booking-required slots ───────────────────────────

# Ordered for natural conversation flow.  Real receptionist asks:
#   1. WHAT they need (service)     - understand intent first
#   2. WHEN they need it (date, time) - narrow the search
#   3. WHO they are (caller_name)   - contact info second
#   4. PHONE (phone)                - contact info LAST
# Phone-last matches human habit and is the highest-leverage slot for
# the LK sub-agent prompt (structured capture with read-back).
_BOOKING_REQUIRED_ORDER = (
    "service",
    "date",
    "time",
    "caller_name",
    "phone",
)


# ── main API ────────────────────────────────────────────────


def compute_missing_slots(
    known_slots: dict,
    recent_caller_texts: list[str],
    booking_intent_signal: Optional[bool] = None,
) -> list[str]:
    """Return the ordered list of slots still needed for a booking.

    Args:
      known_slots: what we already have (from _extract_known_slots +
        anything else already committed to state).  Keys are the
        canonical slot names in _BOOKING_REQUIRED_ORDER.
      recent_caller_texts: last N caller utterances, newest first.
        Used for intent detection.  Pass just the caller lines, not
        agent replies.
      booking_intent_signal: OPTIONAL override.  When True, forces
        booking-intent detection regardless of keyword match (e.g.
        already-in-progress booking flow).  When False, forces
        no-intent regardless of matches.  Default None = infer from
        recent_caller_texts.

    Returns:
      [] when no booking intent detected → NextActionPolicy falls
      through to ANSWER as before.  Non-empty ordered list when
      booking-intent + slots are missing → NextActionPolicy fires
      ASK_SLOT on missing[0].

    Never raises.  Malformed input → [].
    """
    try:
        # 1. Detect booking intent.
        if booking_intent_signal is True:
            has_intent = True
        elif booking_intent_signal is False:
            has_intent = False
        else:
            has_intent = any(
                _has_booking_intent(t) for t in recent_caller_texts
                if isinstance(t, str)
            )
        if not has_intent:
            return []
        # 2. Compute what's missing.
        known = known_slots or {}
        missing: list[str] = []
        for slot in _BOOKING_REQUIRED_ORDER:
            if not known.get(slot):
                missing.append(slot)
        return missing
    except Exception:
        return []


def recent_caller_texts_from_state(state, limit: int = 5) -> list[str]:
    """Extract the last N caller utterances from a conversation state.

    Defensive wrapper — different state shapes across our code base.
    Reads whatever is available: `state.transcript`, `state.turns`,
    or the compact `state.recent_user_text`.  Returns a list of
    strings, newest first, up to `limit` long.  Empty on any shape
    we can't parse.
    """
    try:
        # Preferred: state.transcript is a list of TranscriptTurn.
        transcript = getattr(state, "transcript", None)
        if transcript:
            texts: list[str] = []
            for turn in reversed(transcript):
                role = getattr(turn, "role", None)
                role_val = (
                    role.value if hasattr(role, "value") else str(role)
                )
                if role_val and role_val.lower() in ("user", "caller"):
                    text = getattr(turn, "text", "") or ""
                    if text.strip():
                        texts.append(text)
                        if len(texts) >= limit:
                            break
            if texts:
                return texts
        # Fallback: state.turns
        turns = getattr(state, "turns", None)
        if turns:
            texts = [
                t.get("text", "") if isinstance(t, dict) else ""
                for t in reversed(list(turns))
                if (
                    isinstance(t, dict)
                    and str(t.get("role", "")).lower() in ("user", "caller")
                    and t.get("text")
                )
            ][:limit]
            if texts:
                return texts
        # Fallback: single recent-text field.
        one = getattr(state, "recent_user_text", None) or ""
        if one:
            return [one]
    except Exception:
        pass
    return []


__all__ = [
    "compute_missing_slots",
    "recent_caller_texts_from_state",
]
