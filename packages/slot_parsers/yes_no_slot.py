"""Yes/no slot: Layer A normalizer + Layer B validator.

2026-08-30 (task #142): the smallest of the four slot parsers.
Adapted from LK's boolean-confirmation shape (10-line file, no
confirm tool needed — the answer IS the confirmation).

## Normalization (Layer A)

Callers rarely say a clean 'yes' or 'no'. Real shapes:
  * yes: 'yes', 'yeah', 'yep', 'yup', 'yeah okay', 'sure', 'yes please',
    'absolutely', 'confirmed', 'that's right', 'that's correct', 'go
    ahead', 'sounds good', 'perfect', 'right'
  * no: 'no', 'nope', 'nah', 'no thanks', 'no way', 'never mind',
    'actually no', 'cancel that', 'wrong', 'not right'
  * ambiguous: 'kind of', 'maybe', 'I think so', 'not really', 'I
    don't know'

## Validation (Layer B)

Simple keyword match with confidence. VALID for clear yes/no,
POSSIBLE for the ambiguous shapes (actor can re-ask), INVALID for
unrelated utterances.

The value stored is 'yes' or 'no' — canonical string. Downstream
booking-tool args are already booleans; caller wires that shape
conversion.
"""
from __future__ import annotations

import re
from typing import Optional

from .session import SlotResult, SlotStatus


# ── keyword sets ─────────────────────────────────────────


_YES_KEYWORDS = {
    "yes", "yeah", "yep", "yup", "yea", "aye", "ya",
    "yes please", "yes thanks", "yes indeed",
    "sure", "sure thing", "absolutely", "definitely",
    "of course", "please do", "please",
    "confirmed", "correct", "that's right", "that is right",
    "that's correct", "that is correct",
    "go ahead", "sounds good", "perfect", "great",
    "right", "affirmative", "roger", "10-4",
    "ok", "okay", "kk", "gotcha", "got it",
    "do it", "book it", "book me",
}


_NO_KEYWORDS = {
    "no", "nope", "nah", "no thanks", "no thank you",
    "no way", "never mind", "nevermind",
    "actually no", "wait no",
    "wrong", "incorrect", "not right", "not correct",
    "cancel that", "cancel", "stop",
    "negative", "hell no", "absolutely not",
    "don't", "don't do it", "do not",
    "hold on", "hang on",   # often means 'wait, not yet' during confirm
}


_AMBIGUOUS_KEYWORDS = {
    "maybe", "kind of", "kinda", "sort of", "sorta",
    "i think so", "i think", "probably",
    "not really", "not sure", "not certain",
    "i don't know", "i dont know", "dunno",
    "let me think", "hmm", "uhh", "uhm",
}


# ── normalization ──────────────────────────────────────


def normalize_yes_no(raw: str) -> str:
    """Layer A: lowercase, strip punctuation + filler."""
    if not raw:
        return ""
    out = raw.lower().strip()
    # Strip common filler prefixes/suffixes.
    out = re.sub(r"^(?:um+|uh+|well|so)[,\s]+", "", out)
    out = re.sub(r"[,.!?;:]+$", "", out).strip()
    return out


# ── validation ─────────────────────────────────────────


def yes_no_validator(canonical: str, config: dict) -> SlotResult:
    """Layer B: canonical → SlotResult.

    * VALID with value='yes' or 'no' when clear match
    * POSSIBLE when ambiguous ('maybe', 'kind of')
    * INVALID when utterance is unrelated
    """
    if not canonical:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits="",
            reason="empty",
        )
    text = canonical.strip().lower()
    # Check ambiguous FIRST — some phrases contain 'yes' or 'no' as
    # substrings ('not sure' contains 'no', 'i think so' contains no
    # single yes/no keyword but is ambiguous).
    if text in _AMBIGUOUS_KEYWORDS:
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value=None,
            raw_digits=canonical,
            reason=f"ambiguous: {text!r} — re-ask directly",
        )
    for phrase in _AMBIGUOUS_KEYWORDS:
        if phrase in text.split():
            return SlotResult(
                status=SlotStatus.POSSIBLE,
                value=None,
                raw_digits=canonical,
                reason=f"contains ambiguous marker: {phrase!r}",
            )
    # Check YES + NO. Exact match first (highest confidence).
    if text in _YES_KEYWORDS:
        return SlotResult(
            status=SlotStatus.VALID, value="yes",
            raw_digits=canonical,
        )
    if text in _NO_KEYWORDS:
        return SlotResult(
            status=SlotStatus.VALID, value="no",
            raw_digits=canonical,
        )
    # Prefix/substring — 'yes go ahead and book' → yes.
    # Check first word.
    first = text.split(" ", 1)[0]
    if first in _YES_KEYWORDS:
        return SlotResult(
            status=SlotStatus.VALID, value="yes",
            raw_digits=canonical,
        )
    if first in _NO_KEYWORDS:
        return SlotResult(
            status=SlotStatus.VALID, value="no",
            raw_digits=canonical,
        )
    # Full-text substring search — last resort. Catches 'okay yeah
    # let's do it' patterns.  Order: check NO before YES so
    # negations like "no that's not right" don't misfire as YES
    # via a "right" hit later in the string.
    for kw in _NO_KEYWORDS:
        if f" {kw} " in f" {text} " or text.startswith(kw + " "):
            return SlotResult(
                status=SlotStatus.VALID, value="no",
                raw_digits=canonical,
            )
    for kw in _YES_KEYWORDS:
        if f" {kw} " in f" {text} " or text.startswith(kw + " "):
            return SlotResult(
                status=SlotStatus.VALID, value="yes",
                raw_digits=canonical,
            )
    return SlotResult(
        status=SlotStatus.INVALID,
        raw_digits=canonical,
        reason="no yes/no signal",
    )


__all__ = [
    "normalize_yes_no",
    "yes_no_validator",
]
