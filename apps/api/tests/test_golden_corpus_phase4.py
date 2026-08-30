"""Phase 4 acceptance — golden corpus + regression sweep (task #97).

Tests the corpus + sweep primitive in isolation (no live LLM, no DB).
Fake llm_caller returns canned verdicts so we verify:
  - GoldCall + GoldCorpus data shapes
  - Matcher priority: tenant+intent+length → tenant+length → no_match
  - Regression detection: pass→fail flip OR delta > threshold
  - Sweep sequential execution + per-call failure isolation
  - Summary aggregation across the sweep
"""
from __future__ import annotations

import asyncio
import json

import pytest

from packages.evals.golden_corpus import (
    GoldCall,
    GoldCorpus,
    RegressionSignal,
    RegressionSweep,
    SweepSummary,
    summarize,
)


# ─── fixtures ───────────────────────────────────────────────────────────────


def _fake_llm_all_pass():
    async def caller(messages, tools):
        return {"tool_calls": [{"function": {"arguments":
            json.dumps({"verdict": "pass", "reasoning": "looks fine"})}}]}
    return caller


def _fake_llm_verdict(v: str):
    async def caller(messages, tools):
        return {"tool_calls": [{"function": {"arguments":
            json.dumps({"verdict": v, "reasoning": "test"})}}]}
    return caller


def _fake_llm_candidate_fail_gold_pass():
    """The sweep scores CANDIDATE first, then GOLD. First 5 responses
    (candidate) return fail; next 5 (gold) return pass. Simulates a
    regressed candidate against a good gold — every judge should
    flip pass→fail on the RegressionSignal."""
    state = {"count": 0}
    async def caller(messages, tools):
        state["count"] += 1
        # Judges run concurrently via asyncio.gather, so responses come
        # back in call-arrival order, not guaranteed order. But the
        # POOL of 5 requests is either 'candidate' or 'gold' depending
        # on which sweep step we're in — first 5 = candidate, next 5 = gold.
        v = "fail" if state["count"] <= 5 else "pass"
        return {"tool_calls": [{"function": {"arguments":
            json.dumps({"verdict": v, "reasoning": f"resp #{state['count']}"})}}]}
    return caller


def _gold_call(
    call_id: str, tenant: str = "clinic", intent: str = "booking",
    turns: int = 6, verdict: str = "win",
) -> GoldCall:
    return GoldCall(
        call_id=call_id, tenant_id=tenant,
        transcript=[{"role": "user", "text": "hi"}] * turns,
        verdict=verdict, intent=intent, turn_count=turns,
        notes=f"gold test call {call_id}",
    )


# ─── 1. GoldCall + GoldCorpus data shapes ───────────────────────────────────


def test_gold_call_defaults():
    g = GoldCall(call_id="CA1", tenant_id="clinic", transcript=[], verdict="win")
    assert g.intent == "unknown"
    assert g.turn_count == 0
    assert g.notes == ""


def test_corpus_size_and_by_tenant():
    corpus = GoldCorpus([
        _gold_call("CA1", tenant="clinic"),
        _gold_call("CA2", tenant="clinic"),
        _gold_call("CA3", tenant="real-estate"),
    ])
    assert corpus.size() == 3
    assert len(corpus.by_tenant("clinic")) == 2
    assert len(corpus.by_tenant("real-estate")) == 1
    assert corpus.by_tenant("unknown-tenant") == []


def test_corpus_from_loader():
    loader = lambda: [_gold_call("CA1"), _gold_call("CA2")]
    corpus = GoldCorpus.from_loader(loader)
    assert corpus.size() == 2


# ─── 2. Matcher priority ────────────────────────────────────────────────────


def test_match_tenant_intent_length_wins():
    corpus = GoldCorpus([
        _gold_call("CA_far", intent="booking", turns=20),
        _gold_call("CA_close", intent="booking", turns=6),
        _gold_call("CA_wrong_intent", intent="reschedule", turns=6),
    ])
    match, reason = corpus.find_match(
        tenant_id="clinic", candidate_intent="booking", candidate_turn_count=6,
    )
    assert match.call_id == "CA_close"
    assert reason == "tenant+intent+length"


def test_match_falls_back_to_length_when_intent_misses():
    corpus = GoldCorpus([
        _gold_call("CA1", intent="reschedule", turns=10),
        _gold_call("CA2", intent="cancel", turns=4),
    ])
    match, reason = corpus.find_match(
        tenant_id="clinic", candidate_intent="booking", candidate_turn_count=5,
    )
    assert match.call_id == "CA2"  # closer to 5 turns
    assert reason == "tenant+length"


def test_match_no_gold_returns_none():
    corpus = GoldCorpus([
        _gold_call("CA1", tenant="other-tenant"),
    ])
    match, reason = corpus.find_match(
        tenant_id="clinic", candidate_intent="booking", candidate_turn_count=5,
    )
    assert match is None
    assert reason == "no_match"


# ─── 3. Regression detection semantics ─────────────────────────────────────


def test_regression_signal_flip_is_regression():
    from packages.evals.judge import EvaluationResult
    s = RegressionSignal(
        call_id="C", tenant_id="t", matched_gold_call_id="G",
        matched_by="tenant+intent+length",
        candidate_score=0.8, gold_score=0.9, delta=-0.1,
        candidate_result=EvaluationResult(call_id="C"),
        flipped_to_fail=["task_completion"],
    )
    # delta is only -0.1 (below threshold) but a flip = regression
    assert s.is_regression is True


