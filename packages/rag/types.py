"""Core data types for voice-agent RAG.

These stay simple and JSON-serializable so they can round-trip through
SQLite, Postgres, Pinecone, or any other backend without leakage.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ChunkKind(str, Enum):
    """What kind of source a chunk came from.

    Affects retrieval weighting downstream — FAQs and services are
    always high-precision matches; docs are broader knowledge.
    """
    FAQ = "faq"
    SERVICE = "service"
    HOURS = "hours"
    POLICY = "policy"
    DOC = "doc"
    OTHER = "other"


@dataclass
class Chunk:
    """One retrievable unit.

    IDs are content-derived (SHA-256 of source+text) so ingesting the
    same source twice deduplicates cleanly.
    """
    text: str
    business_id: str
    source: str                     # e.g. "business.json:faqs.insurance" or "policies.md:section_3"
    kind: ChunkKind = ChunkKind.OTHER
    metadata: dict = field(default_factory=dict)
    id: Optional[str] = None

    def __post_init__(self):
        if not self.id:
            h = hashlib.sha256()
            h.update(self.business_id.encode())
            h.update(b"\x00")
            h.update(self.source.encode())
            h.update(b"\x00")
            h.update(self.text.encode())
            self.id = h.hexdigest()[:16]


@dataclass
class RetrievalHit:
    """One search result from a Retriever.

    Score is backend-specific (cosine, BM25, fused) — treat as opaque.
    Confidence is normalized to [0, 1] where >= 0.7 = safe to speak,
    0.4-0.7 = maybe, < 0.4 = escalate.
    """
    chunk: Chunk
    score: float
    confidence: float               # [0, 1] normalized

    @property
    def is_safe_to_speak(self) -> bool:
        return self.confidence >= 0.7

    @property
    def needs_escalation(self) -> bool:
        return self.confidence < 0.4
