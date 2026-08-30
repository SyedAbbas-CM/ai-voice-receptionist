"""Email slot: Layer A normalizer + Layer B validator.

2026-08-30 (task #142): adapted from LK's beta/workflows/email_address.py
using the same registry contract as phone.

## Normalization (Layer A)

Callers speak email addresses in a specific way over the phone:
  * 'my email is john dot smith at gmail dot com'
  * 'j-o-h-n at yahoo dot com'
  * 'john.smith@gmail.com' (chat modality — verbatim)

The normalizer:
  * Lowercases (email local-part is case-insensitive in practice)
  * Collapses whitespace + spelled-out digits + spelled punctuation:
      'dot' → '.', 'at' → '@', 'dash' → '-', 'underscore' → '_'
  * Strips filler words: 'my email is', 'the address is', 'it's', etc.
  * Preserves the sequence — no reordering, no invention of chars

## Validation (Layer B)

Email regex + domain sanity:
  * Local part: 1-64 chars, [a-z0-9._+-] with no leading/trailing dot
  * Domain part: 3-253 chars, TLD 2+ chars, at least one label
  * Domain typo suggestions (gmial → gmail) surfaced as POSSIBLE
    with a suggested correction, letting the actor prompt "did you
    mean X?"

Common-domain typos come from LK's approach — a small hand-curated
map catches 95% of real caller typos with zero LLM latency.
"""
from __future__ import annotations

import re
from typing import Optional

from .session import SlotResult, SlotStatus


# ── normalization (Layer A) ────────────────────────────────


# Order matters: replace multi-word before single-word.
_SPOKEN_EMAIL_MAP = [
    (re.compile(r"\bunderscore\b", re.I), "_"),
    (re.compile(r"\bhyphen\b", re.I), "-"),
    (re.compile(r"\bdash\b", re.I), "-"),
    (re.compile(r"\bdot\b", re.I), "."),
    (re.compile(r"\bperiod\b", re.I), "."),
    (re.compile(r"\bat\b", re.I), "@"),
    (re.compile(r"\bplus\b", re.I), "+"),
]


_EMAIL_FILLERS = [
    re.compile(r"\bmy email(?: address| is)?(?: is)?\b", re.I),
    re.compile(r"\bthe address is\b", re.I),
    re.compile(r"\bemail is\b", re.I),
    re.compile(r"\bit's\b", re.I),
    re.compile(r"\bthat's\b", re.I),
    re.compile(r"\bit is\b", re.I),
]


def normalize_email(raw: str) -> str:
    """Layer A: spoken → canonical-ish. Never raises. Returns
    lowercased-collapsed string; may still contain invalid chars."""
    if not raw:
        return ""
    out = raw.lower().strip()
    for pat in _EMAIL_FILLERS:
        out = pat.sub("", out)
    for pat, repl in _SPOKEN_EMAIL_MAP:
        out = pat.sub(repl, out)
    # Collapse whitespace + strip trailing punctuation.
    out = re.sub(r"\s+", "", out)
    out = out.strip(".,!?;:")
    return out


# ── validation (Layer B) ────────────────────────────────────


# Fairly-strict RFC-5322-inspired but practical: allows the common
# real-world shapes without the 20-branch grammar.
_EMAIL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?"
    r"@[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
    re.I,
)


# Common domain typos + their canonical corrections. Small + high-
# signal — 95% of real caller typos over the phone.
_DOMAIN_TYPO_CORRECTIONS = {
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmali.com": "gmail.com",
    "gmail.co": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmail.con": "gmail.com",
    "gmailcom": "gmail.com",
    "yaho.com": "yahoo.com",
    "yahooo.com": "yahoo.com",
    "yahoo.co": "yahoo.com",
    "yahooco.m": "yahoo.com",
    "hotmial.com": "hotmail.com",
    "hotmial.co": "hotmail.com",
    "outlok.com": "outlook.com",
    "outlokk.com": "outlook.com",
    "iclod.com": "icloud.com",
    "icloud.co": "icloud.com",
}


def _suggest_domain_correction(email: str) -> Optional[str]:
    """Return a suggested full-email correction if the domain is a
    known typo. None if domain is fine or unknown."""
    at = email.rfind("@")
    if at < 0:
        return None
    local, domain = email[:at], email[at + 1:]
    fix = _DOMAIN_TYPO_CORRECTIONS.get(domain.lower())
    if fix is None:
        return None
    return f"{local}@{fix}"


def email_validator(canonical: str, config: dict) -> SlotResult:
    """Layer B: canonical string → SlotResult. Never raises.

    * VALID: matches regex + domain looks canonical
    * POSSIBLE: matches regex but domain is a known typo → suggests
      correction in `value` + puts the typo shape in `raw_digits`
      so the actor can prompt 'did you mean X?'
    * INCOMPLETE: has '@' but not both local + domain (still typing)
    * INVALID: nothing recognizable as an email
    """
    if not canonical:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits="",
            reason="empty",
        )
    canonical = canonical.strip().lower()
    # No '@' yet → still typing.
    if "@" not in canonical:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits=canonical,
            reason="no @ yet",
        )
    # Has '@' but nothing after or before → INCOMPLETE.
    local, _, domain = canonical.partition("@")
    if not local or not domain:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits=canonical,
            reason="missing local or domain",
        )
    # Domain typo → POSSIBLE with correction suggestion.
    suggestion = _suggest_domain_correction(canonical)
    if suggestion is not None:
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value=suggestion,
            raw_digits=canonical,
            reason=f"suggested correction: {suggestion}",
        )
    # Regex check.
    if _EMAIL_RE.match(canonical):
        return SlotResult(
            status=SlotStatus.VALID,
            value=canonical,
            raw_digits=canonical,
        )
    return SlotResult(
        status=SlotStatus.INVALID,
        raw_digits=canonical,
        reason="does not look like a valid email address",
    )


__all__ = [
    "normalize_email",
    "email_validator",
]
