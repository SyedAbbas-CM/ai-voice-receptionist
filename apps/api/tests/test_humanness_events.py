"""Tests for humanness_events — structured event schema.

2026-08-29 (LiveKit steal #8 + debugging infra): every humanness event
must round-trip through pydantic validation cleanly, must serialize
to a dict the call_event_log can persist, and the emit helper must
never raise even when the log is unavailable.
"""
from __future__ import annotations

import pytest

from packages.observability.humanness_events import (
    BargeInDetectedEvent,
    EmptyLlmCompletionEvent,
    EmptyLlmDeterministicFallbackEvent,
    EmptyLlmRescueEvent,
    LlmClaimGuardEvent,
    PolicyDecisionEvent,
    ServiceResolutionEvent,
    SpeechGateDroppedEvent,
    TransferAttemptEvent,
    TurnSignalReducedEvent,
    emit_humanness_event,
)


# ── construction + defaults ─────────────────────────────────────


def test_empty_llm_completion_event_defaults():
    e = EmptyLlmCompletionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
    )
    assert e.event_kind == "empty_llm_completion"
    assert e.turn_generation == 0
    assert e.ts_ms == 0
    assert e.user_text == ""


def test_empty_llm_rescue_defaults():
    e = EmptyLlmRescueEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
    )
    assert e.event_kind == "empty_llm_rescue"
    assert e.recovered_text is False
    assert e.recovered_tools is False


def test_policy_decision_event_captures_action():
    e = PolicyDecisionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        turn_generation=3,
        action="ask_slot",
        acknowledgment="ack_understood",
        delivery_intent="standard",
        max_tokens=40,
        requested_slot="phone",
    )
    assert e.action == "ask_slot"
    assert e.requested_slot == "phone"


def test_turn_signal_reduced_captures_reasons():
    e = TurnSignalReducedEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        last_caller_text="my tooth hurts",
        caller_shared_hardship=True,
        reasons=["hardship_kw:hurt"],
    )
    assert e.caller_shared_hardship is True
    assert "hardship_kw:hurt" in e.reasons


def test_service_resolution_event_ambiguous():
    e = ServiceResolutionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        spoken="consultation",
        kind="ambiguous",
        candidates=["Invisalign consultation", "Implant consultation"],
        confidence=0.7,
        reason="multiple similar",
    )
    assert e.kind == "ambiguous"
    assert len(e.candidates) == 2


def test_transfer_attempt_captures_outcome():
    e = TransferAttemptEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        mode="warm",
        destination_id="agent_maria",
        destination_label="Maria Chen",
        reason="complaint",
        outcome="bridged",
    )
    assert e.outcome == "bridged"


def test_barge_in_event_kinds():
    for kind in ("real", "false_positive", "backchannel",
                  "min_words_not_met"):
        e = BargeInDetectedEvent(
            call_id="CA1", tenant_id="t1", session_id="s1",
            kind=kind,
        )
        assert e.kind == kind


def test_barge_in_invalid_kind_rejected():
    with pytest.raises(Exception):
        BargeInDetectedEvent(
            call_id="CA1", tenant_id="t1", session_id="s1",
            kind="fake_kind",  # type: ignore[arg-type]
        )


def test_speech_gate_dropped_captures_category():
    e = SpeechGateDroppedEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        category="wait_promise",
        sentence_preview="Let me check that for you",
    )
    assert e.category == "wait_promise"


def test_llm_claim_guard_captures_action():
    e = LlmClaimGuardEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        guard="booking",
        claim_text_preview="you're all set for Tuesday",
        receipt_present=False,
        action_taken="rewrote",
    )
    assert e.guard == "booking"
    assert e.receipt_present is False


# ── serialization contract ──────────────────────────────────


def test_event_serializes_to_dict_via_model_dump():
    """The emit helper calls .model_dump() — must produce a plain
    dict that JSON/SQLite can persist."""
    e = ServiceResolutionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        spoken="A follow-up", kind="match_exact",
        canonical_name="Follow-up visit", confidence=0.95,
    )
    d = e.model_dump()
    assert isinstance(d, dict)
    assert d["spoken"] == "A follow-up"
    assert d["kind"] == "match_exact"
    assert d["canonical_name"] == "Follow-up visit"


