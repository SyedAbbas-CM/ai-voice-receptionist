"""Sprint 11a: capability-aware LLM routing tests.

Coverage:
  * Every capability table entry has non-empty approved_operations
  * MAIN_BRAIN never assigns a non-tool-calling model
  * PERF_PLANNER prefers REALTIME + FAST class
  * preferred_order_for de-dupes providers
  * models_for_operation respects max_latency filter
  * models_for_operation respects locale filter
  * capability_snapshot returns table + per_operation
"""
from __future__ import annotations

import pytest

from packages.dialogue import (
    CAPABILITY_TABLE,
    LatencyClass,
    Operation,
    capability_snapshot,
    models_for_operation,
    preferred_model_for,
    preferred_order_for,
)


def test_every_capability_has_approved_operations():
    for cap in CAPABILITY_TABLE:
        assert cap.approved_operations, \
            f"{cap.provider}:{cap.model} has empty approved_operations"


def test_main_brain_all_have_tool_calling():
    """A model approved for MAIN_BRAIN must have reliable_tool_calling.
    Booking absolutely can't downgrade to a non-tool model."""
    approved = models_for_operation(Operation.MAIN_BRAIN)
    for cap in approved:
        assert cap.reliable_tool_calling, \
            f"{cap.provider}:{cap.model} approved for MAIN_BRAIN but no tool calling"


def test_perf_planner_prefers_realtime_or_fast():
    """Perf planner has a 200-400ms budget — no DELIBERATE models."""
    approved = models_for_operation(
        Operation.PERF_PLANNER, max_latency=LatencyClass.FAST,
    )
    assert approved, "perf planner must have at least one approved model"
    for cap in approved:
        assert cap.latency_class in (LatencyClass.REALTIME, LatencyClass.FAST)


def test_preferred_order_dedupes_providers():
    """A provider with multiple approved models appears only once in
    the router-order string."""
    order = preferred_order_for(Operation.EXTRACTOR)
    providers = order.split(",")
    assert len(providers) == len(set(providers)), \
        f"duplicate providers in order: {order}"


def test_max_latency_filter_excludes_slower_class():
    """Setting max_latency=REALTIME must exclude FAST and DELIBERATE models."""
    fast_or_faster = models_for_operation(
        Operation.PERF_PLANNER, max_latency=LatencyClass.REALTIME,
    )
    for cap in fast_or_faster:
        assert cap.latency_class == LatencyClass.REALTIME


def test_locale_filter_english_permissive():
    """Every model supports en by default — en filter should return all
    otherwise-approved models."""
    with_en = models_for_operation(Operation.MAIN_BRAIN, locale="en")
    without = models_for_operation(Operation.MAIN_BRAIN)
    assert with_en == without


def test_locale_filter_french_restricts_to_multilingual_models():
    """Only Mistral + Gemini declare fr in multilingual."""
    fr_models = models_for_operation(Operation.MAIN_BRAIN, locale="fr")
    for cap in fr_models:
        assert "fr" in cap.multilingual


def test_require_tool_calling_filter():
    approved = models_for_operation(
        Operation.EXTRACTOR, require_tool_calling=True,
    )
    for cap in approved:
        assert cap.reliable_tool_calling


def test_preferred_model_returns_first_from_order():
    order_list = models_for_operation(Operation.PERF_PLANNER)
    preferred = preferred_model_for(Operation.PERF_PLANNER)
    if order_list:
        assert preferred == order_list[0]


def test_capability_snapshot_shape():
    snap = capability_snapshot()
    assert "table" in snap
    assert "per_operation" in snap
    # Every operation should appear
    for op in Operation:
        assert op.value in snap["per_operation"]


def test_write_guard_only_gets_strong_models():
    """Safety-critical: write_guard must not include ministral-3b or
    other weaker models even if they claim structured_output."""
    approved = models_for_operation(Operation.WRITE_GUARD)
    for cap in approved:
        assert cap.reliable_tool_calling
        # Explicit exclusion — 3B is too small for this
        assert not (cap.provider == "mistral" and "ministral-3b" in cap.model), \
            "ministral-3b must not be approved for write_guard"


def test_realtime_class_populated():
    """We need at least one REALTIME model or the perf planner has no
    fast path.  This is our latency-budget canary."""
    realtime_models = [c for c in CAPABILITY_TABLE
                       if c.latency_class == LatencyClass.REALTIME]
    assert realtime_models, "no realtime-class models in capability table"


def test_operations_have_at_least_one_approved_model():
    """Every Operation must have at least one approved model or we have
    a hole in the config."""
    for op in Operation:
        approved = models_for_operation(op)
        assert approved, f"no approved models for operation {op.value}"
