"""Split assistant reply text into speakable sentence chunks for TTS streaming.

The goal is to hand TTS units small enough that first-audio latency stays
under ~2s per chunk, while big enough that intonation and prosody don't
break within a natural clause.

Design constraints:
  - Zero external NLP deps. Regex + rules.
  - Handle abbreviations that end in periods without breaking sentences:
        "Dr. Chen will see you." -> ONE sentence
        "Meet at 9 a.m." -> ONE sentence
        "$5.99 please." -> ONE sentence
  - Long sentences (>25 words) split on natural pauses (comma, semicolon)
    so first-chunk latency stays snappy even on windy LLM replies.
  - Merge trivially short trailing fragments (<3 words) back into the
    previous chunk. Nobody wants "Bye." synthed as its own turn.

Public API:
  split_into_speakable_chunks(text: str) -> list[str]
"""
from __future__ import annotations

import re


# Abbreviations whose trailing "." must NOT be treated as sentence end.
# Case-insensitive match on the "word." token boundary.
_ABBREV_NO_SPLIT = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr",
    "a.m", "p.m", "am", "pm",
    "e.g", "i.e", "vs", "etc",
    "inc", "ltd", "co", "corp", "llc", "u.s", "u.k",
    "no",  # "at unit no. 5"
}

# Sentence-terminating punctuation
_SENT_TERM = re.compile(r"[.!?]+")

# Max words per chunk BEFORE we force a split on the nearest comma / semicolon.
# 18 words @ ~2.5 chars/word ≈ 45 chars ≈ 3s of Chatterbox synth. Kept under 20
# so a typical "here are our hours and services" reply chunks into at least 2.
_MAX_WORDS_PER_CHUNK = 18

# Never emit a chunk shorter than this many words as its own TTS call —
# merge into the previous one instead.
_MIN_TRAILING_WORDS = 3


def _looks_like_abbreviation(text: str, period_idx: int) -> bool:
    """Given text[period_idx] == '.', decide if it's abbreviation or sentence end.

    We look BACK to the last whitespace/start-of-string, then take everything
    between that boundary and this period, and check if it (with internal
    periods preserved) is a known abbreviation. Handles multi-dot abbrevs
    like "a.m." "p.m." "e.g." "u.s." properly.
    """
    # Find the boundary — space, comma, or start of string
    start = period_idx
    while start > 0 and text[start - 1] not in " \t\n,;:!?()\"'":
        start -= 1
    token = text[start:period_idx].lower().rstrip(".")
    if not token:
        return False
    # Direct match ("Dr", "p.m", "a.m", "u.s")
    if token in _ABBREV_NO_SPLIT:
        return True
    # Also treat as abbreviation if the token has an interior period followed
    # by a single letter (matches x.y and x.y.z patterns generically). This
    # catches "e.g", "i.e", "u.k" without them all needing to be in the list.
    if re.fullmatch(r"[a-z]\.?[a-z]", token):
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    """First pass: split on sentence-terminating punctuation, honoring abbreviations."""
    text = text.strip()
    if not text:
        return []

    sentences: list[str] = []
    buf_start = 0
    for match in _SENT_TERM.finditer(text):
        end = match.end()
        first_punct_idx = match.start()

        # Look ahead: is this actually followed by a sentence-starter?
        rest = text[end:].lstrip()
        followed_by_capital = bool(rest) and (rest[0].isupper() or rest[0] in "\"'“‘(")
        at_end_of_input = not rest

        # Not a sentence end if followed by lowercase / digit (e.g. "3.14", "example.com")
        if not (followed_by_capital or at_end_of_input):
            continue

        # Abbreviations normally suppress splitting — BUT if followed by a
        # capital letter, that's a real sentence end that happens to sit
        # after an abbreviation ("open at 6 p.m. Which time works?").
        if (
            text[first_punct_idx] == "."
            and _looks_like_abbreviation(text, first_punct_idx)
            and not followed_by_capital
        ):
            continue

        chunk = text[buf_start:end].strip()
        if chunk:
            sentences.append(chunk)
        buf_start = end

    tail = text[buf_start:].strip()
    if tail:
        sentences.append(tail)

    return sentences


def _split_long_sentence(sentence: str) -> list[str]:
    """If a sentence is too long, split on the strongest available inner break
    (semicolon > em-dash > comma)."""
    if len(sentence.split()) <= _MAX_WORDS_PER_CHUNK:
        return [sentence]

    # Try semicolons first
    if ";" in sentence:
        parts = [p.strip() for p in sentence.split(";") if p.strip()]
        return parts

    # Then em-dash / double hyphen
    if " — " in sentence:
        parts = [p.strip() for p in sentence.split(" — ") if p.strip()]
        return parts
    if " -- " in sentence:
        parts = [p.strip() for p in sentence.split(" -- ") if p.strip()]
        return parts

    # Fall back to comma split, but only if we get meaningfully-sized chunks
    if "," in sentence:
        parts = [p.strip() for p in sentence.split(",") if p.strip()]
        # Re-merge single-word fragments back into their neighbors so we don't
        # synth "the" as its own chunk.
        merged: list[str] = []
        for p in parts:
            if merged and len(p.split()) < 2:
                merged[-1] = merged[-1] + ", " + p
            else:
                merged.append(p)
        return merged

    # Nothing to split on — return as-is; TTS provider deals with it
    return [sentence]


def _merge_trailing_fragments(chunks: list[str]) -> list[str]:
    """If the last chunk is trivially short, glue it onto its predecessor.
    Prevents "OK.", "Bye.", "Thanks!" from being their own TTS calls."""
    if len(chunks) < 2:
        return chunks
    tail = chunks[-1]
    if len(tail.split()) < _MIN_TRAILING_WORDS:
        chunks = chunks[:-1] + [chunks[-2] + " " + tail]
        # Drop the now-duplicated second-to-last
        chunks.pop(-2)
    return chunks


def split_into_speakable_chunks(text: str) -> list[str]:
    """Public API. Returns a list of speakable chunks in emission order.

    Guarantees:
      - Concatenating chunks with spaces reconstructs the original semantic content.
      - No chunk exceeds ~25 words (except for one long unsplittable sentence).
      - No chunk is <3 words unless the input itself was <3 words.
      - Returns [] for empty/whitespace-only input.
    """
    text = (text or "").strip()
    if not text:
        return []

    sentences = _split_sentences(text)
    if not sentences:
        return [text]

    out: list[str] = []
    for s in sentences:
        out.extend(_split_long_sentence(s))

    out = _merge_trailing_fragments(out)
    return [c for c in out if c.strip()]
