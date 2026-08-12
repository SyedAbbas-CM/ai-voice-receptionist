from __future__ import annotations

import re

# End-of-sentence: . ? ! followed by whitespace or end-of-string.
# Avoid splitting on abbreviations by requiring the char to NOT be
# preceded by a single capital letter (e.g. "Dr."). Cheap heuristic;
# the sanitizer expands abbreviations later so any leaks are cosmetic.
_SENT_END = re.compile(r'(?<![A-Z])[.?!](?:\s+|$)')


class SentenceBuffer:
    """Accumulates streamed LLM tokens and emits complete sentences.

    The buffer holds tokens until a sentence-ending punctuation lands
    followed by whitespace or stream end. `push()` returns any newly
    complete sentences; `flush()` returns whatever is left after the
    stream ends. `full_text` is always the raw accumulated stream —
    used for guards (fake-booking) and ledger reconciliation.

    `min_first_chars` prevents firing on a lone "Yes." or "Sure." at
    the start of the reply — those are too short to justify a TTS RTT.
    When the first sentence is very short (< min_first_chars // 2) and
    a second sentence boundary exists in the current buffer, the two
    are merged into one emission so the TTS gets a fuller chunk.
    When the first sentence is short but not tiny (>= min_first_chars
    // 2) and a second sentence exists, both are emitted individually
    once the gate is passed.
    """

    def __init__(self, min_first_chars: int = 20) -> None:
        self._buf = ""
        self._full = ""
        self._first_emitted = False
        self._min_first_chars = min_first_chars

    def push(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._full += delta
        self._buf += delta
        out: list[str] = []
        while True:
            m = _SENT_END.search(self._buf)
            if m is None:
                break
            end = m.end()
            candidate = self._buf[:end].strip()
            if not candidate:
                self._buf = self._buf[end:]
                continue
            if not self._first_emitted and len(candidate) < self._min_first_chars:
                # First sentence is shorter than the threshold — peek ahead.
                m2 = _SENT_END.search(self._buf, end)
                if m2 is None:
                    # No second boundary yet; wait for more tokens.
                    break
                if len(candidate) < self._min_first_chars // 2:
                    # Very short opener (e.g. "Sure.") — merge with the
                    # following sentence into one TTS chunk.
                    end2 = m2.end()
                    combined = self._buf[:end2].strip()
                    out.append(combined)
                    self._buf = self._buf[end2:]
                else:
                    # Close to threshold (e.g. "Sure, one moment.") —
                    # emit as its own chunk and allow the next to follow.
                    out.append(candidate)
                    self._buf = self._buf[end:]
                self._first_emitted = True
                continue
            out.append(candidate)
            self._buf = self._buf[end:]
            self._first_emitted = True
        return out

    def flush(self) -> str:
        residual = self._buf.strip()
        self._buf = ""
        return residual

    @property
    def full_text(self) -> str:
        return self._full
