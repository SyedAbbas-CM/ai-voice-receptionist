"""RAG Evidence Bundle (Sprint 11b).

Audit's finding: RAG returns a nicely-worded prose answer.  That
gives the LLM a fluent-but-unverifiable string; hallucinations creep
in during the "shape for voice" step and go undetected.

Fix: return an EvidenceBundle.  Each claim carries its source_id,
source_span, and freshness.  The semantic planner (or the brain) then
decides how to speak from grounded evidence — and can flag what
wasn't covered.

Also introduces answerability classification: even a strong retrieval
hit doesn't guarantee the caller's question is answerable from that
chunk (mismatched intent, partial coverage).

Kept as pure data types + a builder.  LookupAnswerHandler decides
whether to emit an EvidenceBundle (new schema) or a legacy prose
answer (back-compat), gated by a flag on the handler.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Answerability(str, Enum):
    """How confidently the retrieved evidence supports the question."""
    SUPPORTED = "supported"
    """Evidence directly answers the question with high relevance."""
    PARTIALLY_SUPPORTED = "partially_supported"
    """Evidence addresses the topic but leaves parts of the question
    unanswered (multi-part questions, missing context)."""
    UNSUPPORTED = "unsupported"
    """Retrieved evidence is on a different topic; answer not derivable."""
    CONFLICTING = "conflicting"
    """Multiple retrieved sources contradict each other; caller needs
    a human or fresh source."""


class EvidenceClaim(BaseModel):
    """One factual claim backed by retrieved evidence."""
    claim: str
    """The claim in plain language.  This is what the agent would
    speak if it chose to include this claim."""
    source_id: str
    """Stable identifier for the source (chunk_id, doc id, etc)."""
    source_span: str
    """The verbatim text from the source that supports the claim.
    Kept as evidence for audit / display."""
    relevance: float = Field(ge=0.0, le=1.0)
    """0..1 how directly the source supports the claim."""
    freshness_iso: Optional[str] = None
    """ISO timestamp of when the source was last updated.  Enables
    stale-source detection (freshness < X days is preferred)."""


class EvidenceBundle(BaseModel):
    """Full RAG output for one caller question.  Consumed by the
    semantic planner to decide what to say.

    Downstream contract:
      * If answerability == SUPPORTED: speak using critical facts.
      * If PARTIALLY_SUPPORTED: speak what's supported + acknowledge
        unsupported_parts + offer callback for the rest.
      * If UNSUPPORTED: don't fabricate — say we don't have that info +
        offer callback.
      * If CONFLICTING: escalate; don't gamble on which source is right.
    """
    question: str
    answerability: Answerability
    claims: list[EvidenceClaim] = Field(default_factory=list)
    unsupported_parts: list[str] = Field(default_factory=list)
    """Sub-questions the retrieved evidence doesn't address.  Semantic
    planner should call these out to the caller."""
    top_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    """Highest per-claim relevance in this bundle.  Legacy handlers
    that expect a single 'confidence' number read this."""
    reason: Optional[str] = None
    """Machine-readable code when answerability is UNSUPPORTED
    (e.g. 'no_match', 'low_confidence', 'freshness_expired')."""

    def critical_claims(self, min_relevance: float = 0.7) -> list[EvidenceClaim]:
        """Claims above the trust threshold — safe to speak verbatim."""
        return [c for c in self.claims if c.relevance >= min_relevance]

    def is_speakable(self) -> bool:
        """True when the semantic planner has enough to say something
        useful without hedging into uselessness."""
        return self.answerability in (
            Answerability.SUPPORTED, Answerability.PARTIALLY_SUPPORTED,
        ) and len(self.critical_claims()) > 0


# ── builder ─────────────────────────────────────────────────────────

def build_bundle_from_hits(
    question: str,
    hits: list,
    *,
    confidence_threshold: float = 0.7,
    freshness_lookup=None,
) -> EvidenceBundle:
    """Convert a list of RAG hits (RetrievalHit) into an EvidenceBundle.

    Answerability heuristic:
      * top hit >= threshold AND >=1 other hit >= threshold*0.6 → SUPPORTED
      * top hit >= threshold, others weak → PARTIALLY_SUPPORTED
      * top hit < threshold → UNSUPPORTED
      * top-2 both >= threshold but contradict (same slot different
        answers) → CONFLICTING (out of scope for this heuristic;
        upgrade to semantic contradiction detection in a followup)

    `freshness_lookup(source_id) -> iso_ts_or_None` is optional; when
    provided, each claim gets a freshness_iso stamp.
    """
    if not hits:
        return EvidenceBundle(
            question=question,
            answerability=Answerability.UNSUPPORTED,
            top_confidence=0.0,
            reason="no_match",
        )

    top = hits[0]
    top_conf = float(top.confidence)

    if top_conf < confidence_threshold:
        return EvidenceBundle(
            question=question,
            answerability=Answerability.UNSUPPORTED,
            top_confidence=round(top_conf, 3),
            reason="low_confidence",
        )

    # Build claims from top-K supporting hits
    claims: list[EvidenceClaim] = []
    for h in hits[:5]:
        conf = float(h.confidence)
        if conf < confidence_threshold * 0.6:
            continue
        freshness = None
        if freshness_lookup is not None:
            try:
                freshness = freshness_lookup(h.chunk.source)
            except Exception:
                pass
        claims.append(EvidenceClaim(
            claim=h.chunk.text.strip()[:400],
            source_id=h.chunk.source,
            source_span=h.chunk.text.strip()[:400],
            relevance=round(conf, 3),
            freshness_iso=freshness,
        ))

    # Answerability: SUPPORTED vs PARTIALLY_SUPPORTED
    if len(claims) >= 2:
        second_conf = claims[1].relevance
        if second_conf >= confidence_threshold * 0.75:
            answerability = Answerability.SUPPORTED
        else:
            answerability = Answerability.PARTIALLY_SUPPORTED
    else:
        answerability = Answerability.PARTIALLY_SUPPORTED

    return EvidenceBundle(
        question=question,
        answerability=answerability,
        claims=claims,
        top_confidence=round(top_conf, 3),
    )
