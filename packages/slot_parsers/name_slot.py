"""Name slot: Layer A normalizer + Layer B validator.

2026-08-30 (task #142): adapted from LK's beta/workflows/name.py.
Includes LK's `_clean_name_arg` defensive scrub for null/none LLM
pollution — worth stealing verbatim.

## Normalization (Layer A)

Callers give names in many shapes:
  * 'John Smith'
  * 'my name is John Smith'
  * 'This is John, John Smith'
  * 'S-M-I-T-H, first name John'  (spellback)

Normalizer:
  * Strip common intro phrases ('my name is', 'this is', 'it's', 'I am')
  * Preserve casing (proper-noun display matters)
  * Collapse whitespace
  * De-hyphenate spellback ('S-M-I-T-H' → 'SMITH')

## Validation (Layer B)

Not a regex checker — real names contain apostrophes, hyphens,
diacritics, single letters (as initials). Instead:
  * VALID: 2+ characters, at least one letter, no obvious junk
  * INVALID: contains 'null' / 'none' / 'n/a' / empty / all digits
  * POSSIBLE: single-word name (may want to confirm with full name
    for booking)

The `_clean_name_arg` scrub — from LK — is the real value here.
When gpt-4o-mini class models hallucinate they often pass 'null'
or '"none"' as string values. Explicit defensive normalize catches
that at the tool boundary.
"""
from __future__ import annotations

import re
from typing import Optional

from .session import SlotResult, SlotStatus


# ── LK-inspired defensive scrub ──────────────────────────────


_LLM_JUNK_TOKENS = {
    "", "null", "none", "n/a", "na", "unknown", "undefined",
    "user", "customer", "caller", "the caller", "the user",
    "no name given", "not provided", "not given",
    "test", "test user", "test caller",
}


def _clean_name_arg(raw: Optional[str]) -> str:
    """LK's null/none defensive scrub. Returns empty string on any
    known junk pattern; original stripped-string otherwise. Never
    raises."""
    if not raw:
        return ""
    s = str(raw).strip()
    if s.lower() in _LLM_JUNK_TOKENS:
        return ""
    # Sometimes LLM wraps in quotes.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
        if s.lower() in _LLM_JUNK_TOKENS:
            return ""
    return s


# ── normalization (Layer A) ──────────────────────────────────


_NAME_INTROS = [
    re.compile(r"^\s*my name is\s+", re.I),
    re.compile(r"^\s*this is\s+", re.I),
    re.compile(r"^\s*it['’]s\s+", re.I),
    re.compile(r"^\s*i am\s+", re.I),
    re.compile(r"^\s*i['’]m\s+", re.I),
    re.compile(r"^\s*name['’]s?\s+", re.I),
    re.compile(r"^\s*the name is\s+", re.I),
    # Trailing junk phrases callers add after their name.
    re.compile(r"\s+that['’]s (?:my name|correct)\s*$", re.I),
    re.compile(r"\s+is my name\s*$", re.I),
]


def normalize_name(raw: str) -> str:
    """Layer A: strip intro phrases, de-hyphenate spellback,
    collapse whitespace. Preserves case."""
    if not raw:
        return ""
    out = raw.strip()
    for pat in _NAME_INTROS:
        out = pat.sub("", out)
    # De-hyphenate short-letter spellback ('S-M-I-T-H' → 'SMITH').
    # Only when the segment reads as letter-hyphen-letter-hyphen...
    def _dehyphen(match):
        seq = match.group(0)
        parts = seq.split("-")
        if all(len(p) == 1 and p.isalpha() for p in parts):
            return "".join(parts)
        return seq
    out = re.sub(r"\b(?:[a-zA-Z]-){2,}[a-zA-Z]\b", _dehyphen, out)
    # Collapse whitespace.
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ── validation (Layer B) ────────────────────────────────────


_NAME_HAS_LETTER = re.compile(r"[a-zA-Z]")
_NAME_ALL_DIGITS = re.compile(r"^\d+$")


def name_validator(canonical: str, config: dict) -> SlotResult:
    """Layer B: canonical → SlotResult. Never raises.

    * INVALID: empty after cleaning, all digits, matches LK junk pattern
    * INCOMPLETE: single character, or looks like a fragment
    * POSSIBLE: single word (a first name only — may want full name)
    * VALID: two+ words OR a single word 2+ chars with letters
    """
    cleaned = _clean_name_arg(canonical)
    if not cleaned:
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical or "",
            reason="empty or junk pattern",
        )
    if _NAME_ALL_DIGITS.match(cleaned):
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason="all digits — not a name",
        )
    if not _NAME_HAS_LETTER.search(cleaned):
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason="no letters",
        )
    if len(cleaned) < 2:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits=canonical,
            reason="single character",
        )
    words = cleaned.split()
    # Single-word full name: valid (first-name only is common for
    # informal bookings) but flag as POSSIBLE so actor can decide
    # whether to ask 'and last name?' based on the vertical.
    if len(words) == 1:
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value=cleaned,
            raw_digits=canonical,
            reason="single-word name; may want full name",
        )
    # Multi-word → VALID.
    return SlotResult(
        status=SlotStatus.VALID,
        value=cleaned,
        raw_digits=canonical,
    )


__all__ = [
    "normalize_name",
    "name_validator",
    "_clean_name_arg",
]
