"""Sprint 11b: RAG evidence bundle tests.

Coverage:
  * build_bundle_from_hits: no hits → UNSUPPORTED with reason=no_match
  * top hit below threshold → UNSUPPORTED with reason=low_confidence
  * strong top + weak rest → PARTIALLY_SUPPORTED
  * strong top + strong second → SUPPORTED
  * claims respect min_relevance filter
  * critical_claims / is_speakable helpers
  * LookupAnswerHandler emits bundle when flag on, prose when off
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from packages.rag import (
    Answerability,
    EvidenceBundle,
    EvidenceClaim,
    build_bundle_from_hits,
)
from packages.rag.types import Chunk, ChunkKind, RetrievalHit


def _hit(text: str, source: str, confidence: float) -> RetrievalHit:
    chunk = Chunk(
        id=f"c-{source}",
        business_id="acme",
        source=source,
        kind=ChunkKind.FAQ,
        text=text,
    )
    return RetrievalHit(chunk=chunk, score=confidence, confidence=confidence)


# ── build_bundle_from_hits ─────────────────────────────────────────

def test_no_hits_returns_unsupported():
    b = build_bundle_from_hits("hours?", [])
    assert b.answerability == Answerability.UNSUPPORTED
    assert b.reason == "no_match"
    assert b.top_confidence == 0.0
    assert b.claims == []


def test_top_below_threshold_unsupported():
    hits = [_hit("some weak answer", "src1", 0.4)]
    b = build_bundle_from_hits("hours?", hits, confidence_threshold=0.7)
    assert b.answerability == Answerability.UNSUPPORTED
    assert b.reason == "low_confidence"
    assert b.claims == []


def test_strong_top_weak_rest_partially_supported():
    hits = [
        _hit("Mon-Fri 9-5", "hours1", 0.9),
        _hit("Parking is free", "parking1", 0.3),  # dropped: below 0.6*0.7=0.42
    ]
    b = build_bundle_from_hits("what are your hours?", hits,
                               confidence_threshold=0.7)
    assert b.answerability == Answerability.PARTIALLY_SUPPORTED
    # Weak second chunk dropped from claims list
    assert len(b.claims) == 1


def test_strong_top_strong_second_supported():
    hits = [
        _hit("Mon-Fri 9-5", "hours1", 0.9),
        _hit("Sat 10-2", "hours2", 0.75),   # >= 0.7 * 0.75 = 0.525 ✓
    ]
    b = build_bundle_from_hits("hours?", hits, confidence_threshold=0.7)
    assert b.answerability == Answerability.SUPPORTED
    assert len(b.claims) == 2


def test_top_confidence_matches_first_hit():
    hits = [_hit("...", "s", 0.87)]
    b = build_bundle_from_hits("q?", hits, confidence_threshold=0.7)
    assert b.top_confidence == 0.87


def test_claims_capped_at_five():
    hits = [_hit(f"text {i}", f"src{i}", 0.8) for i in range(10)]
    b = build_bundle_from_hits("q?", hits, confidence_threshold=0.7)
    assert len(b.claims) == 5


def test_freshness_lookup_populates_field():
    hits = [_hit("...", "src1", 0.9)]
    b = build_bundle_from_hits(
        "q?", hits, confidence_threshold=0.7,
        freshness_lookup=lambda sid: "2026-07-01T00:00:00" if sid == "src1" else None,
    )
    assert b.claims[0].freshness_iso == "2026-07-01T00:00:00"


def test_freshness_lookup_exception_no_crash():
    hits = [_hit("...", "src1", 0.9)]
    def _boom(sid): raise RuntimeError("boom")
    b = build_bundle_from_hits(
        "q?", hits, confidence_threshold=0.7, freshness_lookup=_boom,
    )
    assert b.claims[0].freshness_iso is None


# ── bundle helpers ─────────────────────────────────────────────────

def test_critical_claims_filter():
    b = EvidenceBundle(
        question="q?", answerability=Answerability.SUPPORTED,
        claims=[
            EvidenceClaim(claim="strong", source_id="s1",
                          source_span="s", relevance=0.9),
            EvidenceClaim(claim="weak", source_id="s2",
                          source_span="s", relevance=0.5),
        ],
    )
    assert len(b.critical_claims()) == 1
    assert b.critical_claims()[0].claim == "strong"


def test_is_speakable_true_when_supported_with_claims():
    b = EvidenceBundle(
        question="q?", answerability=Answerability.SUPPORTED,
        claims=[EvidenceClaim(claim="c", source_id="s", source_span="s",
                              relevance=0.9)],
    )
    assert b.is_speakable() is True


def test_is_speakable_false_when_unsupported():
    b = EvidenceBundle(
        question="q?", answerability=Answerability.UNSUPPORTED,
    )
    assert b.is_speakable() is False


def test_is_speakable_false_when_supported_but_no_critical_claims():
    b = EvidenceBundle(
        question="q?", answerability=Answerability.PARTIALLY_SUPPORTED,
        claims=[EvidenceClaim(claim="weak", source_id="s", source_span="s",
                              relevance=0.3)],
    )
    assert b.is_speakable() is False


# ── LookupAnswerHandler wiring ─────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_prose_output_default():
    """Default emit_evidence_bundle=False returns prose {answer, ...}."""
    from packages.integrations.rag_tool import LookupAnswerHandler
    from packages.schemas import ToolCall

    class _FakeRetriever:
        async def search(self, q, business_id, top_k=3):
            return [_hit("we take delta dental", "faq_insurance", 0.9)]
    class _FakeShaper:
        async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
            class _R: text = "Yes, we take Delta Dental."
            r = _R()
            r.tool_calls = []
            r.finish_reason = "stop"
            r.raw = None
            return r

    handler = LookupAnswerHandler(
        business_id="acme", retriever=_FakeRetriever(),
        shaper_llm=_FakeShaper(), confidence_threshold=0.7,
    )
    result = await handler(ToolCall(id="c1", name="lookup_answer",
                                    arguments={"question": "delta?"}))
    assert "answer" in result.result
    assert result.result["answer"]   # prose non-empty
    assert "answerability" not in result.result


@pytest.mark.asyncio
async def test_handler_bundle_output_when_flag_on():
    from packages.integrations.rag_tool import LookupAnswerHandler
    from packages.schemas import ToolCall

    class _FakeRetriever:
        async def search(self, q, business_id, top_k=3):
            return [
                _hit("we accept delta dental ppo", "faq_ins", 0.92),
                _hit("we accept cigna dppo", "faq_ins2", 0.78),
            ]

    handler = LookupAnswerHandler(
        business_id="acme", retriever=_FakeRetriever(),
        shaper_llm=None, confidence_threshold=0.7,
        emit_evidence_bundle=True,
    )
    result = await handler(ToolCall(id="c1", name="lookup_answer",
                                    arguments={"question": "delta?"}))
    r = result.result
    assert "answerability" in r
    assert r["answerability"] == "supported"
    assert len(r["claims"]) == 2
    assert r["top_confidence"] == 0.92
    # Prose 'answer' key must NOT be present
    assert "answer" not in r


@pytest.mark.asyncio
async def test_handler_bundle_output_no_match():
    from packages.integrations.rag_tool import LookupAnswerHandler
    from packages.schemas import ToolCall

    class _FakeRetriever:
        async def search(self, q, business_id, top_k=3):
            return []

    handler = LookupAnswerHandler(
        business_id="acme", retriever=_FakeRetriever(),
        shaper_llm=None, confidence_threshold=0.7,
        emit_evidence_bundle=True,
    )
    result = await handler(ToolCall(id="c1", name="lookup_answer",
                                    arguments={"question": "delta?"}))
    r = result.result
    assert r["answerability"] == "unsupported"
    assert r["reason"] == "no_match"