def test_regression_signal_big_delta_is_regression():
    from packages.evals.judge import EvaluationResult
    s = RegressionSignal(
        call_id="C", tenant_id="t", matched_gold_call_id="G",
        matched_by="tenant+intent+length",
        candidate_score=0.5, gold_score=0.9, delta=-0.4,
        candidate_result=EvaluationResult(call_id="C"),
        flipped_to_fail=[],
    )
    assert s.is_regression is True


def test_no_regression_when_delta_small_and_no_flip():
    from packages.evals.judge import EvaluationResult
    s = RegressionSignal(
        call_id="C", tenant_id="t", matched_gold_call_id="G",
        matched_by="tenant+length",
        candidate_score=0.85, gold_score=0.9, delta=-0.05,
        candidate_result=EvaluationResult(call_id="C"),
        flipped_to_fail=[],
    )
    assert s.is_regression is False


# ─── 4. Sweep — end-to-end ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_all_pass_no_regression():
    corpus = GoldCorpus([_gold_call("CA_gold", intent="booking", turns=6)])
    sweep = RegressionSweep(corpus)
    signals = await sweep.sweep(
        candidates=[("CA_cand", "clinic",
                     [{"role": "user", "text": "hi"}] * 6, "booking")],
        llm_caller=_fake_llm_all_pass(),
    )
    assert len(signals) == 1
    s = signals[0]
    assert s.matched_gold_call_id == "CA_gold"
    assert s.matched_by == "tenant+intent+length"
    assert s.candidate_score == 1.0
    assert s.gold_score == 1.0
    assert s.delta == 0.0
    assert not s.is_regression


@pytest.mark.asyncio
async def test_sweep_no_gold_available():
    """Candidate is scored but no comparison — reported as no_match, not regression."""
    corpus = GoldCorpus([])  # empty
    sweep = RegressionSweep(corpus)
    signals = await sweep.sweep(
        candidates=[("CA_cand", "clinic",
                     [{"role": "user", "text": "hi"}], "booking")],
        llm_caller=_fake_llm_all_pass(),
    )
    assert len(signals) == 1
    s = signals[0]
    assert s.matched_gold_call_id is None
    assert s.matched_by == "no_match"
    assert s.candidate_score == 1.0
    assert not s.is_regression


@pytest.mark.asyncio
async def test_sweep_detects_regression_on_flip():
    """Gold: all pass. Candidate: all fail. Should flag every judge."""
    corpus = GoldCorpus([_gold_call("CA_gold", intent="booking", turns=6)])
    sweep = RegressionSweep(corpus)
    signals = await sweep.sweep(
        candidates=[("CA_bad", "clinic",
                     [{"role": "user", "text": "hi"}] * 6, "booking")],
        llm_caller=_fake_llm_candidate_fail_gold_pass(),
    )
    s = signals[0]
    assert s.candidate_score == 0.0, (
        f"expected candidate=fail (score 0.0), got {s.candidate_score}"
    )
    assert s.gold_score == 1.0, (
        f"expected gold=pass (score 1.0), got {s.gold_score}"
    )
    assert s.delta == -1.0
    assert len(s.flipped_to_fail) == 5  # all judges flipped
    assert s.is_regression


@pytest.mark.asyncio
async def test_sweep_sequential_per_candidate_failure_isolated():
    """One candidate transcript that crashes the sweep must not
    prevent others from being scored."""
    # Use a transcript that judge module accepts + a normal one.
    corpus = GoldCorpus([_gold_call("CA_gold", intent="booking", turns=6)])
    sweep = RegressionSweep(corpus)
    signals = await sweep.sweep(
        candidates=[
            ("CA_ok1", "clinic",
             [{"role": "user", "text": "hi"}] * 3, "booking"),
            ("CA_ok2", "clinic",
             [{"role": "user", "text": "yo"}] * 4, "booking"),
        ],
        llm_caller=_fake_llm_all_pass(),
    )
    assert len(signals) == 2
    assert all(s.candidate_score == 1.0 for s in signals)


# ─── 5. Summary aggregation ─────────────────────────────────────────────────


def test_summarize_empty():
    s = summarize([])
    assert s.total == 0
    assert s.regressions == 0
    assert s.no_matches == 0
    assert s.mean_delta == 0.0
    assert s.worst is None


def test_summarize_mixed():
    from packages.evals.judge import EvaluationResult

    def mk(cid, delta, flip=None, gold_id="G"):
        return RegressionSignal(
            call_id=cid, tenant_id="t", matched_gold_call_id=gold_id,
            matched_by="tenant+intent+length" if gold_id else "no_match",
            candidate_score=0.5, gold_score=0.5 - delta, delta=delta,
            candidate_result=EvaluationResult(call_id=cid),
            flipped_to_fail=flip or [],
        )

    signals = [
        mk("CA1", -0.05),                          # normal, no reg
        mk("CA2", -0.30, flip=["accuracy"]),       # regression via delta AND flip
        mk("CA3", -0.10, flip=["coherence"]),      # regression via flip only
        mk("CA4", 0.0, gold_id=None),              # no match (won't flag flipped)
    ]
    s = summarize(signals)
    assert s.total == 4
    assert s.regressions == 2  # CA2 + CA3
    assert s.no_matches == 1   # CA4
    assert s.worst is not None
    assert s.worst.call_id == "CA2"  # worst delta
