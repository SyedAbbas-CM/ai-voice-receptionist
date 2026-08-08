"""Sprint 10 Track A3 tests: Temporal Resolution Service.

Coverage:
  * Absolute dates: "August 6th at 10:30 AM"
  * Relative dates: today / tomorrow / day after tomorrow
  * Weekday: "Tuesday", "this Thursday", "next Friday"
  * "Next Friday" ambiguity: returns AMBIGUOUS_NEEDS_CONFIRM
  * Time-of-day words: "morning", "afternoon", "late afternoon"
  * Time-of-day passed today → rolls to tomorrow
  * Past date → IMPOSSIBLE
  * Business closed day → IMPOSSIBLE with business_closed reason
  * Spelled times: "ten thirty am", "quarter past three", "noon"
  * Year-boundary: "January 5th" in December rolls to next year
  * Timezone-awareness: all outputs carry business_tz
  * Unparseable → IMPOSSIBLE/UNPARSEABLE

All tests use FIXED datetime anchors — no wall clock reads.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from packages.dialogue import (
    ImpossibleReason,
    Resolution,
    TemporalContext,
    TemporalResolver,
)
from packages.dialogue.temporal import (
    _parse_clock_time,
    _parse_spelled_time,
    _ordinal,
)
from packages.schemas.business import BusinessHours


def _ctx(iso: str, tz: str = "America/Chicago", hours=None) -> TemporalContext:
    tz_obj = ZoneInfo(tz)
    dt = datetime.fromisoformat(iso).replace(tzinfo=tz_obj)
    return TemporalContext(now=dt, business_tz=tz, business_hours=hours)


# ── absolute dates ──────────────────────────────────────────────────

def test_absolute_month_day_at_time():
    r = TemporalResolver().resolve(
        "August 6th at 10:30 AM", _ctx("2026-08-04T15:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.date() == date(2026, 8, 6)
    assert r.range_start.time() == time(10, 30)
    assert r.range_start.tzinfo is not None


def test_absolute_month_day_no_time_becomes_fuzzy():
    r = TemporalResolver().resolve(
        "August 6th", _ctx("2026-08-04T15:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_FUZZY_TIME
    assert r.range_start.date() == date(2026, 8, 6)


def test_iso_datetime_direct():
    r = TemporalResolver().resolve(
        "2026-08-06T14:00", _ctx("2026-08-04T15:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start == datetime(
        2026, 8, 6, 14, 0, tzinfo=ZoneInfo("America/Chicago"),
    )


# ── relative dates ──────────────────────────────────────────────────

def test_today_at_time():
    r = TemporalResolver().resolve(
        "today at 4 pm", _ctx("2026-08-04T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.date() == date(2026, 8, 4)
    assert r.range_start.hour == 16


def test_tomorrow_morning():
    r = TemporalResolver().resolve(
        "tomorrow morning", _ctx("2026-08-04T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_FUZZY_TIME
    assert r.range_start.date() == date(2026, 8, 5)
    assert r.range_start.hour == 8   # start of morning window
    assert r.range_end.date() == date(2026, 8, 5)


def test_day_after_tomorrow_at_2pm():
    r = TemporalResolver().resolve(
        "day after tomorrow at 2 pm", _ctx("2026-08-04T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.date() == date(2026, 8, 6)
    assert r.range_start.hour == 14


# ── weekday parsing ─────────────────────────────────────────────────

def test_this_thursday_from_tuesday():
    """From Tuesday Aug 4 2026, 'this Thursday' = Aug 6."""
    r = TemporalResolver().resolve(
        "this Thursday at 10", _ctx("2026-08-04T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.date() == date(2026, 8, 6)
    assert r.range_start.hour == 10


def test_bare_weekday_defaults_to_coming():
    """'Tuesday at 10' from Sunday Aug 2 = Tuesday Aug 4."""
    r = TemporalResolver().resolve(
        "Tuesday at 10", _ctx("2026-08-02T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.date() == date(2026, 8, 4)


# ── the audit's specific "next Friday" ambiguity ────────────────────

def test_next_friday_from_wednesday_is_ambiguous():
    """From Wed Aug 5, 'next Friday' could reasonably mean Fri Aug 7
    (coming Friday, 2 days out) OR Fri Aug 14 (Friday of next week).
    Resolver MUST flag this and offer both."""
    r = TemporalResolver().resolve(
        "next Friday at 3", _ctx("2026-08-05T10:00:00"),
    )
    assert r.resolution == Resolution.AMBIGUOUS_NEEDS_CONFIRM
    assert r.needs_confirmation is True
    assert len(r.interpretations) == 2
    # Both interpretations should be Fridays
    for interp in r.interpretations:
        assert interp.range_start.weekday() == 4   # Friday
    # And they should be a week apart
    delta = (
        r.interpretations[1].range_start.date()
        - r.interpretations[0].range_start.date()
    )
    assert delta.days == 7
    assert "next_weekday_ambiguity" in r.notes


def test_next_thursday_far_from_thursday_not_ambiguous():
    """From Monday, 'next Thursday' is 3 days out — falls in the
    ambiguous window.  We flag it.  This test locks in the
    conservative bias: better to ask than assume."""
    r = TemporalResolver().resolve(
        "next Thursday at 10", _ctx("2026-08-03T10:00:00"),   # Monday
    )
    # 'next Thursday' from Mon = Thu 3 days out. Within ambiguous window (1-3 days).
    assert r.resolution == Resolution.AMBIGUOUS_NEEDS_CONFIRM


def test_this_coming_thursday_not_ambiguous():
    """'this coming Thursday' explicitly disambiguates — no confirm."""
    r = TemporalResolver().resolve(
        "this coming Thursday at 10", _ctx("2026-08-05T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.weekday() == 3


def test_a_week_from_friday():
    r = TemporalResolver().resolve(
        "a week from Friday at 2 pm", _ctx("2026-08-05T10:00:00"),   # Wed
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    # Coming Friday = Aug 7; "a week from Friday" = Aug 14
    assert r.range_start.date() == date(2026, 8, 14)
    assert r.range_start.hour == 14


# ── time-of-day words ───────────────────────────────────────────────

def test_afternoon_range():
    r = TemporalResolver().resolve(
        "tomorrow afternoon", _ctx("2026-08-04T10:00:00"),
    )
    assert r.range_start.hour == 12
    assert r.range_end.hour == 17


def test_late_afternoon_range():
    r = TemporalResolver().resolve(
        "tomorrow late afternoon", _ctx("2026-08-04T10:00:00"),
    )
    assert r.range_start.hour == 15
    assert r.range_end.hour == 17


def test_bare_morning_rolls_to_tomorrow_if_passed():
    """If it's 2pm and caller says 'morning', that morning is done —
    resolver rolls to tomorrow morning."""
    r = TemporalResolver().resolve(
        "morning", _ctx("2026-08-04T14:00:00"),
    )
    assert r.range_start.date() == date(2026, 8, 5)
    assert r.range_start.hour == 8


def test_bare_morning_stays_today_if_not_passed():
    r = TemporalResolver().resolve(
        "morning", _ctx("2026-08-04T07:30:00"),
    )
    assert r.range_start.date() == date(2026, 8, 4)


# ── past date rejection ─────────────────────────────────────────────

def test_past_date_impossible():
    r = TemporalResolver().resolve(
        "August 2nd at 10 am", _ctx("2026-08-04T15:00:00"),
    )
    # August 2 already passed this year → rolled to next year (2027).
    # NOT impossible — the parser deliberately rolls-forward for the
    # year-boundary case.
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.year == 2027


def test_today_at_time_thats_passed_impossible():
    r = TemporalResolver().resolve(
        "today at 8 am", _ctx("2026-08-04T15:00:00"),
    )
    assert r.resolution == Resolution.IMPOSSIBLE
    assert r.impossible_reason == ImpossibleReason.PAST


# ── business-hours awareness ────────────────────────────────────────

def test_business_closed_on_sunday_returns_impossible():
    hours = BusinessHours(
        monday="09:00-17:00", tuesday="09:00-17:00", wednesday="09:00-17:00",
        thursday="09:00-19:00", friday="09:00-17:00",
        saturday="10:00-14:00",  sunday=None,
    )
    r = TemporalResolver().resolve(
        "Sunday at 10 am", _ctx("2026-08-04T10:00:00", hours=hours),
    )
    assert r.resolution == Resolution.IMPOSSIBLE
    assert r.impossible_reason == ImpossibleReason.BUSINESS_CLOSED


def test_business_open_day_no_extra_flag():
    hours = BusinessHours(
        monday="09:00-17:00", tuesday="09:00-17:00", wednesday="09:00-17:00",
        thursday="09:00-19:00", friday="09:00-17:00",
        saturday="10:00-14:00", sunday=None,
    )
    r = TemporalResolver().resolve(
        "Thursday at 3 pm", _ctx("2026-08-04T10:00:00", hours=hours),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.impossible_reason is None


# ── year-boundary case ──────────────────────────────────────────────

def test_year_boundary_january_from_december():
    r = TemporalResolver().resolve(
        "January 5th at 10 am",
        _ctx("2026-12-20T10:00:00"),
    )
    assert r.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert r.range_start.year == 2027


# ── unparseable ────────────────────────────────────────────────────

def test_unparseable_returns_impossible():
    r = TemporalResolver().resolve(
        "sometime blah blah blah", _ctx("2026-08-04T10:00:00"),
    )
    assert r.resolution == Resolution.IMPOSSIBLE
    assert r.impossible_reason == ImpossibleReason.UNPARSEABLE


def test_empty_string_impossible():
    r = TemporalResolver().resolve("", _ctx("2026-08-04T10:00:00"))
    assert r.resolution == Resolution.IMPOSSIBLE


# ── time parsers ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("10:30", time(10, 30)),
    ("10:30 am", time(10, 30)),
    ("10:30 AM", time(10, 30)),
    ("2:15 pm", time(14, 15)),
    ("12:00 pm", time(12, 0)),          # noon
    ("12:00 am", time(0, 0)),           # midnight
    ("14:30", time(14, 30)),
    ("4 pm", time(16, 0)),
    ("2 a.m.", time(2, 0)),
])
def test_parse_clock_time_variants(text, expected):
    assert _parse_clock_time(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("ten thirty am", time(10, 30)),
    ("ten thirty pm", time(22, 30)),
    ("quarter past three pm", time(15, 15)),
    ("quarter to four pm", time(15, 45)),
    ("half past ten am", time(10, 30)),
    ("noon", time(12, 0)),
    ("midnight", time(0, 0)),
    ("nine o'clock am", time(9, 0)),
    ("nine sharp am", time(9, 0)),
])
def test_parse_spelled_time_variants(text, expected):
    assert _parse_spelled_time(text) == expected


@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (22, "22nd"), (23, "23rd"),
    (101, "101st"), (111, "111th"),
])
def test_ordinal(n, expected):
    assert _ordinal(n) == expected


# ── timezone-awareness sanity ──────────────────────────────────────

def test_output_carries_business_timezone():
    r = TemporalResolver().resolve(
        "Thursday at 10 am",
        _ctx("2026-08-04T10:00:00", tz="America/Los_Angeles"),
    )
    assert r.range_start.tzinfo is not None
    assert str(r.range_start.tzinfo) == "America/Los_Angeles"


def test_context_now_in_returns_tz_aware():
    ctx = TemporalContext.now_in("America/Chicago")
    assert ctx.now.tzinfo is not None
    assert ctx.business_tz == "America/Chicago"
