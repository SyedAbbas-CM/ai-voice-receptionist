"""Golden corpus + regression sweep (Phase 4, task #97).

Turns human-annotated calls tagged `is_gold=true` into a regression
safety net. On every deploy (or nightly), sweep re-runs the LK judges
over the last N calls, compares each score to the nearest gold match,
and alerts when scores drop below threshold.

## Design

**Gold corpus** = subset of `call_annotations` where `is_gold=true`.
Reviewer marks a well-behaved call as gold via the annotation UI
(checkbox already there — Phase 1). Voice-agent's product-lead
subagent is generating scripted ideal transcripts that get inserted
+ marked gold; reviewer-marked real calls also flow in.

**Similarity matching** = simple + defensible: match candidate call
to nearest gold by (tenant_id + business intent + call length band).
Not vector similarity — deliberately simple so a reviewer can predict
which gold call any candidate maps to. Fancy embedding-based matching
would obscure why an alert fires.

**Regression signal** = score delta between candidate and its matched
gold. Any judge going from `pass` on gold to `fail` on candidate is
the strongest signal. Aggregate score delta > threshold → alert.

## Not in v1

- No gold-transcript editing UI — reviewer marks existing rows, or
  voice-agent's script generator INSERTS new synthetic gold rows.
- No time-series drift analysis — one candidate vs one nearest gold,
  no rolling baseline yet.
- No auto-triage — alerts land in a log line + (later) a webhook.
  Human still decides "revert?"
- No cross-tenant learning — gold is per-tenant. Cross-tenant judges
  come with a bigger schema change.

## Wire status: MODULE ONLY

Follow-up commit adds:
1. `packages/evals/background_regression_runner.py` — on deploy or
   nightly cron, iterate recent calls + emit deltas.
2. Alert sink: log line for now; webhook when we have a real dest.
3. Env flag `ENABLE_REGRESSION_SWEEP=true`.

Consumers of the module can also invoke it directly for one-off
audits without wiring — e.g. `python -m packages.evals.golden_corpus
--tenant clinic --since-days 7`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from packages.evals.judge import (
    EvaluationResult,
    JudgeGroup,
    default_judge_panel,
)


log = logging.getLogger(__name__)


# ─── data shapes ────────────────────────────────────────────────────────────


@dataclass
class GoldCall:
    """One reference call from the gold corpus.

    Corresponds to a CallAnnotation row where `is_gold=true`, joined
    with its SessionRow + transcript. Kept as a plain dataclass so
    this module doesn't force the DB layer's shape on every caller
    (Phase 4 works from live SQLAlchemy rows OR fixture dicts —
    same interface).
    """
    call_id: str
    tenant_id: str
    transcript: list[dict]
    # Human's whole-call verdict at the time it was marked gold. Nearly
    # always "win" — but capture the raw value for audit.
    verdict: str
    # Intent tag — what THIS call is supposed to demonstrate ("booking
    # a follow-up", "reschedule", "cancel", "info question"). Set on
    # the annotation via a `turn_tag` of shape {"tag": "intent:X"}.
    # Defaults to "unknown" — the matcher then falls back to
    # length-band only.
    intent: str = "unknown"
    # Approx turn count — used for length-band matching.
    turn_count: int = 0
    # Free-text notes the reviewer left — surfaced in the alert
    # message when this gold is the matched reference.
    notes: str = ""


@dataclass
class RegressionSignal:
    """One candidate call's comparison to its matched gold."""
    call_id: str
    tenant_id: str
    matched_gold_call_id: Optional[str]
    matched_by: str  # "tenant+intent+length" | "tenant+length" | "no_match"
    candidate_score: float
    gold_score: float
    delta: float  # candidate - gold. Negative = regression.
    candidate_result: EvaluationResult
    # Per-judge deltas that flipped from pass to fail:
    flipped_to_fail: list[str] = field(default_factory=list)

    @property
    def is_regression(self) -> bool:
        """Any judge flipping pass→fail is a regression. Aggregate
        score drop >0.15 is also a regression."""
        return bool(self.flipped_to_fail) or self.delta < -0.15


# ─── corpus loader ─────────────────────────────────────────────────────────


class GoldCorpus:
    """In-memory view of the gold set for one tenant.

    Loader function is injected so this module doesn't own the DB
    session lifecycle. Real caller (background runner) will pass a fn
    that reads `call_annotations` + `sessions` + `transcript` and
    yields GoldCall objects.
    """

    def __init__(self, calls: list[GoldCall]) -> None:
        self._calls = list(calls)

    @classmethod
    def from_loader(
        cls,
        loader: Callable[[], list[GoldCall]],
    ) -> "GoldCorpus":
        return cls(loader())

    def size(self) -> int:
        return len(self._calls)

    def by_tenant(self, tenant_id: str) -> list[GoldCall]:
        return [c for c in self._calls if c.tenant_id == tenant_id]

    def find_match(
        self,
        tenant_id: str,
        candidate_intent: str,
        candidate_turn_count: int,
    ) -> tuple[Optional[GoldCall], str]:
        """Simple deterministic match. Returns (gold_call_or_None, reason)."""
        tenant_pool = self.by_tenant(tenant_id)
        if not tenant_pool:
            return None, "no_match"

        # 1st preference: same tenant + same intent + closest length
        by_intent = [c for c in tenant_pool if c.intent == candidate_intent]
        if by_intent:
            best = min(
                by_intent,
                key=lambda c: abs(c.turn_count - candidate_turn_count),
            )
            return best, "tenant+intent+length"

        # 2nd preference: same tenant + closest length (intent didn't match)
        best = min(
            tenant_pool,
            key=lambda c: abs(c.turn_count - candidate_turn_count),
        )
        return best, "tenant+length"


