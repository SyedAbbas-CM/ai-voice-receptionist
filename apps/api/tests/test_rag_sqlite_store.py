"""SqliteVecRetriever tests. Uses NoopEmbedder so we don't load 33M
sentence-transformer weights in CI. That means the vector-side scoring
is degenerate (all-zeros vectors), so tests focus on:
  - upsert / dedup behavior
  - business_id scoping
  - BM25 finds keyword matches even without meaningful vectors
  - confidence normalization returns a valid [0, 1]

Full-fidelity retrieval quality is verified separately with an
integration test that loads real BGE embeddings (skipped in CI).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packages.rag import Chunk, ChunkKind, RetrievalHit
from packages.rag.embedder import NoopEmbedder
from packages.rag.sqlite_store import SqliteVecRetriever


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "kb.db")
    return SqliteVecRetriever(db_path=db_path, embedder=NoopEmbedder(dim=8))


def _make_chunk(business_id: str, text: str, source: str) -> Chunk:
    return Chunk(
        text=text,
        business_id=business_id,
        source=source,
        kind=ChunkKind.FAQ,
    )


@pytest.mark.asyncio
async def test_upsert_writes_all_three_tables(store):
    chunks = [
        _make_chunk("clinic1", "Q: Do you take Aetna insurance?\nA: Yes, we take Aetna PPO.", "business.json:faqs.insurance"),
    ]
    n = await store.upsert(chunks)
    assert n == 1
    assert await store.size() == 1


@pytest.mark.asyncio
async def test_upsert_is_idempotent(store):
    """Same chunks upserted twice = same size."""
    chunks = [
        _make_chunk("c1", "Insurance answer", "faqs.insurance"),
        _make_chunk("c1", "Hours answer", "hours"),
    ]
    await store.upsert(chunks)
    await store.upsert(chunks)
    assert await store.size() == 2


@pytest.mark.asyncio
async def test_search_scopes_to_business_id(store):
    """A chunk under business B should never surface in a search for A."""
    await store.upsert([
        _make_chunk("clinic_A", "Aetna insurance is accepted at clinic A", "faqs"),
        _make_chunk("clinic_B", "Blue Cross insurance is accepted at clinic B", "faqs"),
    ])

    hits = await store.search("insurance", business_id="clinic_A", top_k=3)
    assert all(h.chunk.business_id == "clinic_A" for h in hits), \
        "Search leaked chunks from another business"


@pytest.mark.asyncio
async def test_search_finds_bm25_match(store):
    """Even with degenerate vectors, BM25 should find keyword matches."""
    await store.upsert([
        _make_chunk("clinic1", "Aetna insurance is accepted", "faqs.insurance"),
        _make_chunk("clinic1", "Parking is behind the building", "faqs.parking"),
    ])

    hits = await store.search("Aetna insurance", business_id="clinic1", top_k=3)
    assert len(hits) >= 1
    # Top result should be the insurance chunk
    assert "insurance" in hits[0].chunk.text.lower()


@pytest.mark.asyncio
async def test_search_returns_empty_on_empty_query(store):
    hits = await store.search("", business_id="c1")
    assert hits == []


@pytest.mark.asyncio
async def test_retrieval_hit_confidence_bounds(store):
    await store.upsert([_make_chunk("c1", "Aetna insurance", "faqs")])
    hits = await store.search("Aetna", business_id="c1")
    for h in hits:
        assert 0.0 <= h.confidence <= 1.0


@pytest.mark.asyncio
async def test_size_scoped_by_business_id(store):
    await store.upsert([
        _make_chunk("a", "one", "s1"),
        _make_chunk("a", "two", "s2"),
        _make_chunk("b", "three", "s3"),
    ])
    assert await store.size("a") == 2
    assert await store.size("b") == 1
    assert await store.size() == 3


@pytest.mark.asyncio
async def test_search_top_k_caps_results(store):
    await store.upsert([
        _make_chunk("c1", f"chunk {i} about insurance", f"s{i}") for i in range(10)
    ])
    hits = await store.search("insurance", business_id="c1", top_k=3)
    assert len(hits) <= 3


@pytest.mark.asyncio
async def test_retrieval_hit_safety_helpers():
    """Just the RetrievalHit dataclass helpers — no store involvement."""
    high = RetrievalHit(chunk=_make_chunk("c", "x", "s"), score=1.0, confidence=0.9)
    mid = RetrievalHit(chunk=_make_chunk("c", "x", "s"), score=0.5, confidence=0.5)
    low = RetrievalHit(chunk=_make_chunk("c", "x", "s"), score=0.1, confidence=0.2)

    assert high.is_safe_to_speak is True
    assert high.needs_escalation is False

    assert mid.is_safe_to_speak is False
    assert mid.needs_escalation is False   # borderline zone

    assert low.is_safe_to_speak is False
    assert low.needs_escalation is True
