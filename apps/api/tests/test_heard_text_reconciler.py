"""Sprint 10 C3: heard-text reconciliation tests.

The audit-called-out moat: on interruption, brain's transcript must
reflect what the caller ACTUALLY heard, not the full planned reply.

Coverage:
  * split_into_sentences preserves offsets
  * split_into_playback_chunks emits sentence-granularity chunks
  * long sentences get sub-split at commas
  * chunks carry unique mark_ids + text_start/text_end
  * reconcile rewrites the ASSISTANT turn to heard_text_for()
  * reconcile no-op when nothing was heard (planning-only turn)
  * reconcile no-op when planned text == heard text
"""
from __future__ import annotations

import pytest

from packages.runtime import (
    PlaybackLedger,
    reconcile_transcript_on_interrupt,
    split_into_playback_chunks,
    split_into_sentences,
)


# ── sentence splitter ─────────────────────────────────────────────

def test_split_single_sentence():
    result = split_into_sentences("Hello there.")
    assert result == [("Hello there.", 0, 12)]


def test_split_three_sentences():
    text = "Hi. How are you? I'm fine."
    result = split_into_sentences(text)
    assert len(result) == 3
    assert result[0][0] == "Hi."
    assert result[1][0] == "How are you?"
    assert result[2][0] == "I'm fine."


def test_split_offsets_reference_original():
    text = "First. Second sentence."
    result = split_into_sentences(text)
    s2, start, end = result[1]
    assert text[start:end] == "Second sentence."


def test_split_empty_string():
    assert split_into_sentences("") == []


def test_split_no_terminator_treats_whole_as_one():
    result = split_into_sentences("no punctuation at end")
    assert len(result) == 1
    assert result[0][0] == "no punctuation at end"


# ── chunk builder ─────────────────────────────────────────────────

def test_chunks_per_sentence():
    text = "First one. Second one."
    chunks = split_into_playback_chunks(text, generation_id="gen-1")
    assert len(chunks) == 2
    assert chunks[0].text == "First one."
    assert chunks[1].text == "Second one."


def test_chunks_have_unique_mark_ids():
    text = "One. Two. Three."
    chunks = split_into_playback_chunks(text, generation_id="gen-1")
    ids = {c.mark_id for c in chunks}
    assert len(ids) == len(chunks)


def test_final_chunk_flagged():
    text = "First. Second."
    chunks = split_into_playback_chunks(text, generation_id="gen-1")
    assert chunks[-1].is_final is True
    assert not chunks[0].is_final


def test_final_chunk_text_end_anchored_to_full_text():
    text = "First. Second."
    chunks = split_into_playback_chunks(text, generation_id="gen-1")
    assert chunks[-1].text_end == len(text)


def test_long_sentence_split_at_commas():
    text = "This is a really long sentence with a lot of clauses, and it keeps going with more clauses, and yet more clauses, until finally it terminates."
    chunks = split_into_playback_chunks(
        text, generation_id="gen-1", max_chunk_chars=60,
    )
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= 100   # allow some slack for last piece


def test_empty_text_returns_no_chunks():
    assert split_into_playback_chunks("", generation_id="gen-1") == []


def test_chunk_text_start_end_sequential():
    text = "First. Second. Third."
    chunks = split_into_playback_chunks(text, generation_id="gen-1")
    for i in range(len(chunks) - 1):
        assert chunks[i].text_end <= chunks[i+1].text_start


# ── reconciliation ────────────────────────────────────────────────

class _FakeTurn:
    def __init__(self, role: str, text: str):
        self.role = role
        self.text = text

    def model_copy(self, update: dict):
        new = _FakeTurn(self.role, self.text)
        for k, v in update.items():
            setattr(new, k, v)
        return new


class _FakeState:
    def __init__(self, transcript):
        self.transcript = transcript


def _make_ledger_with_partial_hearing(gen: int, full: str,
                                      heard_boundary: int) -> PlaybackLedger:
    """Build a ledger where mark_acks land on the first chunk that
    would set heard_text_end == heard_boundary."""
    from packages.runtime.playback_ledger import AudioChunk
    led = PlaybackLedger()
    led.start_generation(gen, full)
    chunks = split_into_playback_chunks(full, generation_id=f"gen-{gen}")
    for c in chunks:
        led.queue_chunk(gen, c)
    # ACK chunks until we cross the requested boundary
    for c in chunks:
        led.mark_ack(gen, c.mark_id)
        if c.text_end >= heard_boundary:
            break
    return led


def test_reconcile_truncates_transcript_to_heard():
    full = "I have openings at nine. Ten thirty. And two fifteen."
    ledger = _make_ledger_with_partial_hearing(
        gen=1, full=full, heard_boundary=24,   # stops after "nine."
    )
    transcript = [
        _FakeTurn("USER", "book me"),
        _FakeTurn("ASSISTANT", full),
    ]
    state = _FakeState(transcript)
    new = reconcile_transcript_on_interrupt(state, ledger, generation=1)
    assert new is not None
    assert transcript[-1].text == new
    # New text is a proper prefix of the full
    assert full.startswith(new)
    # And shorter
    assert len(new) < len(full)


def test_reconcile_noop_when_nothing_heard():
    """No mark ACKs → heard boundary is 0.  Reconcile rewrites the
    assistant turn to empty string so brain has no phantom claim."""
    full = "I have three openings for you."
    ledger = PlaybackLedger()
    ledger.start_generation(1, full)
    # Never ack any marks
    transcript = [
        _FakeTurn("USER", "book me"),
        _FakeTurn("ASSISTANT", full),
    ]
    state = _FakeState(transcript)
    new = reconcile_transcript_on_interrupt(state, ledger, generation=1)
    assert new == ""
    assert transcript[-1].text == ""


def test_reconcile_noop_when_planned_equals_heard():
    """Full-hearing case: no interruption → nothing to reconcile."""
    full = "OK, one moment."
    ledger = _make_ledger_with_partial_hearing(
        gen=1, full=full, heard_boundary=len(full),
    )
    transcript = [_FakeTurn("ASSISTANT", full)]
    state = _FakeState(transcript)
    new = reconcile_transcript_on_interrupt(state, ledger, generation=1)
    # Either None (equal) or equal — both acceptable no-op semantics
    assert new is None or new == full


def test_reconcile_no_assistant_turn_returns_none():
    ledger = PlaybackLedger()
    ledger.start_generation(1, "hi")
    state = _FakeState([_FakeTurn("USER", "hi")])
    new = reconcile_transcript_on_interrupt(state, ledger, generation=1)
    assert new is None


def test_reconcile_only_touches_most_recent_assistant_turn():
    full_earlier = "Earlier reply that was fully heard."
    # Multi-sentence so partial hearing crosses a chunk boundary
    full_current = "One. Two. Three. Four. Five."
    ledger = _make_ledger_with_partial_hearing(
        gen=2, full=full_current, heard_boundary=10,
    )
    transcript = [
        _FakeTurn("ASSISTANT", full_earlier),
        _FakeTurn("USER", "..."),
        _FakeTurn("ASSISTANT", full_current),
    ]
    state = _FakeState(transcript)
    reconcile_transcript_on_interrupt(state, ledger, generation=2)
    # Earlier ASSISTANT turn untouched
    assert transcript[0].text == full_earlier
    # Most recent one truncated
    assert transcript[2].text != full_current
    assert full_current.startswith(transcript[2].text)
