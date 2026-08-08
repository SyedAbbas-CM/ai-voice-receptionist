"""Playback ledger — tracks generated / queued / heard audio separately.

Sprint 8c: fixes the audit finding that our current Twilio code treats
sent audio as spoken, which corrupts LLM history after an interruption.
If the agent starts saying "The address is 4592 Sengkang Way" and the
caller barges in at "4592", the LLM's next-turn context should contain
"The address is" — not the full sentence.

Three notions of speech:

  * GENERATED — audio exists inside a TTS worker (may or may not be sent)
  * QUEUED — audio has been sent to Twilio (may still be buffered)
  * HEARD — Twilio has ack'd a mark past this audio (caller definitely
    heard it, or the ack came before a `clear` command)

The ledger advances the HEARD boundary when Twilio's `mark` webhook
fires.  On a confirmed interruption, we send `clear` — subsequent mark
returns identify what was cleared vs completed.

LLM history + booking side-effects must consult HEARD, not GENERATED.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One piece of TTS output emitted for a single agent speech turn.

    text_start / text_end index into the full utterance text so we can
    compute the *heard* substring after an interruption.
    """
    generation_id: str        # speech_generation this chunk belongs to
    sequence: int             # per-generation sequence, monotonic
    audio_bytes: int          # size of the audio payload (bytes)
    duration_ms: int          # planned duration when played
    text: str                 # the exact text this chunk was generated from
    text_start: int           # index into the full utterance text
    text_end: int
    mark_id: Optional[str] = None   # Twilio mark name attached to this chunk
    is_final: bool = False


@dataclass
class _GenerationEntry:
    """One agent speech generation's ledger state."""
    generation_id: str
    full_text: str
    chunks: list[AudioChunk] = field(default_factory=list)
    heard_text_end: int = 0   # index in full_text of the last heard character
    cleared: bool = False


class PlaybackLedger:
    """Per-call playback state.

    Public flow:

        led = PlaybackLedger()

        # start a new speech generation for a new turn
        gen_id = led.start_generation(speech_generation=3, full_text="Hi, I have openings at nine, ten thirty, and two fifteen.")

        # for each TTS chunk sent to Twilio:
        led.queue_chunk(AudioChunk(...))

        # when Twilio's mark webhook fires with the mark name:
        led.mark_ack(mark_id="mark-3-2")

        # on caller interruption:
        led.clear_current_generation()

        # ask what the caller actually heard:
        heard = led.heard_text_for(speech_generation=3)
        # -> "Hi, I have openings at nine"  (assuming mark ack'd through that point)

    LLM must call heard_text_for() before appending the assistant turn
    to conversation history.
    """

    def __init__(self) -> None:
        # {speech_generation: _GenerationEntry}
        self._generations: dict[int, _GenerationEntry] = {}

    def start_generation(self, speech_generation: int, full_text: str) -> str:
        """Begin tracking a new agent speech turn.  Returns a generation_id
        the caller uses when tagging chunks."""
        gen_id = f"gen-{speech_generation}"
        self._generations[speech_generation] = _GenerationEntry(
            generation_id=gen_id, full_text=full_text,
        )
        return gen_id

    def queue_chunk(self, speech_generation: int, chunk: AudioChunk) -> None:
        """Register a chunk that's been sent to Twilio (queued but not
        necessarily heard yet)."""
        entry = self._generations.get(speech_generation)
        if entry is None:
            log.warning("queue_chunk for unknown generation %s", speech_generation)
            return
        entry.chunks.append(chunk)

    def mark_ack(self, speech_generation: int, mark_id: str) -> None:
        """Twilio confirms this mark reached the caller.  Advance the
        heard-text boundary to the end of that chunk.

        If the generation was cleared before this mark ack came back,
        this mark identifies audio that was actually CLEARED, not heard.
        In that case, do nothing.
        """
        entry = self._generations.get(speech_generation)
        if entry is None or entry.cleared:
            return
        for chunk in entry.chunks:
            if chunk.mark_id == mark_id:
                if chunk.text_end > entry.heard_text_end:
                    entry.heard_text_end = chunk.text_end
                return
        log.debug("mark %s not matched in generation %s", mark_id, speech_generation)

    def clear_current_generation(self, speech_generation: int) -> None:
        """Caller interrupted; freeze the heard boundary where it is."""
        entry = self._generations.get(speech_generation)
        if entry is None:
            return
        entry.cleared = True

    def heard_text_for(self, speech_generation: int) -> str:
        """Substring of the full utterance the caller actually heard.

        LLM history append MUST use this, not the full utterance the
        planner intended.  Fixes the "agent thinks it said the whole
        sentence but the caller cut it off" bug.
        """
        entry = self._generations.get(speech_generation)
        if entry is None:
            return ""
        return entry.full_text[: entry.heard_text_end]

    def is_generation_cleared(self, speech_generation: int) -> bool:
        entry = self._generations.get(speech_generation)
        return bool(entry and entry.cleared)

    def drop_generation(self, speech_generation: int) -> None:
        """Free memory once a generation is fully committed to LLM history."""
        self._generations.pop(speech_generation, None)
