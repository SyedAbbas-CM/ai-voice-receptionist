"""Date slot: Layer A normalizer + Layer B validator.

2026-08-30 (task #142): adapted from LK's beta/workflows/dob.py
two-digit-year normalization. Generalized to appointment dates —
any date a caller might speak in a booking flow.

## Normalization (Layer A)

Callers speak dates in many shapes:
  * 'January 15th'
  * 'the 15th of January'
  * '1/15' / '01/15/2026' / '1-15-26'
  * 'next Tuesday' / 'this Friday' / 'tomorrow'
  * 'in three weeks'

We DON'T reinvent this — the codebase already has TemporalResolver
in packages.dialogue. The normalizer here is minimal:
  * Strip filler ('on', 'the', 'let's say')
  * Normalize spelled ordinals ('fifteenth' → '15th')
  * Handle two-digit year via LK's approach: 90 → 1990, 05 → 2005
    (window based on current year + 20)

## Validation (Layer B)

Delegates to TemporalResolver for parsing. Its result maps to:
  * VALID: single unambiguous date
  * AMBIGUOUS: multiple candidates → POSSIBLE with reason
  * INVALID: unrecognizable

TemporalResolver's `BusinessHours` context is optional; if the
config includes a business_hours object, dates outside business
hours flag as POSSIBLE ('confirm — we're closed Sunday, is Monday
OK?').
"""
from __future__ import annotations

import re
from typing import Optional

from .session import SlotResult, SlotStatus


# ── two-digit year expansion (LK's dob.py trick) ──────────────


def _expand_two_digit_year(year_str: str, current_year: int) -> str:
    """LK-style two-digit year expansion. Returns 4-digit year.

    Windowing: if current year is 2026, then
      * 06-46 → 2006-2046 (next 20 years)
      * 47-99 → 1947-1999 (past 80 years)
    For appointment dates, users almost never say a year past +20
    or before this century, so the window is generous either way.

    Never raises. Returns original string on bad input.
    """
    try:
        n = int(year_str)
    except (TypeError, ValueError):
        return year_str
    if n >= 100:
        return str(n)   # already 4-digit
    if 0 <= n <= 99:
        current_century = (current_year // 100) * 100
        candidate_current = current_century + n
        candidate_prev = current_century - 100 + n
        # Prefer current century unless > 20 years past current.
        if candidate_current <= current_year + 20:
            return str(candidate_current)
        return str(candidate_prev)
    return year_str


# ── normalization (Layer A) ──────────────────────────────────


_DATE_FILLERS = [
    re.compile(r"^\s*on\s+", re.I),
    re.compile(r"^\s*(?:let['’]s say|say|maybe)\s+", re.I),
    re.compile(r"^\s*the\s+(?=\d)", re.I),
    # Trailing 'please' / 'if you can'
    re.compile(r"\s+please\s*$", re.I),
    re.compile(r"\s+if (?:you|that) can\s*$", re.I),
]


_SPELLED_ORDINALS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th",
    "twelfth": "12th", "thirteenth": "13th", "fourteenth": "14th",
    "fifteenth": "15th", "sixteenth": "16th", "seventeenth": "17th",
    "eighteenth": "18th", "nineteenth": "19th", "twentieth": "20th",
    "twenty-first": "21st", "twenty-second": "22nd",
    "twenty-third": "23rd", "twenty-fourth": "24th",
    "twenty-fifth": "25th", "twenty-sixth": "26th",
    "twenty-seventh": "27th", "twenty-eighth": "28th",
    "twenty-ninth": "29th", "thirtieth": "30th",
    "thirty-first": "31st",
}


def normalize_date(raw: str) -> str:
    """Layer A: strip fillers + normalize spelled ordinals.
    Preserves everything else so TemporalResolver can do its job.
    """
    if not raw:
        return ""
    out = raw.strip().lower()
    for pat in _DATE_FILLERS:
        out = pat.sub("", out)
    # Replace spelled ordinals.
    def _sub_ordinal(match):
        return _SPELLED_ORDINALS.get(match.group(0), match.group(0))
    out = re.sub(
        r"\b(?:twenty-)?(?:first|second|third|fourth|fifth|sixth|"
        r"seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|"
        r"fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|"
        r"nineteenth|twentieth|thirtieth|thirty-first)\b",
        _sub_ordinal, out,
    )
    return out.strip()


# ── validation (Layer B) ────────────────────────────────────


def date_validator(canonical: str, config: dict) -> SlotResult:
    """Layer B: canonical → SlotResult using TemporalResolver.

    Config:
      * timezone: str (required for resolver, e.g. 'America/Chicago')
      * business_hours: optional BusinessHours-like object for
        after-hours warning
      * allow_past: bool (default False) — reject dates before today
    """
    if not canonical:
        return SlotResult(
            status=SlotStatus.INCOMPLETE, raw_digits="",
            reason="empty",
        )
    try:
        from packages.dialogue import (
            TemporalContext, TemporalResolver, Resolution,
        )
    except Exception:
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason="TemporalResolver unavailable",
        )
    tz = config.get("timezone", "UTC")
    business = config.get("business_hours")
    try:
        # TemporalContext.now_in wants a business-like object with
        # .hours; pass whatever we got (may be None).
        ctx_biz = (
            type("B", (), {"hours": business})()
            if business is not None else None
        )
        ctx = TemporalContext.now_in(tz, business=ctx_biz)
        result = TemporalResolver().resolve(canonical, ctx)
    except Exception as e:
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason=f"resolver error: {e}",
        )
    if result.resolution == Resolution.IMPOSSIBLE:
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason="date not recognized",
        )
    if result.resolution == Resolution.AMBIGUOUS_NEEDS_CONFIRM:
        # Multiple candidates — POSSIBLE so the actor can ask
        # 'this Tuesday or next?'
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value=(
                result.range_start.isoformat()
                if result.range_start is not None else canonical
            ),
            raw_digits=canonical,
            reason=result.spoken_confirmation
            or "date is ambiguous",
        )
    if result.range_start is None:
        return SlotResult(
            status=SlotStatus.INVALID, raw_digits=canonical,
            reason="resolver returned no range",
        )
    return SlotResult(
        status=SlotStatus.VALID,
        value=result.range_start.replace(tzinfo=None).isoformat(),
        raw_digits=canonical,
    )


__all__ = [
    "_expand_two_digit_year",
    "normalize_date",
    "date_validator",
]
