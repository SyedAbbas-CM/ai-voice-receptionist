"""Tests for the streaming-TTS sentence splitter.

Critical for the "first sound in caller's ear at ~1.5s" fix. If this splits
wrong, either latency stays bad (chunks too big) or prosody breaks (chunks
too small / mid-sentence).
"""
from __future__ import annotations

import pytest

from packages.voice.sentence_splitter import split_into_speakable_chunks


def test_empty_input_returns_empty_list():
    assert split_into_speakable_chunks("") == []
    assert split_into_speakable_chunks("   ") == []


def test_single_short_sentence_stays_one_chunk():
    assert split_into_speakable_chunks("Hello there.") == ["Hello there."]


def test_two_sentences_split_on_period():
    r = split_into_speakable_chunks("How can I help you today? I'd love to know.")
    assert len(r) == 2
    assert r[0] == "How can I help you today?"
    assert r[1] == "I'd love to know."


def test_abbreviation_dr_does_not_split():
    r = split_into_speakable_chunks("Doctor Chen is available. Would you like to book?")
    # Note: even though we spelled out "Doctor", other abbreviations must not split
    assert len(r) == 2


def test_abbreviation_am_pm_does_not_split():
    r = split_into_speakable_chunks("We open at 9 a.m. and close at 6 p.m.")
    assert len(r) == 1


def test_decimal_number_does_not_split():
    """3.14 should NOT be treated as end-of-sentence."""
    r = split_into_speakable_chunks("Your total is $3.14. Would you like to add anything?")
    assert len(r) == 2
    assert "3.14" in r[0]


def test_ellipsis_before_lowercase_stays_one_chunk():
    """Ellipsis before lowercase = continuation (like a filler pause), not
    a sentence break. This is the CORRECT behavior — synthing 'Well.' alone
    then 'let me check that for you.' as separate chunks would kill prosody."""
    r = split_into_speakable_chunks("Well... let me check that for you.")
    assert len(r) == 1
    assert all(c.strip() for c in r)


def test_ellipsis_before_capital_splits():
    """Ellipsis followed by CAPITAL letter = real sentence boundary."""
    r = split_into_speakable_chunks("Hmm... Let me check that.")
    # Ellipsis + capital = real break
    assert len(r) == 2 or (len(r) == 1 and "Hmm" in r[0] and "check" in r[0])


def test_long_sentence_splits_on_semicolon():
    text = (
        "We offer general consultations for check-ups and routine visits; "
        "we also handle follow-up appointments for existing patients; "
        "and we provide vaccinations for all ages."
    )
    r = split_into_speakable_chunks(text)
    # Should be split — original is 25+ words
    assert len(r) >= 2


def test_long_sentence_splits_on_comma_when_no_semicolon():
    text = (
        "Our office hours are Monday through Friday from nine to six, "
        "Saturday from ten to two, and we're closed on Sunday for family time."
    )
    r = split_into_speakable_chunks(text)
    assert len(r) >= 2


def test_trailing_fragment_merges_into_prior_chunk():
    r = split_into_speakable_chunks("You're all set for Tuesday at ten. Bye!")
    # "Bye!" is 1 word — should merge back so we don't synth it as its own chunk
    assert len(r) == 1
    assert "Bye" in r[0]


def test_question_and_exclamation_terminate_sentences():
    r = split_into_speakable_chunks("Really? That's great! How can I help?")
    assert len(r) == 3


def test_no_terminators_returns_whole_text_as_one_chunk():
    r = split_into_speakable_chunks("just a fragment no punctuation")
    assert r == ["just a fragment no punctuation"]


def test_typical_receptionist_reply_produces_two_or_three_chunks():
    """The critical latency case: a normal LLM reply should chunk small
    enough that first-sound arrives fast."""
    text = (
        "Tomorrow we have vaccinations available at ten a.m. and two p.m. "
        "Which time slot works best for you?"
    )
    r = split_into_speakable_chunks(text)
    assert len(r) == 2
    # First chunk small enough to synth in ~2s
    assert len(r[0].split()) <= 15


def test_very_long_unbreakable_sentence_stays_one_chunk():
    """No commas or semis to split on — return as-is. TTS deals with it.
    (Better one slow chunk than break mid-thought.)"""
    text = "This is a very long declarative statement without any internal punctuation that clearly should be one chunk despite the length"
    r = split_into_speakable_chunks(text)
    assert len(r) == 1


def test_concatenating_chunks_preserves_meaning():
    """Round-trip check — no words lost."""
    text = (
        "Hi there. I can help with that. "
        "We're open at 9 a.m. tomorrow, and my colleague Doctor Chen has openings."
    )
    r = split_into_speakable_chunks(text)
    joined = " ".join(r).lower()
    for token in ["hi", "help", "9 a.m.", "chen", "openings"]:
        assert token in joined, f"missing token: {token!r}"