# ─── sweep runner ──────────────────────────────────────────────────────────


class RegressionSweep:
    """Run judges over candidate calls + compare to matched gold.

    Judges are cached across candidates — one instance covers a whole
    sweep. Concurrency: JudgeGroup.evaluate already runs judges
    concurrently for one call. This runner sweeps calls SEQUENTIALLY
    to avoid burying the LLM in N×5 parallel requests. For a nightly
    sweep of 50 calls that's fine; if we ever need faster, we bound
    with a semaphore.
    """

    def __init__(
        self,
        corpus: GoldCorpus,
        judges: Optional[list] = None,
        # Alert threshold: aggregate score drop worse than this fires.
        # 0.15 = "one judge worth of failure" on a 5-judge panel.
        regression_threshold: float = 0.15,
    ) -> None:
        self._corpus = corpus
        self._judge_group = JudgeGroup(judges or default_judge_panel())
        self._threshold = regression_threshold

    async def score_candidate(
        self,
        call_id: str,
        tenant_id: str,
        transcript: list[dict],
        candidate_intent: str,
        llm_caller: Callable,
    ) -> RegressionSignal:
        """Score ONE candidate + compare to matched gold.

        `llm_caller` is the judge LLM — see judge.Judge.evaluate contract.
        """
        candidate_turn_count = len(transcript)

        # Judge the candidate FIRST — we always want the score even if
        # there's no gold to compare against.
        cand_result = await self._judge_group.evaluate(
            call_id, transcript, llm_caller,
        )

        gold, matched_by = self._corpus.find_match(
            tenant_id, candidate_intent, candidate_turn_count,
        )
        if gold is None:
            return RegressionSignal(
                call_id=call_id,
                tenant_id=tenant_id,
                matched_gold_call_id=None,
                matched_by="no_match",
                candidate_score=cand_result.score,
                gold_score=0.0,
                delta=0.0,
                candidate_result=cand_result,
            )

        # Score the gold call too (cache-friendly: gold rarely
        # changes, so the caller can memoize this externally).
        gold_result = await self._judge_group.evaluate(
            gold.call_id, gold.transcript, llm_caller,
        )
        delta = cand_result.score - gold_result.score

        # Which judges regressed from pass → fail?
        flipped: list[str] = []
        for name, cand_j in cand_result.judgments.items():
            gold_j = gold_result.judgments.get(name)
            if gold_j is None:
                continue
            if gold_j.verdict == "pass" and cand_j.verdict == "fail":
                flipped.append(name)

        signal = RegressionSignal(
            call_id=call_id,
            tenant_id=tenant_id,
            matched_gold_call_id=gold.call_id,
            matched_by=matched_by,
            candidate_score=cand_result.score,
            gold_score=gold_result.score,
            delta=delta,
            candidate_result=cand_result,
            flipped_to_fail=flipped,
        )

        # Log every result — alert only on regression.
        if signal.is_regression:
            log.warning(
                "REGRESSION call=%s tenant=%s delta=%+.2f flipped=%s "
                "matched=%s(%s) notes=%r",
                call_id, tenant_id, delta, flipped,
                gold.call_id, matched_by, gold.notes[:100],
            )
        else:
            log.info(
                "regression-sweep OK call=%s delta=%+.2f matched=%s(%s)",
                call_id, delta, gold.call_id, matched_by,
            )
        return signal

    async def sweep(
        self,
        candidates: list[tuple[str, str, list[dict], str]],
        llm_caller: Callable,
    ) -> list[RegressionSignal]:
        """Sweep a list of candidate calls.

        `candidates` = [(call_id, tenant_id, transcript, intent), ...]
        Returns one RegressionSignal per candidate. Preserves order.
        """
        signals: list[RegressionSignal] = []
        for call_id, tenant_id, transcript, intent in candidates:
            try:
                sig = await self.score_candidate(
                    call_id, tenant_id, transcript, intent, llm_caller,
                )
                signals.append(sig)
            except Exception as e:
                log.error(
                    "regression-sweep candidate %s crashed: %r — skipping",
                    call_id, e,
                )
                continue
        return signals


# ─── convenience: summary aggregator ───────────────────────────────────────


@dataclass
class SweepSummary:
    """Aggregate across a whole sweep. What the alert channel receives."""
    total: int
    regressions: int
    no_matches: int
    mean_delta: float
    worst: Optional[RegressionSignal] = None


def summarize(signals: list[RegressionSignal]) -> SweepSummary:
    """Build the alert-ready summary. Callers push this to log/webhook."""
    if not signals:
        return SweepSummary(0, 0, 0, 0.0, None)
    regressions = [s for s in signals if s.is_regression]
    no_matches = [s for s in signals if s.matched_gold_call_id is None]
    mean_delta = sum(s.delta for s in signals) / len(signals)
    worst = min(signals, key=lambda s: s.delta) if signals else None
    return SweepSummary(
        total=len(signals),
        regressions=len(regressions),
        no_matches=len(no_matches),
        mean_delta=mean_delta,
        worst=worst if worst and worst.is_regression else None,
    )