def test_event_round_trip_via_validate():
    """model_validate should reconstruct the event from a dict —
    consumers (evals, dashboard) round-trip via this path."""
    e = PolicyDecisionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        action="confirm_action",
        acknowledgment="ack_agreement",
        delivery_intent="warm",
        max_tokens=80,
        must_include_facts_count=3,
    )
    d = e.model_dump()
    reconstructed = PolicyDecisionEvent.model_validate(d)
    assert reconstructed == e


def test_event_extra_fields_rejected():
    """`extra=forbid` — schema drift caught at construction time."""
    with pytest.raises(Exception):
        PolicyDecisionEvent(
            call_id="CA1", tenant_id="t1", session_id="s1",
            action="answer",
            unknown_extra_field="oops",   # type: ignore[call-arg]
        )


def test_event_is_frozen():
    """Events are immutable after construction — no accidental
    mutation once emitted."""
    e = EmptyLlmCompletionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
    )
    with pytest.raises(Exception):
        e.user_text = "attempt mutation"  # type: ignore[misc]


# ── emit helper ─────────────────────────────────────────────


def test_emit_never_raises_when_log_unavailable(monkeypatch):
    """emit_humanness_event must swallow failures — humanness events
    must never crash the call path."""
    def _bad_getter():
        raise RuntimeError("log down")
    monkeypatch.setattr(
        "packages.observability.humanness_events.__name__",
        "packages.observability.humanness_events",
    )
    # Patch the call_event_log getter to raise.
    import packages.observability.call_event_log as _cel
    monkeypatch.setattr(_cel, "get_call_event_log", _bad_getter)
    e = EmptyLlmCompletionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
    )
    # Should NOT raise.
    emit_humanness_event(e)


def test_emit_never_raises_when_log_returns_none(monkeypatch):
    """When call_event_log returns None (no configured writer), emit
    is a no-op — no exception, no error log."""
    import packages.observability.call_event_log as _cel
    monkeypatch.setattr(_cel, "get_call_event_log", lambda: None)
    e = ServiceResolutionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        spoken="x", kind="unknown",
    )
    emit_humanness_event(e)


def test_emit_writes_when_log_available(monkeypatch):
    """The happy path — log.write() gets called with the payload."""
    written = []

    class _FakeLog:
        def write(self, event):
            written.append(event)

    import packages.observability.call_event_log as _cel
    monkeypatch.setattr(_cel, "get_call_event_log", lambda: _FakeLog())

    e = ServiceResolutionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        spoken="A follow-up", kind="match_exact",
        canonical_name="Follow-up visit",
    )
    emit_humanness_event(e)
    assert len(written) == 1
    ce = written[0]
    assert ce.call_id == "CA1"
    assert ce.tenant_id == "t1"
    assert ce.kind == "service_resolution"
    assert ce.payload["spoken"] == "A follow-up"
    assert ce.payload["canonical_name"] == "Follow-up visit"


# ── event_kind stability ────────────────────────────────────


def test_all_event_kinds_are_stable_strings():
    """The event_kind literal on each class is the query key downstream
    tools use.  Regression guard: if we rename one accidentally,
    the incident.py + evals harness break silently."""
    # (class, extra required fields, expected event_kind)
    cases = [
        (EmptyLlmCompletionEvent, {}, "empty_llm_completion"),
        (EmptyLlmRescueEvent, {}, "empty_llm_rescue"),
        (EmptyLlmDeterministicFallbackEvent, {},
         "empty_llm_deterministic_fallback"),
        (PolicyDecisionEvent, {"action": "answer"}, "policy_decision"),
        (TurnSignalReducedEvent, {}, "turn_signal_reduced"),
        (ServiceResolutionEvent, {"spoken": "x", "kind": "unknown"},
         "service_resolution"),
        (BargeInDetectedEvent, {}, "barge_in_detected"),
        (SpeechGateDroppedEvent, {}, "speech_gate_dropped"),
        (TransferAttemptEvent, {"mode": "warm"}, "transfer_attempt"),
        (LlmClaimGuardEvent, {"guard": "booking"}, "llm_claim_guard"),
    ]
    for cls, extra, expected in cases:
        instance = cls(
            call_id="x", tenant_id="y", session_id="z", **extra
        )
        assert instance.event_kind == expected, (
            f"{cls.__name__}.event_kind drifted to {instance.event_kind!r}"
        )
