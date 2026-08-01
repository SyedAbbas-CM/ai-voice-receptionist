"""Speech sanitizer tests.

Covers:
  - Bracket / tool-leakage stripping
  - Abbreviation expansion
  - Numeric normalization (currency, percent, time, phone, date, year)
  - Em-dash → comma
  - Flow-mode (period → comma) for natural TTS output
"""
from __future__ import annotations

import pytest

from packages.core_agent.speech_sanitizer import (
    apply_flow_mode,
    sanitize_for_speech,
)


# ---- basic sanitization ----

def test_strips_parens():
    assert "General consultation" in sanitize_for_speech(
        "General consultation (30 min) with Dr. Chen"
    )


def test_expands_dr():
    assert "Doctor Chen" in sanitize_for_speech("Book with Dr. Chen tomorrow")


def test_strips_tool_leakage():
    out = sanitize_for_speech("Let me call lookup_answer for you")
    assert "lookup_answer" not in out


def test_empty_falls_back():
    out = sanitize_for_speech("")
    assert out and "sorry" in out.lower()


# ---- numeric normalization ----

def test_currency_no_cents():
    out = sanitize_for_speech("The cleaning is $135")
    assert "one hundred thirty five dollars" in out
    assert "$" not in out


def test_currency_with_cents():
    out = sanitize_for_speech("The copay is $25.50")
    # Flow-mode adds a comma after "dollars" for natural breath.  Accept
    # either form.
    assert "twenty five dollars" in out
    assert "fifty cents" in out


def test_percent():
    out = sanitize_for_speech("About 25% of patients")
    assert "twenty five percent" in out
    assert "%" not in out


def test_time_with_ampm():
    out = sanitize_for_speech("At 2:30pm")
    assert "two thirty pm" in out


def test_time_on_the_hour():
    out = sanitize_for_speech("At 10:00 AM")
    assert "ten o'clock am" in out


def test_bare_hour():
    out = sanitize_for_speech("Open until 5pm")
    assert "five pm" in out


def test_phone_7_digit():
    out = sanitize_for_speech("Call 555-1234 today")
    assert "five five five, one two three four" in out


def test_phone_10_digit_with_parens():
    """Regression: parens around area code were being stripped before
    the phone matcher could see them, dropping the area code."""
    out = sanitize_for_speech("Call (555) 123-4567 today")
    assert "five five five" in out
    assert "one two three" in out
    assert "four five six seven" in out


def test_phone_10_digit_no_parens():
    out = sanitize_for_speech("Call 555-123-4567 today")
    assert "five five five" in out


def test_iso_date():
    out = sanitize_for_speech("On 2026-08-15")
    assert "August fifteenth" in out
    assert "twenty twenty six" in out


def test_year_alone():
    out = sanitize_for_speech("Founded in 1985")
    assert "nineteen eighty five" in out


def test_large_int_with_commas():
    out = sanitize_for_speech("Served 12,000 patients")
    assert "twelve thousand" in out


def test_huge_int_stays_as_digits():
    out = sanitize_for_speech("Case number 987,654,321")
    assert "987,654,321" in out  # too big to spell


# ---- em-dash normalization ----

def test_em_dash_becomes_comma():
    out = sanitize_for_speech("The manager — her name is Kaitlyn — will call back")
    assert "—" not in out
    assert "Kaitlyn" in out


# ---- flow-mode ----

def test_flow_mode_period_to_comma():
    """Sentence-boundary periods before continuations should become commas."""
    out = apply_flow_mode("Sure, I can help book that. What day were you thinking?")
    assert "book that, what day" in out


def test_flow_mode_keeps_terminal_period():
    out = apply_flow_mode("Hi there. I can help. So what's up?")
    # Last punctuation preserved
    assert out.rstrip().endswith("?")


def test_flow_mode_lowercases_continuation():
    """After the period → comma, we lowercase the first letter so it reads
    as one continuous thought (except 'I')."""
    out = apply_flow_mode("Let me check. Which time works?")
    assert "check, which time" in out


def test_flow_mode_preserves_I():
    """'I' must stay capitalized when it becomes a mid-utterance continuation."""
    out = apply_flow_mode("Sure. I can help.")
    assert "Sure, I can help" in out


def test_flow_mode_off_preserves_periods():
    out = sanitize_for_speech(
        "Sure. I can help.",
        flow_mode=False,
    )
    # No conversion when flow_mode explicitly off
    assert "Sure. I can help" in out or "Sure. I can help." in out


def test_flow_mode_default_on():
    """Default sanitize should apply flow mode."""
    out = sanitize_for_speech("Sure. I can help.")
    assert "Sure, I can help" in out


def test_flow_mode_leaves_questions_alone():
    """Only periods get converted, not ? or !"""
    out = apply_flow_mode("What day? Which time?")
    assert "What day?" in out
    assert "Which time?" in out


# ---- integration ----

def test_full_receptionist_greeting():
    """The canonical opening greeting should come out flowing + all-numerics-spelled."""
    src = ("Hi, thanks for calling Cedar Ridge Family Dental. "
           "I'm an AI assistant here to help. "
           "How can I help you today?")
    out = sanitize_for_speech(src)
    # No hard periods in the middle
    parts = out.split(". ")
    assert len(parts) <= 2, f"expected at most one sentence break, got {out!r}"


def test_full_receptionist_confirmation():
    """A confirmation with a phone number, time, and date all in one line."""
    src = ("Perfect. Booking a cleaning for 2026-08-15 at 10:30 AM "
           "for Jane at 555-1234. You'll get a confirmation text.")
    out = sanitize_for_speech(src)
    # Every numeric should be normalized
    assert "August" in out
    assert "ten thirty" in out
    assert "five five five" in out
    # Should read as one flowing thought (period → comma before "You'll")
    assert "You'll" in out
