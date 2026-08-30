"""Task #151 tests: answer_context_task + regress_context_tasks tools.

Closes the discovery loop from task #150. Without these, brain
directs the LLM to ask the discovery question but has no mechanism
to advance the orchestrator when the caller answers — LLM would
loop forever.
"""
from __future__ import annotations

import pytest

from packages.dialogue.context_discovery import (
    ContextDiscoveryOrchestrator,
    build_discovery_tools,
    handle_discovery_tool_call,
)


# ── build_discovery_tools ─────────────────────────────────────


def test_no_tools_when_orchestrator_none():
    assert build_discovery_tools(None) == []


def test_no_tools_when_orchestrator_complete():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    o.complete_current("August 15th")
    assert o.is_complete()
    assert build_discovery_tools(o) == []


def test_answer_tool_present_when_active():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    tools = build_discovery_tools(o)
    names = [t.name for t in tools]
    assert "answer_context_task" in names


def test_regress_tool_not_present_when_no_tasks_visited():
    """LK parity: out_of_scope tool is only injected once at least
    one task is complete.  Fresh orchestrator has nothing to regress
    to."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    tools = build_discovery_tools(o)
    names = [t.name for t in tools]
    assert "regress_context_tasks" not in names


def test_regress_tool_present_after_one_completion():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    tools = build_discovery_tools(o)
    names = [t.name for t in tools]
    assert "regress_context_tasks" in names


def test_answer_tool_description_mentions_current_task_id():
    """Discovery directive tells LLM the current task_id; the tool
    description also carries it as reinforcement."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    tools = build_discovery_tools(o)
    answer_tool = next(
        t for t in tools if t.name == "answer_context_task"
    )
    assert "original_procedure" in answer_tool.description


def test_regress_tool_enum_lists_visited_task_ids():
    """The task_ids enum in the schema restricts LLM to visited
    tasks only (LK's out_of_scope pattern)."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    tools = build_discovery_tools(o)
    regress_tool = next(
        t for t in tools if t.name == "regress_context_tasks"
    )
    # Verify schema has enum with the visited task IDs.
    enum = (
        regress_tool.parameters["properties"]["task_ids"]
        ["items"]["enum"]
    )
    assert "original_procedure" in enum
    assert "original_provider" in enum
    # Third task not yet visited.
    assert "original_visit_date" not in enum


# ── handle_discovery_tool_call ──────────────────────────────


def test_answer_advances_orchestrator():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    receipt = handle_discovery_tool_call(
        o, "answer_context_task", {"answer": "a filling"},
    )
    assert receipt["ok"] is True
    assert receipt["task_id"] == "original_procedure"
    assert receipt["answer"] == "a filling"
    assert receipt["next_task_id"] == "original_provider"
    assert receipt["discovery_complete"] is False
    # Orchestrator actually advanced.
    assert o.current_task().task_id == "original_provider"
    assert o.tasks["original_procedure"].result == "a filling"


def test_answer_marks_discovery_complete_on_last_task():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    receipt = handle_discovery_tool_call(
        o, "answer_context_task", {"answer": "August 15th"},
    )
    assert receipt["discovery_complete"] is True
    assert receipt["next_task_id"] is None
    assert o.is_complete()


def test_answer_empty_string_returns_error():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    receipt = handle_discovery_tool_call(
        o, "answer_context_task", {"answer": ""},
    )
    assert receipt["ok"] is False
    # Orchestrator unchanged.
    assert o.current_task().task_id == "original_procedure"


def test_answer_missing_argument_returns_error():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    receipt = handle_discovery_tool_call(
        o, "answer_context_task", {},
    )
    assert receipt["ok"] is False


def test_regress_reopens_named_tasks():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    receipt = handle_discovery_tool_call(
        o, "regress_context_tasks",
        {"task_ids": ["original_procedure"]},
    )
    assert receipt["ok"] is True
    assert receipt["next_task_id"] == "original_procedure"


def test_regress_bad_task_ids_type_returns_error():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    receipt = handle_discovery_tool_call(
        o, "regress_context_tasks",
        {"task_ids": "not a list"},
    )
    assert receipt["ok"] is False


def test_unknown_tool_name_returns_none():
    """Non-discovery tool → return None so brain falls through to
    the real tool_handler."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    receipt = handle_discovery_tool_call(
        o, "book_appointment", {"caller_name": "X"},
    )
    assert receipt is None


def test_none_orchestrator_returns_none():
    receipt = handle_discovery_tool_call(
        None, "answer_context_task", {"answer": "x"},
    )
    assert receipt is None


# ── directive mentions the mandatory tool call ────────────


def test_directive_mentions_answer_tool_by_name():
    """Discovery directive should instruct LLM to call
    answer_context_task, not just ask + wait."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    directive = o.as_directive_note()
    assert "answer_context_task" in directive
    assert "MANDATORY" in directive or "call" in directive.lower()


def test_directive_mentions_regress_when_tasks_visited():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    directive = o.as_directive_note()
    assert "regress_context_tasks" in directive
