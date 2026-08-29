"""Tests for missing_slots — the fix for the ASK_SLOT-never-fires bug.

Live-diagnosed 2026-08-29 from CallSid CA3dac680ae8661459bc74735603f2cbc9.
Every policy_decision returned action='answer' because state.missing was
always []. Root cause: brain didn't compute missing_slots and never
passed it to build_decision_state_with_signals.

These tests lock the fix: booking-intent detection + ordered slot
computation + defensive against arbitrary state shapes.
"""
from __future__ import annotations

import pytest

from packages.core_agent.missing_slots import (
    _has_booking_intent,
    compute_missing_slots,
    recent_caller_texts_from_state,
)


# ── booking-intent detection ──────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "I want to book an appointment",
    "Can I schedule a cleaning?",
    "I need a filling",
    "an exam please",
    "I'd like a check-up",
    "a follow-up",
    "book me in",
    "get me an appointment",
    "any availability tomorrow?",
    "can I come in for a consultation",
    "next week works for me",
    "this Thursday morning",
    "reservation for two",
    "table for four",
    "property viewing please",
])
def test_booking_intent_positive(phrase):
    assert _has_booking_intent(phrase), (
        f"{phrase!r} should trigger booking-intent"
    )


@pytest.mark.parametrize("phrase", [
    "hi",
    "what are your hours?",
    "do you take Delta Dental?",
    "where are you located?",
    "is Dr. Chen there today?",
    "just calling to say thanks",
    "",
    "   ",
])
def test_booking_intent_negative(phrase):
    assert not _has_booking_intent(phrase), (
        f"{phrase!r} should NOT trigger booking-intent"
    )


# ── compute_missing_slots ─────────────────────────────────────────


def test_no_intent_returns_empty_missing():
    """Non-booking turn → empty missing → policy falls to ANSWER."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["what are your hours?"],
    )
    assert r == []


def test_booking_intent_no_known_slots_returns_full_list_in_order():
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["I want to book an appointment"],
    )
    # Full booking-required list in natural order — service first,
    # phone LAST (per LK sub-agent + human receptionist pattern).
    assert r == [
        "service", "date", "time", "caller_name", "phone",
    ]


def test_booking_intent_service_known_advances_to_date():
    r = compute_missing_slots(
        known_slots={"service": "Adult cleaning"},
        recent_caller_texts=["I want a cleaning"],
    )
    assert r[0] == "date"
    assert "service" not in r


def test_booking_intent_all_known_returns_empty():
    """Everything captured — policy should NOT fire ASK_SLOT (it should
    fire CONFIRM_ACTION or similar)."""
    r = compute_missing_slots(
        known_slots={
            "service": "Adult cleaning", "date": "2026-09-01",
            "time": "10:00", "caller_name": "Abbas",
            "phone": "+15551234567",
        },
        recent_caller_texts=["I want to book"],
    )
    assert r == []


def test_phone_asked_last():
    """Even when phone is the only missing slot, it comes last if other
    slots are also missing.  When it's alone at the end, it's simply
    the only entry."""
    r = compute_missing_slots(
        known_slots={
            "service": "Adult cleaning", "date": "2026-09-01",
            "time": "10:00", "caller_name": "Abbas",
        },
        recent_caller_texts=["I want a cleaning tomorrow"],
    )
    assert r == ["phone"]


def test_multiple_utterances_only_one_needs_intent():
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=[
            "what's your address?",
            "who's the dentist?",
            "book me in for tomorrow",
        ],
    )
    assert r  # non-empty — booking intent detected in one of them


def test_intent_signal_override_true():
    """Explicit True forces booking-intent even without keywords."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["yeah"],   # no keywords
        booking_intent_signal=True,
    )
    assert r  # non-empty


def test_intent_signal_override_false():
    """Explicit False forces no-intent even with keywords."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["I want to book"],
        booking_intent_signal=False,
    )
    assert r == []


# ── defensive shape handling ─────────────────────────────────


def test_empty_utterances_returns_empty():
    r = compute_missing_slots(
        known_slots={}, recent_caller_texts=[],
    )
    assert r == []


def test_none_known_slots_ok():
    r = compute_missing_slots(
        known_slots=None,   # type: ignore[arg-type]
        recent_caller_texts=["book"],
    )
    assert r  # falls back to empty dict internally, returns full list


def test_none_utterances_ok():
    r = compute_missing_slots(
        known_slots={}, recent_caller_texts=None,  # type: ignore[arg-type]
    )
    assert r == []


def test_garbage_utterances_never_raises():
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=[None, 42, {"x": 1}, "book"],  # type: ignore[list-item]
    )
    # Non-string entries silently skipped; "book" triggers intent.
    assert r


def test_known_slots_with_extra_keys_ok():
    """Unknown keys in known_slots are ignored, don't crash."""
    r = compute_missing_slots(
        known_slots={
            "service": "X", "insurance_carrier": "Delta",
            "birthday": "1990",
        },
        recent_caller_texts=["book"],
    )
    assert "service" not in r  # was known
    assert "date" in r  # still missing


# ── recent_caller_texts_from_state ──────────────────────────


def test_extract_from_transcript_shape():
    """State with `transcript` attribute holding TranscriptTurn-like
    objects."""
    class _Turn:
        def __init__(self, role, text):
            class _R:
                value = role
            self.role = _R()
            self.text = text

    class _State:
        transcript = [
            _Turn("assistant", "Hi how can I help?"),
            _Turn("user", "book me an appointment"),
            _Turn("assistant", "Sure, what day?"),
            _Turn("user", "Tomorrow"),
        ]

    r = recent_caller_texts_from_state(_State())
    # Newest first, only user turns.
    assert r[0] == "Tomorrow"
    assert "book me an appointment" in r


def test_extract_from_none_state():
    class _State:
        pass
    r = recent_caller_texts_from_state(_State())
    assert r == []


def test_extract_respects_limit():
    class _R:
        value = "user"

    class _Turn:
        def __init__(self, text):
            self.role = _R()
            self.text = text

    class _State:
        transcript = [_Turn(f"line {i}") for i in range(20)]

    r = recent_caller_texts_from_state(_State(), limit=3)
    assert len(r) == 3


# ── end-to-end integration signal ──────────────────────────


def test_christiaan_shape_produces_missing_service_or_phone():
    """The exact Christiaan trigger — 'I'd like to book a follow-up'.
    Should produce missing containing 'phone' (and probably 'service'
    since 'follow-up' as-a-service needs discovery, but the resolver
    layer handles that separately)."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["I'd like to book a follow-up please"],
    )
    assert "phone" in r
    assert "service" in r  # follow-up isn't a specific service yet


def test_abbas_shape_produces_missing():
    """Abbas said 'an exam, please, a follow-up?' on his test call.
    That's booking-intent — missing should be non-empty."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["an exam, please, a follow-up?"],
    )
    assert r
    assert "phone" in r
