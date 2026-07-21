"""Retriever abstraction — the search-side of RAG.

Every backend (SQLite, Postgres, LangChain-wrapped, Pinecone) implements
the same interface: given a query string, return top-K RetrievalHits.

Confidence scoring: each backend normalizes its native score into
[0, 1]. is_safe_to_speak / needs_escalation on RetrievalHit is what
the LLM tool uses to decide whether to say the answer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .embedder import Embedder
from .types import Chunk, RetrievalHit


class Retriever(ABC):
    """Query -> RetrievalHits."""

    name: str = "base"

    @abstractmethod
    async def upsert(self, chunks: list[Chunk]) -> int:
        """Add or replace chunks. Returns count actually written.
        Idempotent — same chunk (same id) upserts to same row."""

    @abstractmethod
    async def search(
        self,
        query: str,
        business_id: str,
        top_k: int = 3,
    ) -> list[RetrievalHit]:
        """Return top_k hits scoped to a single business.

        Callers should check hit.is_safe_to_speak before using the text."""

    async def size(self, business_id: Optional[str] = None) -> int:
        """Optional: total chunks. Default = 0 (subclasses override)."""
        return 0


def build_retriever(kind: str = "sqlite", **kwargs) -> Retriever:
    """Factory:
      - sqlite: local SQLite + sqlite-vec + FTS5 (default, zero-config)
      - supabase: Postgres + pgvector (needs supabase-py)
      - noop: returns empty results (tests)
      - langchain: adapter around any LangChain BaseRetriever (see docs)
    """
    kind = (kind or "sqlite").lower()
    if kind == "sqlite":
        from .sqlite_store import SqliteVecRetriever
        return SqliteVecRetriever(**kwargs)
    if kind == "noop":
        return NoopRetriever(**kwargs)
    if kind == "supabase":
        raise NotImplementedError("Supabase retriever coming in a follow-up pass")
    if kind == "langchain":
        raise NotImplementedError(
            "LangChain adapter is documented in docs/rag-adapters.md; "
            "wrap your existing retriever manually as a Retriever subclass."
        )
    raise ValueError(f"unknown retriever kind: {kind!r}")


class NoopRetriever(Retriever):
    """Empty retriever for tests + as the fallback when no KB is loaded."""

    name = "noop"

    def __init__(self, embedder: Optional[Embedder] = None):
        self.embedder = embedder
        self._chunks: dict[str, Chunk] = {}

    async def upsert(self, chunks: list[Chunk]) -> int:
        for c in chunks:
            self._chunks[c.id] = c
        return len(chunks)

    async def search(self, query: str, business_id: str, top_k: int = 3) -> list[RetrievalHit]:
        return []

    async def size(self, business_id: Optional[str] = None) -> int:
        if business_id is None:
            return len(self._chunks)
        return sum(1 for c in self._chunks.values() if c.business_id == business_id)
