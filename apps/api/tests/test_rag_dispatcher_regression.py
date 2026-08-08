"""Audit-3 P0-1 regression: RAG-in-Compose must not shadow vertical tools.

The bug (found in the 2026-08-04 external audit):
  ComposeHandler([rag_handler, clinic_handler]) routed by matching
  the string "unknown tool" in a handler's error message.  The
  LookupAnswerHandler's non-match error said "only handles
  lookup_answer" (no "unknown tool" substring), so ComposeHandler
  treated it as a real reply and never fell through to the vertical
  handler.  Result: enabling RAG silently disabled book_appointment,
  check_availability, lookup_faq, and escalate_to_human.

The fix:
  Both handlers declare TOOL_NAMES + can_handle(tool_name).
  ComposeHandler routes by that, not by error-string parsing.

This test verifies:
  1. Compose([rag, clinic]) with a clinic tool call reaches the clinic
     handler (the exact case the bug missed).
  2. Compose([rag, clinic]) with a lookup_answer call reaches the rag
     handler.
  3. Compose([rag, clinic]) with a tool no handler claims returns the
     structured "no handler matched" error.

Design note: the previous stub in tests returned "unknown tool" which
accidentally matched the dispatcher's string check.  These tests use
the REAL LookupAnswerHandler error message to prove the fix.
"""
from __future__ import annotations

import pytest

from packages.integrations.rag_tool import (
    ComposeHandler,
    LookupAnswerHandler,
)
from packages.schemas import ToolCall, ToolResult


class _StubClinicHandler:
    """Minimal clinic handler with a real can_handle method."""
    TOOL_NAMES = frozenset({
        "check_availability", "book_appointment",
        "lookup_faq", "escalate_to_human",
    })

    def __init__(self) -> None:
        self.calls_received: list[str] = []

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    async def __call__(self, call: ToolCall) -> ToolResult:
        self.calls_received.append(call.name)
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={"stub": True, "tool": call.name},
        )


class _StubRagHandler:
    """Real-shape RAG handler — TOOL_NAMES + can_handle + realistic
    error message ("only handles lookup_answer", NO 'unknown tool')."""
    TOOL_NAMES = frozenset({"lookup_answer"})

    def __init__(self) -> None:
        self.calls_received: list[str] = []

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES

    async def __call__(self, call: ToolCall) -> ToolResult:
        self.calls_received.append(call.name)
        if call.name != "lookup_answer":
            # Deliberately DOES NOT contain the string "unknown tool".
            # Before the fix, this bypassed dispatch fallback and became
            # the returned "result".
            return ToolResult(
                tool_call_id=call.id, name=call.name, result=None,
                error="LookupAnswerHandler only handles lookup_answer",
            )
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={"answer": "stub"},
        )


@pytest.mark.asyncio
async def test_rag_composed_with_clinic_reaches_clinic_for_booking():
    """The bug scenario: with RAG composed FIRST, a book_appointment call
    must still reach the clinic handler — not be intercepted by RAG."""
    rag = _StubRagHandler()
    clinic = _StubClinicHandler()
    compose = ComposeHandler([rag, clinic])

    call = ToolCall(
        id="tc-book", name="book_appointment",
        arguments={"caller_name": "Alice", "phone": "555-0001",
                   "service": "cleaning", "start_iso": "2026-08-06T10:00:00"},
    )
    result = await compose(call)

    assert result.error is None, \
        f"Compose returned error instead of routing to clinic: {result.error!r}"
    assert result.result == {"stub": True, "tool": "book_appointment"}
    assert clinic.calls_received == ["book_appointment"]
    assert rag.calls_received == [], \
        "RAG handler must not receive non-RAG tool calls when can_handle=False"


