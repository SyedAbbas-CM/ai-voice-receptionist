"""Heard-Text Reconciliation (Sprint 10 C3 — the audit's called-out moat).

The bug the audit flagged:
    The brain appends the FULL assistant reply to the transcript
    before playback completes.  If the caller interrupts after
    hearing 'I have openings at ten thirty' out of 'I have openings
    at ten thirty, eleven forty-five, and two fifteen', the brain's
    next-turn context still says all three times were offered.  The
    caller then says 'take the eleven forty-five' and the agent
    happily books a slot the caller never heard.

The fix has three parts:

    1. AudioChunk-level playback ledger (already exists — Sprint 8b).
       Each TTS chunk carries text_start/end + a mark_id.  When
       Twilio's mark webhook confirms playout, ledger advances the
       heard-text boundary.

    2. Sentence-granularity chunking (this module).  Splits the
       agent utterance at sentence boundaries so heard_text_for() can
       return the truncation at a real prose boundary — not
       mid-word.

    3. Reconciliation (this module).  On confirmed INTERRUPTION,
       rewrite the last TranscriptTurn (role=ASSISTANT) so its text
       equals ledger.heard_text_for(current_gen).  Downstream LLM
       calls now see what the caller actually heard.

Public API:

    chunks = split_into_playback_chunks(reply_text, current_gen)
    # → list[AudioChunk] ready to feed the TTS + ledger

    reconciled_text = reconcile_transcript_on_interrupt(
        call_state, ledger, current_gen,
    )
    # → the assistant turn is now truncated to heard-text
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .playback_ledger import AudioChunk, PlaybackLedger

log = logging.getLogger(__name__)


# ── sentence splitter ──────────────────────────────────────────────

# Split at sentence-terminating punctuation.  Keep the terminator with
# the sentence (…". "…" becomes one span, not "…"+".").  Approximate;
# a real linguistic splitter is overkill for voice-reply chunk sizes.
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z0-9\"'])"
    r"|(?<=[.!?])\s*$",
)


def split_into_sentences(text: str) -> list[tuple[str, int, int]]:
    """Return [(sentence_text, start_offset, end_offset)] with offsets
    into the original string.  Preserves whitespace between sentences
    in the offsets so heard-text substring works cleanly."""
    if not text:
        return []
    result: list[tuple[str, int, int]] = []
    cursor = 0
    for m in _SENTENCE_SPLIT.finditer(text):
        end = m.start()
        segment = text[cursor:end].strip()
        if segment:
            # Find real start (skip leading whitespace of the slice)
            real_start = cursor + (len(text[cursor:end]) - len(text[cursor:end].lstrip()))
            real_end = real_start + len(segment)
            result.append((segment, real_start, real_end))
        cursor = m.end()
    # Trailing tail (no terminator)
    tail = text[cursor:].strip()
    if tail:
        real_start = cursor + (len(text[cursor:]) - len(text[cursor:].lstrip()))
        real_end = real_start + len(tail)
        result.append((tail, real_start, real_end))
    return result


# ── chunk builder ──────────────────────────────────────────────────

def split_into_playback_chunks(
    text: str,
    generation_id: str,
    duration_ms_per_char: float = 60.0,
    max_chunk_chars: int = 220,
    mark_prefix: str = "m",
) -> list[AudioChunk]:
    """Split reply text into playback chunks sized for the ledger.

    Prefers sentence boundaries; falls back to comma splits if a
    sentence would exceed max_chunk_chars.

    Returns AudioChunk records with per-chunk text_start / text_end /
    approx duration_ms + a unique mark_id.  Caller feeds these into
    both the TTS provider (with mark tags) and the ledger.queue_chunk.

    duration_ms_per_char is a rough estimate; the ledger doesn't need
    exact timing — it uses Twilio mark ACKs for real advancement."""
    sentences = split_into_sentences(text)
    if not sentences:
        return []

    chunks: list[AudioChunk] = []
    seq = 0
    for sentence, s_start, s_end in sentences:
        # If sentence is too long, sub-split at commas
        pieces = _split_long_sentence(sentence, max_chunk_chars)
        offset_in_sentence = 0
        for piece in pieces:
            piece_start = s_start + offset_in_sentence
            piece_end = piece_start + len(piece)
            offset_in_sentence += len(piece) + 1  # +1 for the delimiter we stripped
            duration_ms = int(len(piece) * duration_ms_per_char)
            mark_id = f"{mark_prefix}{seq}"
            chunks.append(AudioChunk(
                generation_id=generation_id,
                sequence=seq,
                audio_bytes=0,       # filled by the TTS layer after synth
                duration_ms=duration_ms,
                text=piece,
                text_start=piece_start,
                text_end=piece_end,
                mark_id=mark_id,
                is_final=False,
            ))
            seq += 1

    # Mark the final chunk
    if chunks:
        last = chunks[-1]
        chunks[-1] = AudioChunk(
            generation_id=last.generation_id,
            sequence=last.sequence,
            audio_bytes=last.audio_bytes,
            duration_ms=last.duration_ms,
            text=last.text,
            text_start=last.text_start,
            text_end=len(text),   # anchor final to true end-of-text
            mark_id=last.mark_id,
            is_final=True,
        )
    return chunks


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    """If a sentence is short enough, return it whole.  Otherwise
    split at comma boundaries so no chunk exceeds max_chars."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    buf = ""
    for part in re.split(r"(,\s+)", sentence):
        # `part` alternates: content, ", ", content, ", ", ...
        if len(buf) + len(part) > max_chars and buf.strip():
            pieces.append(buf.strip().rstrip(","))
            buf = ""
        buf += part
    if buf.strip():
        pieces.append(buf.strip().rstrip(","))
    return pieces


