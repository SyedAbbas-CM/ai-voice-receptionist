"""Voice-agent RAG.

Different from chatbot RAG in four key ways:
  1. Latency budget is 400-800ms end-to-end (not 3-5s)
  2. Retrieval runs as a tool call, not a preamble to every turn
  3. Answers must be short + speakable (URLs, markdown, tables removed)
  4. Grounding is stricter — voice can't be reread by the caller

Public API (import from here):
    Chunk          — one retrievable unit with source citation
    Retriever      — abstract; hybrid vector+BM25 search interface
    Embedder       — abstract; text -> vector interface
    RetrievalHit   — one search result with score + confidence
    build_retriever(kind, ...)  — factory: sqlite | supabase | langchain
    build_embedder(kind, ...)   — factory: local | openai | cohere
    shape_for_voice(...)        — chunk -> speakable sentence
"""
from .types import Chunk, RetrievalHit, ChunkKind
from .retriever import Retriever, build_retriever
from .embedder import Embedder, build_embedder
from .voice_shaper import shape_for_voice, is_speakable
from .evidence import (
    Answerability, EvidenceClaim, EvidenceBundle, build_bundle_from_hits,
)

__all__ = [
    "Chunk",
    "RetrievalHit",
    "ChunkKind",
    "Retriever",
    "build_retriever",
    "Embedder",
    "build_embedder",
    "shape_for_voice",
    "is_speakable",
    "Answerability",
    "EvidenceClaim",
    "EvidenceBundle",
    "build_bundle_from_hits",
]