@pytest.mark.asyncio
async def test_rag_composed_with_clinic_still_serves_lookup_answer():
    """Sanity: lookup_answer still routes correctly to RAG."""
    rag = _StubRagHandler()
    clinic = _StubClinicHandler()
    compose = ComposeHandler([rag, clinic])

    call = ToolCall(id="tc-la", name="lookup_answer",
                    arguments={"question": "hours?"})
    result = await compose(call)

    assert result.result == {"answer": "stub"}
    assert rag.calls_received == ["lookup_answer"]
    assert clinic.calls_received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", [
    "check_availability", "book_appointment",
    "lookup_faq", "escalate_to_human",
])
async def test_all_clinic_tools_route_through_rag_compose(tool_name):
    """Every clinic tool must reach the clinic handler when RAG is
    composed in front.  Regression coverage for the audit's 'silent
    disable' scenario."""
    rag = _StubRagHandler()
    clinic = _StubClinicHandler()
    compose = ComposeHandler([rag, clinic])

    call = ToolCall(id="tc", name=tool_name, arguments={})
    result = await compose(call)

    assert result.error is None
    assert clinic.calls_received == [tool_name]


@pytest.mark.asyncio
async def test_unclaimed_tool_returns_no_handler_error():
    """A tool no one claims must return a structured error, not
    silently return the last handler's response."""
    rag = _StubRagHandler()
    clinic = _StubClinicHandler()
    compose = ComposeHandler([rag, clinic])

    call = ToolCall(id="tc", name="nonexistent_tool", arguments={})
    result = await compose(call)

    assert result.error is not None
    assert "no handler matched" in result.error
    assert result.result is None
    # Neither handler should have been invoked (they both returned
    # False from can_handle).
    assert clinic.calls_received == []
    assert rag.calls_received == []


@pytest.mark.asyncio
async def test_legacy_handler_without_can_handle_still_dispatches():
    """Back-compat: a handler with no can_handle method should still
    work via the legacy 'try then fall through on "unknown tool"' path."""

    class _LegacyHandler:
        """No can_handle, uses the 'unknown tool: <name>' error convention."""
        def __init__(self) -> None:
            self.received: list[str] = []

        async def __call__(self, call: ToolCall) -> ToolResult:
            self.received.append(call.name)
            if call.name != "legacy_only":
                return ToolResult(
                    tool_call_id=call.id, name=call.name, result=None,
                    error=f"unknown tool: {call.name}",
                )
            return ToolResult(tool_call_id=call.id, name=call.name,
                              result={"legacy_ok": True})

    legacy = _LegacyHandler()
    modern = _StubClinicHandler()
    compose = ComposeHandler([legacy, modern])

    # Modern-owned tool should still reach modern
    r1 = await compose(ToolCall(id="a", name="book_appointment", arguments={}))
    assert r1.result == {"stub": True, "tool": "book_appointment"}
    # Legacy's own tool works too
    r2 = await compose(ToolCall(id="b", name="legacy_only", arguments={}))
    assert r2.result == {"legacy_ok": True}


@pytest.mark.asyncio
async def test_first_claimer_wins_when_two_handlers_declare_same_tool():
    """If two modern handlers both claim a tool, the FIRST one in the
    list wins.  Predictable order matters for tenant overrides."""

    class _FirstHandler:
        TOOL_NAMES = frozenset({"lookup_faq"})
        def can_handle(self, name): return name in self.TOOL_NAMES
        async def __call__(self, call):
            return ToolResult(tool_call_id=call.id, name=call.name,
                              result={"by": "first"})

    class _SecondHandler:
        TOOL_NAMES = frozenset({"lookup_faq"})
        def can_handle(self, name): return name in self.TOOL_NAMES
        async def __call__(self, call):
            return ToolResult(tool_call_id=call.id, name=call.name,
                              result={"by": "second"})

    compose = ComposeHandler([_FirstHandler(), _SecondHandler()])
    r = await compose(ToolCall(id="x", name="lookup_faq", arguments={}))
    assert r.result == {"by": "first"}
