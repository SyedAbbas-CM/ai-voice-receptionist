"""Regression lock for BUG #157 (Doctor Chen hallucination).

Live diagnosis from CAc66749590f6e53986eec4210e49bb425: caller said
"It was doctor." (partial STT).  LLM under time pressure called
`answer_context_task(answer='Chen')` — invented a doctor name never
in the caller's utterance.  Handler accepted because 'Chen' is a
non-empty string.

Fix: grounding check.  The answer's substantive content words must
appear (fuzzy) in the caller's recent utterances.  If not — reject
with ok:false, LLM re-asks.
"""
from __future__ import annotations


def _make_orch_with_task():
    """Build an orchestrator with one open task (procedure)."""
    from packages.dialogue.context_discovery import (
        ContextDiscoveryOrchestrator,
    )
    return ContextDiscoveryOrchestrator.for_service("Follow-up visit")


def test_grounded_answer_accepted():
    """Caller literally said 'implant' → answer='implant' passes."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "implant"},
        recent_caller_texts=["it was for the implant last month"],
    )
    assert result is not None
    assert result.get("ok") is True, (
        f"grounded answer should be accepted; got {result!r}"
    )


def test_hallucinated_provider_name_rejected():
    """Caller only said 'It was doctor.' — LLM invents 'Chen' →
    handler must REJECT (this is the Christiaan BUG 2 reproduction)."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "Chen"},
        recent_caller_texts=["it was doctor"],
    )
    assert result is not None
    assert result.get("ok") is False, (
        f"hallucinated 'Chen' should be rejected; got {result!r}"
    )
    assert "ground" in (result.get("error") or "").lower(), (
        f"error should mention grounding; got {result!r}"
    )


def test_stt_variance_still_accepted():
    """STT gives caller word slightly differently than LLM writes it.
    'crown' vs 'Crown' vs 'crowns' should all pass the grounding check."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "Crown"},  # capitalized
        recent_caller_texts=["i had a crowns done"],  # plural
    )
    assert result is not None
    assert result.get("ok") is True, (
        f"STT variance should still be accepted; got {result!r}"
    )


def test_multiword_answer_all_words_must_ground():
    """Answer 'root canal' — both words must appear in caller
    utterance for it to count as grounded."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    # Caller said 'root canal' → both words present → accepted.
    result_ok = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "root canal"},
        recent_caller_texts=["it was a root canal on my back tooth"],
    )
    assert result_ok.get("ok") is True

    orch2 = _make_orch_with_task()
    # Caller said only 'canal' → 'root' not present → rejected.
    result_bad = handle_discovery_tool_call(
        orch2,
        "answer_context_task",
        {"answer": "root canal"},
        recent_caller_texts=["it was for the canal thing"],
    )
    assert result_bad.get("ok") is False, (
        f"partial-grounding should be rejected; got {result_bad!r}"
    )


def test_stopwords_ignored_in_grounding():
    """Answer 'the crown' — 'the' is a stopword, only 'crown' must
    ground.  Otherwise every LLM answer starting with 'the' rejects."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "the crown"},
        recent_caller_texts=["crown"],
    )
    assert result.get("ok") is True


def test_empty_recent_texts_falls_back_to_permissive():
    """When no caller-utterance context is available (edge case
    e.g. test wire), fall back to old behavior: non-empty answer
    accepted.  We never want the grounding check to break brain when
    context is missing."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "Chen"},
        recent_caller_texts=None,
    )
    assert result.get("ok") is True


def test_backcompat_no_kwarg_still_works():
    """Callers that don't pass recent_caller_texts must still work
    (existing test suite + any pre-fix live callers)."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": "implant"},
    )
    assert result is not None
    assert result.get("ok") is True


def test_empty_answer_still_rejected():
    """Empty-answer branch preserved regardless of grounding."""
    from packages.dialogue.context_discovery import (
        handle_discovery_tool_call,
    )
    orch = _make_orch_with_task()
    result = handle_discovery_tool_call(
        orch,
        "answer_context_task",
        {"answer": ""},
        recent_caller_texts=["hello"],
    )
    assert result.get("ok") is False
    assert "empty" in (result.get("error") or "").lower()
