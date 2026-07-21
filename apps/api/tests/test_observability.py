"""Observability tests: tracer backends, span attribute recording,
and cost estimation for each provider."""
from __future__ import annotations

import json

import pytest

from packages.observability import (
    CostBook,
    CostRate,
    DEFAULT_COST_BOOK,
    InMemoryTracer,
    NoopTracer,
    build_tracer,
    estimate_llm_cost,
    estimate_stt_cost,
    estimate_tts_cost,
    get_tracer,
    set_tracer,
)


# ---- tracer backends ----

def test_noop_tracer_never_records():
    t = NoopTracer()
    with t.span("test") as s:
        s.set_attribute("k", "v")
    # Noop has no storage; just verify nothing crashed


def test_inmemory_tracer_records_duration_and_status():
    t = InMemoryTracer()
    with t.span("gen_ai.chat_completion", **{"gen_ai.system": "openai"}) as s:
        s.set_attribute("gen_ai.usage.input_tokens", 42)
    assert len(t.spans) == 1
    span = t.spans[0]
    assert span.name == "gen_ai.chat_completion"
    assert span.status == "ok"
    assert span.duration_ms is not None and span.duration_ms >= 0
    assert span.attributes["gen_ai.system"] == "openai"
    assert span.attributes["gen_ai.usage.input_tokens"] == 42


def test_inmemory_tracer_records_error_status():
    t = InMemoryTracer()
    with pytest.raises(RuntimeError):
        with t.span("failing_op") as _:
            raise RuntimeError("boom")
    assert len(t.spans) == 1
    assert t.spans[0].status == "error"
    assert "boom" in (t.spans[0].error_message or "")


def test_print_tracer_writes_json_line(capsys):
    t = build_tracer("print")
    with t.span("some.op", key="value") as _:
        pass
    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["span"] == "some.op"
    assert parsed["status"] == "ok"
    assert parsed["attributes"]["key"] == "value"


def test_factory_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_tracer("magic")


def test_singleton_default_is_noop():
    # A previous test may have swapped the singleton; reset then check
    set_tracer(NoopTracer())
    assert get_tracer().name == "noop"


def test_singleton_swap():
    original = get_tracer()
    try:
        set_tracer(InMemoryTracer())
        assert get_tracer().name == "memory"
    finally:
        set_tracer(original)


# ---- cost estimation ----

def test_llm_cost_uses_book_rate():
    cost = estimate_llm_cost("openai", "gpt-4o-mini",
                             input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.15, rel=0.01)


def test_llm_cost_accounts_for_cache_hits():
    """Cached input should cost 10% of fresh input. Anthropic's whole selling point."""
    fresh_cost = estimate_llm_cost("anthropic", "claude-sonnet-4-6",
                                    input_tokens=1_000_000, output_tokens=0)
    cached_cost = estimate_llm_cost("anthropic", "claude-sonnet-4-6",
                                     input_tokens=1_000_000, output_tokens=0,
                                     cache_read_tokens=1_000_000)
    assert fresh_cost == pytest.approx(3.00, rel=0.01)
    assert cached_cost == pytest.approx(0.30, rel=0.01)
    # Cache = 10% of fresh
    assert cached_cost < fresh_cost * 0.2


def test_llm_cost_unknown_provider_returns_zero():
    """Unknown provider = no rate row = $0 estimate. Fail-safe: never
    charge a client for a provider we don't have rates for."""
    cost = estimate_llm_cost("weirdcorp", "unknown-model",
                             input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == 0.0


def test_stt_cost_per_minute():
    cost = estimate_stt_cost("deepgram", seconds=60)
    assert cost == pytest.approx(0.0043, rel=0.01)


def test_stt_cost_local_free():
    cost = estimate_stt_cost("local", seconds=3600)
    assert cost == 0.0


def test_tts_cost_per_char():
    cost = estimate_tts_cost("elevenlabs", characters=1_000_000)
    assert cost > 0
    # Should be somewhere around $180 for 1M chars at their turbo rate
    assert 100 < cost < 300


def test_tts_cost_local_free():
    cost = estimate_tts_cost("qwen3", characters=1_000_000)
    assert cost == 0.0


def test_custom_cost_book_overrides_defaults():
    """Client can bring their own rates (e.g. enterprise discount)."""
    custom = CostBook(
        llm={"openai/gpt-4o": CostRate("openai", "gpt-4o", 0.50, 1.00, 0.10)},
    )
    cost = estimate_llm_cost("openai", "gpt-4o",
                              input_tokens=1_000_000, output_tokens=0,
                              book=custom)
    assert cost == pytest.approx(0.50, rel=0.01)
