"""Task #150 tests: ContextDiscoveryOrchestrator.

The audit-diagnosed missing branch: when caller says 'a follow-up',
policy should ask 'follow-up to what / with which doctor / when was
original visit' BEFORE moving to phone. Adapted from LK's
beta/workflows/task_group.py.
"""
from __future__ import annotations

import pytest

from packages.dialogue.context_discovery import (
    ContextDiscoveryOrchestrator,
    ContextTask,
    ContextTaskStatus,
    context_tasks_for_service,
)


# ── task registry ─────────────────────────────────────────


def test_follow_up_visit_has_three_context_tasks():
    tasks = context_tasks_for_service("Follow-up visit")
    assert len(tasks) == 3
    task_ids = [t.task_id for t in tasks]
    assert task_ids == [
        "original_procedure",
        "original_provider",
        "original_visit_date",
    ]


def test_service_with_no_context_returns_empty():
    """Most services need no context — just book them."""
    assert context_tasks_for_service("Adult cleaning") == []
    assert context_tasks_for_service("Emergency exam") == []


def test_unknown_service_returns_empty():
    assert context_tasks_for_service("Purple submarine") == []


def test_none_service_returns_empty():
    assert context_tasks_for_service(None) == []


def test_context_tasks_are_fresh_instances_per_call():
    """Every call gets fresh ContextTask objects so mutating one
    doesn't leak into the next call."""
    a = context_tasks_for_service("Follow-up visit")
    b = context_tasks_for_service("Follow-up visit")
    assert a is not b
    assert a[0] is not b[0]
    # Mutation of one doesn't affect the other.
    a[0].status = ContextTaskStatus.COMPLETED
    assert b[0].status == ContextTaskStatus.PENDING


# ── orchestrator construction ─────────────────────────


def test_for_service_returns_orchestrator_when_context_needed():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    assert o is not None
    assert o.service_name == "Follow-up visit"
    assert len(o.tasks) == 3


def test_for_service_returns_none_when_no_context_needed():
    o = ContextDiscoveryOrchestrator.for_service("Adult cleaning")
    assert o is None


# ── task progression ─────────────────────────────────


def test_current_task_starts_at_first():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    cur = o.current_task()
    assert cur.task_id == "original_procedure"


def test_complete_current_advances():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    nxt = o.complete_current("composite filling")
    assert nxt.task_id == "original_provider"
    # First task carries the answer.
    assert o.tasks["original_procedure"].result == "composite filling"
    assert o.tasks["original_procedure"].is_complete


def test_is_complete_false_until_all_done():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    assert o.is_complete() is False
    o.complete_current("filling")
    assert o.is_complete() is False
    o.complete_current("Dr. Chen")
    assert o.is_complete() is False
    o.complete_current("August 15th")
    assert o.is_complete() is True
    assert o.current_task() is None


def test_complete_current_when_all_done_is_safe():
    """After the last task, current_task() is None → complete_current
    should no-op silently, not crash."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("a")
    o.complete_current("b")
    o.complete_current("c")
    # No more tasks.
    result = o.complete_current("d")
    assert result is None


# ── regression tool ──────────────────────────────────


def test_regress_to_reopens_task():
    """LK pattern: caller changes their mind → regress_to resets
    the earlier task back to PENDING so orchestrator re-asks."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    # Caller changes mind about which procedure.
    o.regress_to(["original_procedure"])
    assert o.tasks["original_procedure"].status == ContextTaskStatus.PENDING
    assert o.tasks["original_procedure"].result is None
    # Second task stays complete because it wasn't regressed.
    assert o.tasks["original_provider"].is_complete
    # Current task is now the reopened one.
    assert o.current_task().task_id == "original_procedure"


def test_regress_to_multiple_tasks():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    o.complete_current("August 15th")
    assert o.is_complete()
    # Caller: 'wait actually the procedure AND the doctor were different'
    o.regress_to(["original_procedure", "original_provider"])
    assert not o.is_complete()
    # Two tasks reopened.
    assert o.tasks["original_procedure"].status == ContextTaskStatus.PENDING
    assert o.tasks["original_provider"].status == ContextTaskStatus.PENDING


def test_regress_to_unknown_task_id_silent():
    """Never raises on bad input."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.regress_to(["nonexistent_task"])   # silent no-op
    # No effect on real tasks.
    assert o.tasks["original_procedure"].is_complete


def test_regress_to_empty_list_noop():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.regress_to([])
    assert o.tasks["original_procedure"].is_complete


# ── visited_task_repr for LK regress_to tool description ──


def test_visited_task_repr_empty_before_any_completions():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    assert o.visited_task_repr() == {}


def test_visited_task_repr_populates_as_tasks_complete():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    v = o.visited_task_repr()
    assert "original_procedure" in v
    assert "original_provider" not in v  # not visited yet


def test_visited_task_repr_clears_on_regression():
    """Regressed task drops out of the visited set — LK behavior
    (can't regress to something not yet visited)."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    assert "original_procedure" in o.visited_task_repr()
    o.regress_to(["original_procedure"])
    assert "original_procedure" not in o.visited_task_repr()


# ── directive note for brain injection ─────────────


def test_as_directive_note_has_ask_prompt():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    note = o.as_directive_note()
    assert "follow-up to what" in note.lower()
    assert "do not proceed to booking" in note.lower()


def test_as_directive_note_advances_with_task():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    note1 = o.as_directive_note()
    o.complete_current("filling")
    note2 = o.as_directive_note()
    assert note1 != note2
    assert "which dentist" in note2.lower()


def test_as_directive_note_empty_when_all_done():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("a")
    o.complete_current("b")
    o.complete_current("c")
    assert o.as_directive_note() == ""


def test_as_directive_note_mentions_regression_when_tasks_visited():
    """After a task completes, note should hint that regression is
    available so the LLM knows callers may reference earlier answers."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    note = o.as_directive_note()
    assert "regression" in note.lower() or "already-answered" in note.lower()


# ── summary for humanness event ────────────────────


def test_to_summary_shape():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    s = o.to_summary()
    assert s["service_name"] == "Follow-up visit"
    assert s["task_count"] == 3
    assert s["visited_count"] == 0
    assert s["completed_count"] == 0
    assert s["current_task"] == "original_procedure"


def test_to_summary_after_progress():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    s = o.to_summary()
    assert s["visited_count"] == 1
    assert s["completed_count"] == 1
    assert s["current_task"] == "original_provider"
