"""Per-business response cache for common turns.

Design:
  - Key: sha256(business_id, tenant_id, normalized(input_text))[:16]
  - Value: (reply_text, tts_cache_key, hits, created_at, last_used_at)
  - Store: SQLite (fast, transactional, portable, one file per install)
  - Lookup: <1 ms in-process (SQLite is disk-cached in RAM after first read)

Normalization is deliberately AGGRESSIVE to maximize hits:
  "Hi are you open Saturdays?" and "hey you open saturday?"
  should both hash to the same key.

Steps:
  1. lowercase
  2. strip leading/trailing whitespace
  3. collapse whitespace runs
  4. strip trailing punctuation
  5. strip leading fillers ("um", "uh", "like", "hi", "hey", "yeah")
  6. strip trailing "please"/"thanks"

A conservative filter — DON'T cache when:
  - Input contains dates/times/numbers ("book me tomorrow at 3")
  - Input contains PII patterns (phone-like, email-like)
  - Input contains a name ("my name is ...")
  - Business profile just changed (external cache-buster)
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Regex uncacheables — if the input matches any of these, don't cache.
# Callers with slots (dates, names, phones) get unique replies.
_UNCACHEABLE_PATTERNS = [
    re.compile(r"\b\d{1,2}[:.]\d{2}\b"),                    # times "3:30" "9.15"
    re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
    re.compile(r"\btomorrow|today|yesterday|tonight\b", re.I),
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(?:uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\b", re.I),
    re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),        # phone-ish
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),             # email
    re.compile(r"\bmy name is\b", re.I),
    re.compile(r"\bthis is (?!(?:the|a|an|smile|dr))\w", re.I),  # "This is John" but allow "This is the clinic"
    re.compile(r"\b(?:book|schedule|appointment|reserve|reservation)\b.*(?:\d|at|for|on)\b", re.I),
]


_LEADING_FILLERS = re.compile(
    r"^(?:um+|uh+|er+|hmm+|hi|hey|hello|yeah|yep|so|like|okay|ok|well)\s*[,.]?\s*",
    re.I,
)
_TRAILING_POLITENESS = re.compile(r"\s+(?:please|thanks|thank you)[.?!]*$", re.I)
_TRAILING_PUNCT = re.compile(r"[.?!,;:]+$")
_WHITESPACE = re.compile(r"\s+")


def normalize_input(text: str) -> str:
    """Normalize a caller turn for cache-key hashing.

    Returns empty string for uncacheable inputs (unique-slot content)."""
    if not text:
        return ""
    s = text.strip().lower()
    if not s:
        return ""

    # Reject uncacheable slot-bearing content
    for pattern in _UNCACHEABLE_PATTERNS:
        if pattern.search(s):
            return ""

    # Strip fillers repeatedly (handles "um uh hi")
    for _ in range(3):
        new = _LEADING_FILLERS.sub("", s)
        if new == s:
            break
        s = new

    s = _TRAILING_POLITENESS.sub("", s)
    s = _TRAILING_PUNCT.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()

    # Short/trivial acknowledgements → don't cache
    # (backchannels, one-word confirmations aren't reply-worthy).
    _TRIVIAL = {"ok", "yes", "no", "yep", "yeah", "sure", "fine", "cool",
                "nope", "nah", "mhm", "uh huh", "right"}
    if len(s) < 4 or s in _TRIVIAL:
        return ""
    return s


def _hash_key(business_id: str, tenant_id: str, normalized_text: str) -> str:
    """Stable 16-char hex key for (business, tenant, normalized-input)."""
    h = hashlib.sha256(
        f"{tenant_id}|{business_id}|{normalized_text}".encode("utf-8"),
    ).hexdigest()
    return h[:16]


@dataclass
class ResponseCacheEntry:
    """One cached reply for a common input pattern."""
    key: str
    business_id: str
    tenant_id: str
    input_text: str            # original (first-seen) form, for debugging
    normalized_text: str
    reply_text: str
    tts_cache_key: Optional[str]   # points into packages/tts_cache
    hits: int
    created_at: float
    last_used_at: float


class ResponseCache:
    """SQLite-backed response cache.

    All operations are synchronous (SQLite is fast enough that async wrapping
    adds more overhead than it saves at cache-hit sizes)."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS response_cache (
        key             TEXT PRIMARY KEY,
        business_id     TEXT NOT NULL,
        tenant_id       TEXT NOT NULL,
        input_text      TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        reply_text      TEXT NOT NULL,
        tts_cache_key   TEXT,
        hits            INTEGER NOT NULL DEFAULT 0,
        created_at      REAL NOT NULL,
        last_used_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_response_business ON response_cache(business_id, tenant_id);
    CREATE INDEX IF NOT EXISTS idx_response_hits ON response_cache(hits DESC);
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,   # safe: we serialize via WAL + single writer
            isolation_level=None,      # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._SCHEMA)

    def get(
        self,
        business_id: str,
        tenant_id: str,
        input_text: str,
    ) -> Optional[ResponseCacheEntry]:
        """Fast lookup.  Returns None on miss or uncacheable input."""
        normalized = normalize_input(input_text)
        if not normalized:
            return None
        key = _hash_key(business_id, tenant_id, normalized)
        row = self._conn.execute(
            "SELECT key, business_id, tenant_id, input_text, normalized_text, "
            "reply_text, tts_cache_key, hits, created_at, last_used_at "
            "FROM response_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        # Bump hits + last_used (fire-and-forget, don't block reply path)
        now = time.time()
        self._conn.execute(
            "UPDATE response_cache SET hits = hits + 1, last_used_at = ? WHERE key = ?",
            (now, key),
        )
        return ResponseCacheEntry(
            key=row[0], business_id=row[1], tenant_id=row[2], input_text=row[3],
            normalized_text=row[4], reply_text=row[5], tts_cache_key=row[6],
            hits=row[7] + 1, created_at=row[8], last_used_at=now,
        )

    def put(
        self,
        business_id: str,
        tenant_id: str,
        input_text: str,
        reply_text: str,
        tts_cache_key: Optional[str] = None,
    ) -> Optional[str]:
        """Store a (input → reply) mapping.  Returns cache key or None if uncacheable.

        No-op if input normalizes to empty (uncacheable slot content)."""
        normalized = normalize_input(input_text)
        if not normalized:
            return None
        if not reply_text or len(reply_text) < 3:
            return None
        key = _hash_key(business_id, tenant_id, normalized)
        now = time.time()
        # INSERT OR REPLACE preserves the key but resets hits — we don't
        # want to reset hits if the reply text is identical, so do INSERT OR IGNORE
        # then UPDATE the reply if it changed.
        self._conn.execute(
            "INSERT OR IGNORE INTO response_cache "
            "(key, business_id, tenant_id, input_text, normalized_text, "
            "reply_text, tts_cache_key, hits, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (key, business_id, tenant_id, input_text, normalized,
             reply_text, tts_cache_key, now, now),
        )
        # If it already existed with a different reply, update the reply +
        # tts_cache_key but keep hits (we've served this input before, the
        # answer just improved).
        self._conn.execute(
            "UPDATE response_cache SET reply_text = ?, tts_cache_key = ? "
            "WHERE key = ? AND reply_text != ?",
            (reply_text, tts_cache_key, key, reply_text),
        )
        return key

    def invalidate_business(self, business_id: str, tenant_id: str) -> int:
        """Drop all cached entries for a business (e.g. on profile change).
        Returns count deleted."""
        cursor = self._conn.execute(
            "DELETE FROM response_cache WHERE business_id = ? AND tenant_id = ?",
            (business_id, tenant_id),
        )
        return cursor.rowcount

    def top_hits(
        self,
        business_id: str,
        tenant_id: str,
        limit: int = 20,
    ) -> list[ResponseCacheEntry]:
        """Return the N most-hit entries for a business — useful for warmup +
        observability dashboards."""
        rows = self._conn.execute(
            "SELECT key, business_id, tenant_id, input_text, normalized_text, "
            "reply_text, tts_cache_key, hits, created_at, last_used_at "
            "FROM response_cache WHERE business_id = ? AND tenant_id = ? "
            "ORDER BY hits DESC LIMIT ?",
            (business_id, tenant_id, limit),
        ).fetchall()
        return [
            ResponseCacheEntry(
                key=r[0], business_id=r[1], tenant_id=r[2], input_text=r[3],
                normalized_text=r[4], reply_text=r[5], tts_cache_key=r[6],
                hits=r[7], created_at=r[8], last_used_at=r[9],
            )
            for r in rows
        ]

    def stats(self) -> dict:
        """Cache-wide stats for observability."""
        row = self._conn.execute(
            "SELECT COUNT(*), SUM(hits), MAX(hits) FROM response_cache"
        ).fetchone()
        return {
            "entries": row[0] or 0,
            "total_hits": row[1] or 0,
            "top_hits": row[2] or 0,
        }


_shared: Optional[ResponseCache] = None


def get_shared_response_cache(db_path: Optional[Path | str] = None) -> ResponseCache:
    """Process-wide singleton so all callers see the same cache."""
    global _shared
    if _shared is None:
        if db_path is None:
            db_path = Path(__file__).resolve().parents[2] / "data" / "response_cache.db"
        _shared = ResponseCache(db_path)
    return _shared
