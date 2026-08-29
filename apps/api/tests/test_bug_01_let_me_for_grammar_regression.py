"""BUG-01: 'Let me for a X' grammar bug regression tests.

2026-08-29: On the Roxana call the LLM said "Let me check availability
for a tooth extraction..." four times.  The speech_sanitizer's
`_TOOL_LEAK_PATTERNS` matched BOTH the snake_case tool identifier
"check_availability" AND the natural English phrase "check
availability" (the underscore/space alternation in the old regex
`\bcheck[_ ]availability\b`).

The sanitizer stripped "check availability" out of natural utterances,
leaving broken output like:
  "Let me check availability for a tooth extraction on Wednesday..."
  → "Let me for a tooth extraction on Wednesday..."

Fix: tighten the tool-leak regex so it strips ONLY:
  - snake_case identifiers ("check_availability", "book_appointment")
  - space-form names when they read as identifiers ("call the check
    availability tool", "invoke book appointment")
  - meta-narration ("based on the tool result")
Leave natural English verbs alone ("Let me check availability", "I'll
book an appointment for you").

These tests lock the correct behaviour so the sanitizer never
regresses.
"""
from __future__ import annotations

import pytest

from packages.core_agent.speech_sanitizer import sanitize_for_speech


# ── the exact Roxana trigger — natural English survives ────────────


def test_let_me_check_availability_survives():
    """The exact sentence the LLM produced on Roxana's call.  Must
    stay intact — 'check availability' is normal English for a
    receptionist to say."""
    out = sanitize_for_speech(
        "Let me check availability for a tooth extraction on "
        "Wednesday, September 13th."
    )
    assert "check availability" in out.lower()
    # The known-broken output must never appear.
    assert "let me for a" not in out.lower()


def test_let_me_check_availability_variants():
    variants = [
        "Let me check availability for that.",
        "Sure, let me check availability real quick.",
        "Alright, checking availability for you now.",
        "I'll check availability for Thursday afternoon.",
    ]
    for v in variants:
        out = sanitize_for_speech(v)
        assert "let me for" not in out.lower(), (
            f"BUG-01 regressed on: {v!r} → {out!r}"
        )
        assert "i'll for" not in out.lower(), (
            f"BUG-01 regressed on: {v!r} → {out!r}"
        )


def test_book_appointment_natural_survives():
    """'I'll book an appointment for you' is natural English —
    the old regex would strip 'book appointment' → 'I'll for you.'"""
    out = sanitize_for_speech(
        "I'll book an appointment for you tomorrow morning."
    )
    assert "book an appointment" in out.lower() or (
        "book appointment" in out.lower()
    )
    assert "i'll for" not in out.lower()


def test_escalate_natural_verb_survives():
    """'let me escalate to human' is natural.  Old regex would
    strip 'escalate to human' → 'let me.'"""
    out = sanitize_for_speech(
        "Let me escalate to a manager for that one."
    )
    assert "escalate to" in out.lower()
    assert "let me for" not in out.lower()


# ── real tool-name leaks still get stripped ─────────────────────


def test_snake_case_check_availability_stripped():
    """Real leak — LLM narrating its own tool call.  Should still
    be stripped."""
    out = sanitize_for_speech(
        "I'll call check_availability now for you."
    )
    assert "check_availability" not in out.lower()


def test_snake_case_book_appointment_stripped():
    out = sanitize_for_speech(
        "Calling book_appointment with the caller's details."
    )
    assert "book_appointment" not in out.lower()


def test_snake_case_escalate_stripped():
    out = sanitize_for_speech(
        "escalate_to_human triggered for that reason."
    )
    assert "escalate_to_human" not in out.lower()


def test_tool_identifier_narration_stripped():
    """'call the check availability tool' is the LLM narrating
    tool use — SHOULD be stripped."""
    out = sanitize_for_speech(
        "Let me call the check availability tool for you."
    )
    # The space-form phrase should not appear when it reads as a
    # tool identifier.
    assert "check availability tool" not in out.lower()


def test_invoke_book_appointment_stripped():
    out = sanitize_for_speech(
        "I'll invoke book appointment now."
    )
    assert "invoke book appointment" not in out.lower()


def test_based_on_tool_result_stripped():
    """Meta-narration still stripped."""
    out = sanitize_for_speech(
        "Based on the tool result, I have three openings."
    )
    assert "based on the tool result" not in out.lower()
    assert "based on tool result" not in out.lower()


# ── boundary cases — subtle regressions ────────────────────────


def test_check_this_for_you_survives():
    """Not a tool-name — just natural English."""
    out = sanitize_for_speech(
        "Let me check this for you real quick."
    )
    assert "check this for you" in out.lower()


def test_book_this_appointment_survives():
    out = sanitize_for_speech(
        "I'll book this appointment for Wednesday morning."
    )
    assert "book this appointment" in out.lower()
    assert "i'll for" not in out.lower()


def test_sentence_with_multiple_verbs_survives():
    """Multiple natural verbs — none should get eaten."""
    out = sanitize_for_speech(
        "Sure, I'll check availability and then book an appointment "
        "for you Thursday."
    )
    # Neither verb phrase should be silently dropped.
    assert "check availability" in out.lower()
    assert "book" in out.lower() and "appointment" in out.lower()


# ── the four documented Roxana failures ────────────────────────


@pytest.mark.parametrize("roxana_trigger", [
    "Let me check availability for a tooth extraction, what day works?",
    "Let me check availability for a tooth extraction on Wednesday, "
    "September 13th.",
    "Alright, let me check availability for a tooth extraction "
    "tomorrow afternoon.",
    "Okay, let me check availability for a tooth extraction between "
    "two and four.",
])
def test_roxana_call_utterances_survive(roxana_trigger):
    """Every literal utterance that the LLM produced during Roxana's
    call.  Each MUST survive sanitizer intact enough that the
    speech-gate + TTS see grammatical English."""
    out = sanitize_for_speech(roxana_trigger)
    # The broken shape must NEVER appear.
    assert "let me for a" not in out.lower()
    # The intent must be preserved — either the natural verb or a
    # nearby ack that a human could understand.
    assert "check" in out.lower() or "look" in out.lower(), (
        f"Sanitized speech dropped the verb entirely: {out!r}"
    )