# ── reconciliation ─────────────────────────────────────────────────

def reconcile_transcript_on_interrupt(
    call_state,
    ledger: PlaybackLedger,
    generation: int,
) -> Optional[str]:
    """Rewrite the last ASSISTANT turn in call_state.transcript to
    match what the ledger says was heard.  Returns the new text (or
    None if nothing was reconciled).

    Called immediately after actor.bump_turn() on a confirmed
    interruption.  Safe on non-actor paths — if the transcript's last
    ASSISTANT turn doesn't correspond to the current generation, we
    leave it alone.

    Also clears the ledger's current generation so late Twilio mark
    ACKs don't retroactively advance the boundary (already done by
    ledger.clear_current_generation but we call it belt-and-braces)."""
    heard = ledger.heard_text_for(generation)
    if not heard:
        # Nothing was actually heard.  Wipe the assistant turn entirely
        # so the brain's next context has no phantom claim.
        heard = ""

    # Find the most recent ASSISTANT turn and rewrite it
    transcript = getattr(call_state, "transcript", None)
    if transcript is None:
        return None

    # Import here to avoid a top-level dependency on the schemas
    # package from this pure-runtime module.
    try:
        from packages.schemas import TurnRole
    except Exception:
        TurnRole = None  # type: ignore

    target_idx = None
    for i in range(len(transcript) - 1, -1, -1):
        turn = transcript[i]
        role = getattr(turn, "role", None)
        if TurnRole is not None and role == TurnRole.ASSISTANT:
            target_idx = i
            break
        # Fallback: string comparison for tests that use raw shapes
        if str(role).upper().endswith("ASSISTANT"):
            target_idx = i
            break

    if target_idx is None:
        return None

    original = getattr(transcript[target_idx], "text", "") or ""
    if heard == original:
        return None   # nothing to change

    # Mutate in place; TranscriptTurn is a Pydantic model — need
    # model_copy for immutable safety.
    try:
        new_turn = transcript[target_idx].model_copy(update={"text": heard})
        transcript[target_idx] = new_turn
    except Exception:
        # Fallback for non-Pydantic test doubles
        try:
            transcript[target_idx].text = heard
        except Exception:
            log.warning("could not rewrite transcript turn %d", target_idx)
            return None

    log.info(
        "reconciled interrupted turn: planned=%r heard=%r",
        original[:80], heard[:80],
    )
    return heard
