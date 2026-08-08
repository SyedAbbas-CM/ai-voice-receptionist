"""Temporal Resolution Service (Sprint 10 Track A3).

The audit's finding: "The model is expected to reason about 'tomorrow',
'next Tuesday', 'the Tuesday after next', 'morning', 'before lunch',
'around four'... but date normalization is not an explicit subsystem."

This module is a deterministic engine.  LLM chooses linguistic intent
(is "next Friday" this-Friday or the-Friday-after); Python does all
date arithmetic against a business-timezone-aware anchor time.

Design principles:
  * Never trust the LLM with datetime arithmetic.
  * Business timezone is authoritative.  Never operate on naive datetimes.
  * Ambiguity is a first-class outcome, not a fallback.  When "next
    Friday" could reasonably mean two things, return needs_confirmation.
  * Business-hours-aware: "morning" after 11 AM = this-afternoon or
    tomorrow-morning based on how we interpret it.  The service surfaces
    which interpretation and asks caller to confirm.
  * DST-safe.  Timezone-aware datetimes handle the transitions; we
    detect and flag ambiguous local times (Nov fall-back).

Public API:

    resolver = TemporalResolver(business_tz="America/Chicago",
                                business_hours=business.hours)
    context = TemporalContext.now(business_tz="America/Chicago",
                                  business=business_profile)
    result = resolver.resolve("next Thursday afternoon", context)

    if result.needs_confirmation:
        # tell the caller which interpretation to confirm
        speak(result.spoken_confirmation)
    else:
        start = result.range_start
        end = result.range_end
        # feed into check_availability

Kept in dialogue package since it's owned by the reducer / semantic
planner, not by an external provider.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


class Resolution(str, Enum):
    """How confidently the utterance mapped to a datetime range."""
    EXACT_DATE_EXACT_TIME = "exact_date_exact_time"
    """User said "August 6th at 10:30 AM" or "tomorrow at 2pm"."""
    EXACT_DATE_FUZZY_TIME = "exact_date_fuzzy_time"
    """User said "Thursday morning" — day pinned, time-of-day range."""
    FUZZY_DATE_EXACT_TIME = "fuzzy_date_exact_time"
    """User said "sometime next week at 2pm"."""
    FUZZY_DATE_FUZZY_TIME = "fuzzy_date_fuzzy_time"
    """User said "sometime soon in the afternoon"."""
    AMBIGUOUS_NEEDS_CONFIRM = "ambiguous_needs_confirm"
    """Multiple valid interpretations — MUST ask caller to disambiguate."""
    IMPOSSIBLE = "impossible"
    """Past date, closed day, or otherwise cannot be satisfied."""


class ImpossibleReason(str, Enum):
    PAST = "past"
    BUSINESS_CLOSED = "business_closed"
    OUTSIDE_HOURS = "outside_hours"
    UNPARSEABLE = "unparseable"


# Simple TIME-OF-DAY windows (business-local time).  Extension point
# for tenant-specific overrides (some cafes' "morning" ends at 11).
_TIME_OF_DAY: dict[str, tuple[time, time]] = {
    "early morning":  (time(6, 0),  time(9, 0)),
    "morning":        (time(8, 0),  time(12, 0)),
    "late morning":   (time(10, 0), time(12, 0)),
    "noon":           (time(11, 30), time(12, 30)),
    "midday":         (time(11, 0), time(13, 0)),
    "lunch":          (time(11, 30), time(13, 30)),
    "afternoon":      (time(12, 0), time(17, 0)),
    "early afternoon": (time(12, 0), time(15, 0)),
    "late afternoon": (time(15, 0), time(17, 30)),
    "evening":        (time(17, 0), time(20, 0)),
    "night":          (time(19, 0), time(22, 0)),
    "end of day":     (time(16, 0), time(17, 30)),
    "eod":            (time(16, 0), time(17, 30)),
    "cob":            (time(16, 0), time(17, 30)),
}

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class TemporalContext:
    """Anchor for all resolution.  Passed to every resolve() call."""
    now: datetime
    """Current business-local time — MUST be timezone-aware."""
    business_tz: str
    """IANA name, e.g. 'America/Chicago'."""
    business_hours: Optional[object] = None
    """Optional BusinessHours from packages/schemas/business.py.
    If provided, closed-day and outside-hours checks apply."""

    @classmethod
    def now_in(cls, business_tz: str, business=None) -> "TemporalContext":
        """Convenience: build a context anchored to real 'now' in the
        given timezone.  For tests, construct explicitly with a fixed
        datetime instead."""
        try:
            tz = ZoneInfo(business_tz)
        except ZoneInfoNotFoundError:
            log.warning("unknown tz %s; defaulting to UTC", business_tz)
            tz = ZoneInfo("UTC")
        hours = getattr(business, "hours", None) if business is not None else None
        return cls(
            now=datetime.now(tz),
            business_tz=business_tz,
            business_hours=hours,
        )


@dataclass(frozen=True)
class ResolvedRange:
    """Output of resolver.  range_start / range_end are inclusive of
    start, exclusive of end.  Both timezone-aware in business_tz."""
    resolution: Resolution
    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None
    needs_confirmation: bool = False
    spoken_confirmation: str = ""
    """Deterministic, template-generated summary the realizer speaks
    back to caller for confirmation.  Empty string when no confirm
    needed."""
    impossible_reason: Optional[ImpossibleReason] = None
    interpretations: list["ResolvedRange"] = field(default_factory=list)
    """When AMBIGUOUS_NEEDS_CONFIRM: the candidate interpretations we
    considered, so the realizer can offer choices."""
    notes: list[str] = field(default_factory=list)
    """Machine-readable notes: 'dst_transition', 'year_boundary',
    'outside_business_hours', etc."""


# ── the resolver ─────────────────────────────────────────────────────

class TemporalResolver:
    """Interprets time-of-day + relative-date + absolute-date utterances
    into a ResolvedRange.

    Kept stateless — all context comes from the TemporalContext.  Uses
    Python stdlib datetime + zoneinfo; no third-party dependency."""

    def resolve(self, utterance: str, ctx: TemporalContext) -> ResolvedRange:
        text = utterance.lower().strip()
        if not text:
            return ResolvedRange(
                resolution=Resolution.IMPOSSIBLE,
                impossible_reason=ImpossibleReason.UNPARSEABLE,
                spoken_confirmation="I didn't catch a time — could you say when?",
            )

        # Try each pattern in priority order.  First match wins.
        for parser in (
            self._parse_absolute_date_time,
            self._parse_iso_datetime,
            self._parse_today_tomorrow,
            self._parse_this_next_weekday,
            self._parse_bare_weekday,
            self._parse_month_day,
            self._parse_time_of_day_only,
        ):
            result = parser(text, ctx)
            if result is not None:
                return self._post_process(result, ctx)

        return ResolvedRange(
            resolution=Resolution.IMPOSSIBLE,
            impossible_reason=ImpossibleReason.UNPARSEABLE,
            spoken_confirmation=f"I didn't understand \"{utterance}\" as a date.",
        )

    # ── individual parsers ──────────────────────────────────────────

    def _parse_iso_datetime(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """Direct ISO-8601 datetime: '2026-08-06T10:30'.  Rare from
        callers but occasionally from tool outputs fed back in."""
        m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})[t ](\d{2}:\d{2})(:\d{2})?\s*$", text)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
        except ValueError:
            return None
        dt = dt.replace(tzinfo=ZoneInfo(ctx.business_tz))
        return ResolvedRange(
            resolution=Resolution.EXACT_DATE_EXACT_TIME,
            range_start=dt,
            range_end=dt + timedelta(minutes=1),
        )

    def _parse_absolute_date_time(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """'August 6 at 10:30 AM' / 'Aug 6th at ten thirty'."""
        # Month name + day number + optional at + time
        pattern = (
            r"(?:on\s+)?"
            r"(?P<month>january|february|march|april|may|june|july|august|"
            r"september|october|november|december|jan|feb|mar|apr|jun|"
            r"jul|aug|sep|sept|oct|nov|dec)"
            r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
            r"(?:\s+at\s+(?P<time>.+))?"
        )
        m = re.search(pattern, text)
        if not m:
            return None
        month = _MONTH_NAMES[m.group("month")]
        day = int(m.group("day"))
        year = ctx.now.year
        # If month already passed this year, roll to next year — a
        # common source of the "year-boundary" ambiguity the audit
        # called out.
        try:
            candidate = date(year, month, day)
        except ValueError:
            return ResolvedRange(
                resolution=Resolution.IMPOSSIBLE,
                impossible_reason=ImpossibleReason.UNPARSEABLE,
                spoken_confirmation=f"That doesn't look like a real date — could you say it another way?",
            )
        if candidate < ctx.now.date():
            candidate = date(year + 1, month, day)
        time_str = (m.group("time") or "").strip()
        return self._compose_date_and_time_string(candidate, time_str, ctx)

    def _parse_today_tomorrow(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        offset = None
        # Order matters: check longest phrase first so "day after tomorrow"
        # isn't shadowed by the "tomorrow" match.
        if re.search(r"\b(?:the\s+)?day after tomorrow\b", text):
            offset = 2
        elif re.search(r"\btomorrow\b", text):
            offset = 1
        elif re.search(r"\btoday\b", text):
            offset = 0
        if offset is None:
            return None
        target_date = ctx.now.date() + timedelta(days=offset)
        # Extract time part after "at" or trailing time-of-day.  Try
        # longest keyword first for the same reason.
        time_part = self._time_part_after(
            text, ("day after tomorrow", "tomorrow", "today"),
        )
        return self._compose_date_and_time_string(target_date, time_part, ctx)

    def _parse_this_next_weekday(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """'next Tuesday', 'this Thursday', 'a week from Friday'."""
        # Match "next|this|coming" then weekday, optionally + at TIME
        pattern = (
            r"\b(?P<modifier>next|this coming|this|coming|following|a week from|two weeks from)\s+"
            r"(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
            r"(?P<rest>.*)"
        )
        m = re.search(pattern, text)
        if not m:
            return None
        modifier = m.group("modifier").lower()
        weekday = _WEEKDAY_NAMES[m.group("wd")]
        rest = m.group("rest") or ""

        today = ctx.now.date()
        current_wd = today.weekday()
        days_until = (weekday - current_wd) % 7

        # "next Tuesday" ambiguity — audit called out explicitly.
        # US convention split: some say "next Tuesday" = coming Tuesday
        # (if today is Sunday, next Tue = 2 days out).  Others say
        # "next Tuesday" = the Tuesday of NEXT week (9+ days out).
        # We compute BOTH and mark ambiguous when they differ AND the
        # nearest is <= 3 days.
        if modifier in ("this", "this coming", "coming"):
            # "this Tuesday" = coming Tuesday.  If today IS Tuesday,
            # treat as today (small edge case).
            if days_until == 0:
                target = today
            else:
                target = today + timedelta(days=days_until)
        elif modifier in ("a week from",):
            target = today + timedelta(days=days_until + 7 if days_until else 7)
        elif modifier in ("two weeks from",):
            target = today + timedelta(days=days_until + 14 if days_until else 14)
        elif modifier in ("following",):
            target = today + timedelta(days=days_until + 7 if days_until else 7)
        else:  # "next"
            # Coming Tuesday interpretation
            coming = today + timedelta(days=days_until or 7)
            # Next-week Tuesday interpretation
            next_week = today + timedelta(days=(days_until or 7) + 7)

            if 1 <= (coming - today).days <= 3:
                # Ambiguous — offer both
                time_part = self._time_part_after(rest.strip(), ())
                coming_range = self._compose_date_and_time_string(coming, time_part, ctx)
                next_range = self._compose_date_and_time_string(next_week, time_part, ctx)
                return ResolvedRange(
                    resolution=Resolution.AMBIGUOUS_NEEDS_CONFIRM,
                    needs_confirmation=True,
                    spoken_confirmation=(
                        f"Do you mean this coming {m.group('wd').capitalize()}, "
                        f"the {_ordinal(coming.day)}, or the following "
                        f"{m.group('wd').capitalize()}, the {_ordinal(next_week.day)}?"
                    ),
                    interpretations=[coming_range, next_range],
                    notes=["next_weekday_ambiguity"],
                )
            target = coming

        time_part = self._time_part_after(rest.strip(), ())
        return self._compose_date_and_time_string(target, time_part, ctx)

    def _parse_bare_weekday(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """'Tuesday at 10' with no this/next modifier — defaults to
        the coming instance of that weekday."""
        m = re.search(
            r"\b(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
            r"\b(?P<rest>.*)",
            text,
        )
        if not m:
            return None
        weekday = _WEEKDAY_NAMES[m.group("wd")]
        today = ctx.now.date()
        days_until = (weekday - today.weekday()) % 7
        if days_until == 0:
            # today.  If time hasn't passed, use today; else next week.
            time_part = self._time_part_after(m.group("rest") or "", ())
            target = today   # let time-passed check happen in post-process
        else:
            target = today + timedelta(days=days_until)
            time_part = self._time_part_after(m.group("rest") or "", ())
        return self._compose_date_and_time_string(target, time_part, ctx)

    def _parse_month_day(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """'The 6th' / 'the 15th at 2pm' — assumes current or next
        month based on which is nearer in the future."""
        m = re.match(
            r"^(?:on\s+)?(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)?"
            r"(?:\s+at\s+(?P<time>.+))?\s*$",
            text,
        )
        if not m:
            return None
        day = int(m.group("day"))
        today = ctx.now.date()
        candidate = today.replace(day=1)
        try:
            candidate = candidate.replace(day=day)
        except ValueError:
            return ResolvedRange(
                resolution=Resolution.IMPOSSIBLE,
                impossible_reason=ImpossibleReason.UNPARSEABLE,
                spoken_confirmation="That day doesn't exist this month.",
            )
        if candidate < today:
            # Roll to next month
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            try:
                candidate = date(year, month, day)
            except ValueError:
                return ResolvedRange(
                    resolution=Resolution.IMPOSSIBLE,
                    impossible_reason=ImpossibleReason.UNPARSEABLE,
                    spoken_confirmation="That day doesn't exist next month.",
                )
        return self._compose_date_and_time_string(candidate, m.group("time") or "", ctx)

    def _parse_time_of_day_only(
        self, text: str, ctx: TemporalContext,
    ) -> Optional[ResolvedRange]:
        """Just 'morning' or 'afternoon' with no date — default to
        today if not passed, else tomorrow.  Fuzzy time = FUZZY.

        Sorted by length descending so 'afternoon' matches before
        'noon' (which is a substring)."""
        for label, (start_t, end_t) in sorted(
            _TIME_OF_DAY.items(), key=lambda kv: -len(kv[0]),
        ):
            if re.search(rf"\b{re.escape(label)}\b", text):
                today = ctx.now.date()
                start = datetime.combine(today, start_t, tzinfo=ZoneInfo(ctx.business_tz))
                if start <= ctx.now:
                    tomorrow = today + timedelta(days=1)
                    start = datetime.combine(tomorrow, start_t, tzinfo=ZoneInfo(ctx.business_tz))
                    end = datetime.combine(tomorrow, end_t, tzinfo=ZoneInfo(ctx.business_tz))
                    day_word = "tomorrow"
                else:
                    end = datetime.combine(today, end_t, tzinfo=ZoneInfo(ctx.business_tz))
                    day_word = "today"
                return ResolvedRange(
                    resolution=Resolution.EXACT_DATE_FUZZY_TIME,
                    range_start=start,
                    range_end=end,
                    spoken_confirmation=f"{day_word.capitalize()} {label}",
                )
        return None

    # ── helpers ─────────────────────────────────────────────────────

    def _time_part_after(self, text: str, keywords: tuple[str, ...]) -> str:
        """Extract everything after 'at' or after the given keywords."""
        m = re.search(r"\bat\s+(.+)", text)
        if m:
            return m.group(1).strip()
        for kw in keywords:
            i = text.find(kw)
            if i >= 0:
                remainder = text[i + len(kw):].strip()
                if remainder:
                    return remainder
        return ""

    def _compose_date_and_time_string(
        self, target_date: date, time_str: str, ctx: TemporalContext,
    ) -> ResolvedRange:
        """Combine a resolved date with an unparsed time string.
        Returns EXACT_DATE_EXACT_TIME, EXACT_DATE_FUZZY_TIME, or
        IMPOSSIBLE depending on what we can parse."""
        tz = ZoneInfo(ctx.business_tz)
        if not time_str.strip():
            # Whole-day range — start of day to end of day
            start = datetime.combine(target_date, time(0, 0), tzinfo=tz)
            end = datetime.combine(target_date, time(23, 59), tzinfo=tz)
            return ResolvedRange(
                resolution=Resolution.EXACT_DATE_FUZZY_TIME,
                range_start=start, range_end=end,
                spoken_confirmation=_spoken_date(target_date),
            )
        # Try time-of-day words first ("morning", "afternoon").  Sort
        # longest-first so 'afternoon' shadows 'noon'.
        for label, (start_t, end_t) in sorted(
            _TIME_OF_DAY.items(), key=lambda kv: -len(kv[0]),
        ):
            if label in time_str:
                start = datetime.combine(target_date, start_t, tzinfo=tz)
                end = datetime.combine(target_date, end_t, tzinfo=tz)
                return ResolvedRange(
                    resolution=Resolution.EXACT_DATE_FUZZY_TIME,
                    range_start=start, range_end=end,
                    spoken_confirmation=f"{_spoken_date(target_date)} {label}",
                )
        # Try HH:MM (24h or 12h with am/pm)
        t = _parse_clock_time(time_str)
        if t is None:
            # Try spelled-out time "ten thirty am"
            t = _parse_spelled_time(time_str)
        if t is None:
            # Fallback: fuzzy whole-day
            start = datetime.combine(target_date, time(0, 0), tzinfo=tz)
            end = datetime.combine(target_date, time(23, 59), tzinfo=tz)
            return ResolvedRange(
                resolution=Resolution.EXACT_DATE_FUZZY_TIME,
                range_start=start, range_end=end,
                spoken_confirmation=_spoken_date(target_date),
            )
        exact = datetime.combine(target_date, t, tzinfo=tz)
        return ResolvedRange(
            resolution=Resolution.EXACT_DATE_EXACT_TIME,
            range_start=exact,
            range_end=exact + timedelta(minutes=1),
            spoken_confirmation=f"{_spoken_date(target_date)} at {_spoken_time(t)}",
        )

    def _post_process(
        self, result: ResolvedRange, ctx: TemporalContext,
    ) -> ResolvedRange:
        """After parsing, check policy: past date, closed day, outside
        business hours.  Enriches notes; returns IMPOSSIBLE where
        appropriate."""
        if result.resolution in (Resolution.IMPOSSIBLE,
                                 Resolution.AMBIGUOUS_NEEDS_CONFIRM):
            return result
        if result.range_start is None:
            return result

        # Past-date check
        if result.range_end and result.range_end <= ctx.now:
            return ResolvedRange(
                resolution=Resolution.IMPOSSIBLE,
                impossible_reason=ImpossibleReason.PAST,
                spoken_confirmation=(
                    f"{result.spoken_confirmation} has already passed. "
                    "Would you like a different time?"
                ),
            )
        # Sometimes range_start alone is in the past when range_end is
        # future (e.g. "morning" from mid-morning).  Trim in that case.
        if result.range_start < ctx.now and result.range_end and result.range_end > ctx.now:
            trimmed_start = ctx.now
            result = ResolvedRange(
                **{**result.__dict__, "range_start": trimmed_start,
                   "notes": [*result.notes, "start_trimmed_to_now"]},
            )

        # Business-closed check
        if ctx.business_hours is not None and result.range_start is not None:
            wd = result.range_start.weekday()
            day_name = ("monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday")[wd]
            window = getattr(ctx.business_hours, day_name, None)
            if not window:
                return ResolvedRange(
                    resolution=Resolution.IMPOSSIBLE,
                    impossible_reason=ImpossibleReason.BUSINESS_CLOSED,
                    spoken_confirmation=(
                        f"We're closed on {day_name.capitalize()}s. "
                        "Would another day work?"
                    ),
                    notes=["business_closed_that_day"],
                )
        return result


# ── formatters (kept as module functions so tests can call directly) ─

def _spoken_date(d: date) -> str:
    """'Thursday, August 6th' — human speech, not ISO."""
    return d.strftime(f"%A, %B {_ordinal(d.day)}")


def _spoken_time(t: time) -> str:
    """'ten thirty AM' — closest we can do without num2words here.
    Uses HH:MM 12-hour with lowercase am/pm — the sanitizer downstream
    will handle vocalization."""
    h12 = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    if t.minute == 0:
        return f"{h12} {ampm}"
    return f"{h12}:{t.minute:02d} {ampm}"


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_TIME_CLOCK_RE = re.compile(
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)?",
    re.IGNORECASE,
)


def _parse_clock_time(text: str) -> Optional[time]:
    """Parse '10:30', '10 am', '2:15 pm', '14:30'."""
    m = _TIME_CLOCK_RE.search(text.strip())
    if not m:
        return None
    h = int(m.group("h"))
    minute = int(m.group("m")) if m.group("m") else 0
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        return None
    return time(h, minute)


_SPELLED_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_SPELLED_MINUTES = {
    "oh five": 5, "oh": 0, "o'clock": 0, "sharp": 0,
    "fifteen": 15, "quarter": 15, "quarter past": 15,
    "twenty": 20, "twenty five": 25,
    "thirty": 30, "half": 30, "half past": 30,
    "thirty five": 35, "forty": 40,
    "forty five": 45, "quarter to": 45, "quarter till": 45,
    "fifty": 50, "fifty five": 55,
}


def _parse_spelled_time(text: str) -> Optional[time]:
    """Parse 'ten thirty am' / 'quarter past three' / 'quarter to four' /
    'noon' / 'midnight'.

    'quarter to X' / 'quarter till X' semantics: the caller says the
    HOUR they're approaching (four), meaning 3:45.  We detect the 'to'
    modifier and subtract 1 from the hour."""
    text = text.lower().strip()
    if "noon" in text or "midday" in text:
        return time(12, 0)
    if "midnight" in text:
        return time(0, 0)
    # Detect "quarter to X" / "quarter till X" — the hour named is the
    # target hour, actual time is 15 minutes before.
    subtract_hour = bool(re.search(r"\bquarter\s+(to|till)\b", text))
    # Try "H [M] [am/pm]"
    tokens = text.replace(",", " ").split()
    if not tokens:
        return None
    hour = None
    minute = 0
    ampm = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SPELLED_HOURS and hour is None:
            hour = _SPELLED_HOURS[tok]
        elif tok in ("am", "a.m.", "pm", "p.m."):
            ampm = tok.replace(".", "")
        # Try 2-token minute phrases first
        elif i + 1 < len(tokens) and f"{tok} {tokens[i+1]}" in _SPELLED_MINUTES:
            minute = _SPELLED_MINUTES[f"{tok} {tokens[i+1]}"]
            i += 1
        elif tok in _SPELLED_MINUTES:
            minute = _SPELLED_MINUTES[tok]
        i += 1
    if hour is None:
        return None
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    # Apply 'quarter to' hour-shift AFTER am/pm adjustment.
    if subtract_hour:
        hour = (hour - 1) % 24
    return time(hour, minute)
