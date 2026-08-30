"""TF-IDF loop detector — safety net against LLM rephrase loops.

2026-08-30 (task #154, LK port T4 from voice/ivr/ivr_activity.py):
complements the DISCOVER_CONTEXT branch (task #150).  If the LLM
refuses the `answer_context_task` tool and just rephrases the same
discovery question 5x in a row, we need a signal to fire — either
force-open the tool, escalate to a human, or fall back to a canned
recovery script.

## How it works

Maintain a rolling window of the last N transcript chunks (agent
utterances typically — that's what loops).  On every new chunk,
compute TF-IDF cosine similarity of the newest against everything
else in the window.  If similarity > threshold, increment a
consecutive-loop counter.  If the counter reaches K, declare loop.

## Why pure-Python (no sklearn)

sklearn is ~30MB + numpy + scipy transitives.  We call this at most
once per turn against <20 short strings.  A pure-Python TF-IDF cosine
runs in well under 1ms at that scale.  Not worth the wheel.

## Contract

- `add(text)` — feed a new chunk; returns True iff loop detected
  THIS chunk (counter reached consecutive_threshold on this add)
- `reset()` — clear state (e.g. after successful topic transition)
- `state()` — inspection for observability

Never raises.  Malformed input → no-op.  Empty strings ignored.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


# ── TF-IDF cosine similarity (pure Python) ───────────────────


def _tokenize(text: str) -> list[str]:
    """Simple lowercase tokenizer.  Splits on non-word chars +
    drops empties + single-char noise ('a', 'i' still kept — they
    matter in short receptionist speech)."""
    if not text:
        return []
    return [
        t for t in re.split(r"[^\w']+", text.lower())
        if t
    ]


def _tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency (normalized to sum to 1)."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    n = float(sum(counts.values()))
    return {t: c / n for t, c in counts.items()}


def _idf(docs_tokens: list[list[str]]) -> dict[str, float]:
    """Inverse document frequency across the corpus.
    Uses log((N+1)/(df+1)) + 1 smoothing (matches sklearn default).
    """
    if not docs_tokens:
        return {}
    n_docs = len(docs_tokens)
    doc_freq: Counter = Counter()
    for tokens in docs_tokens:
        for term in set(tokens):
            doc_freq[term] += 1
    return {
        term: math.log((n_docs + 1) / (df + 1)) + 1.0
        for term, df in doc_freq.items()
    }


def _tfidf_vec(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf = _tf(tokens)
    return {t: tfv * idf.get(t, 0.0) for t, tfv in tf.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two sparse tf-idf vectors."""
    if not a or not b:
        return 0.0
    # Dot product on shared keys.
    shared = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in shared)
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _max_similarity_to_last(chunks: list[str]) -> float:
    """Max cosine similarity of the last chunk against every OTHER
    chunk in the window.  Returns 0.0 when fewer than 2 chunks."""
    if len(chunks) < 2:
        return 0.0
    docs = [_tokenize(c) for c in chunks]
    idf = _idf(docs)
    vecs = [_tfidf_vec(d, idf) for d in docs]
    last = vecs[-1]
    return max(
        (_cosine(last, other) for other in vecs[:-1]),
        default=0.0,
    )


# ── detector ─────────────────────────────────────────────


@dataclass
class TfidfLoopDetector:
    """Rolling window + consecutive-similar-chunks counter.

    Fields:
      window_size: how many recent chunks to compare against.
      similarity_threshold: cosine similarity above which two chunks
        are 'the same'.  0.85 matches LK's default.
      consecutive_threshold: number of consecutive add() calls above
        threshold before declaring a loop.  3 matches LK.
    """
    window_size: int = 20
    similarity_threshold: float = 0.85
    consecutive_threshold: int = 3
    _chunks: list[str] = field(default_factory=list)
    _consecutive: int = 0

    def __post_init__(self) -> None:
        if self.window_size <= 0:
            raise ValueError("window_size must be > 0")
        if not 0.0 <= self.similarity_threshold <= 1.0:
            raise ValueError(
                "similarity_threshold must be in [0.0, 1.0]"
            )
        if self.consecutive_threshold <= 0:
            raise ValueError("consecutive_threshold must be > 0")

    def reset(self) -> None:
        """Called after a successful topic transition so the detector
        doesn't fire on a healthy new phase."""
        self._chunks = []
        self._consecutive = 0

    def add(self, text: str) -> bool:
        """Feed the newest chunk.  Returns True iff loop detected
        as of THIS chunk.  Never raises.

        Empty / whitespace-only strings are ignored (no state change).
        """
        try:
            if not text or not text.strip():
                return False
            self._chunks.append(text)
            # Trim to window.
            if len(self._chunks) > self.window_size:
                self._chunks = self._chunks[-self.window_size:]
            if len(self._chunks) < 2:
                return False
            max_sim = _max_similarity_to_last(self._chunks)
            if max_sim >= self.similarity_threshold:
                self._consecutive += 1
            else:
                self._consecutive = 0
            return self._consecutive >= self.consecutive_threshold
        except Exception:
            # Never break the caller's turn on a detector bug.
            return False

    def state(self) -> dict:
        """Introspection for observability / trace payloads."""
        return {
            "window_size": self.window_size,
            "similarity_threshold": self.similarity_threshold,
            "consecutive_threshold": self.consecutive_threshold,
            "chunks_in_window": len(self._chunks),
            "consecutive_similar": self._consecutive,
            "last_max_similarity": (
                _max_similarity_to_last(self._chunks)
                if len(self._chunks) >= 2 else 0.0
            ),
        }


__all__ = [
    "TfidfLoopDetector",
    # Exported for tests + advanced tuning.
    "_tokenize",
    "_tf",
    "_idf",
    "_tfidf_vec",
    "_cosine",
    "_max_similarity_to_last",
]
