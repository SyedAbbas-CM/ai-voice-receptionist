"""Task #144 tests: discovery answers land in book_appointment(notes=).

Without this, the Christiaan follow-up scenario completes end-to-end
but front-desk staff sees a follow-up booking with empty notes — the
audit's whole 'False-complete follow-up' failure mode from §5 of the
clinic playbook.
"""
from __future__ import annotations

import pytest

from packages.dialogue.context_discovery import (
    ContextDiscoveryOrchestrator,
)


# ── collected_answers ─────────────────────────────────────


def test_collected_answers_empty_before_completions():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    assert o.collected_answers() == {}


def test_collected_answers_grows_as_tasks_complete():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("a filling")
    assert o.collected_answers() == {"original_procedure": "a filling"}
    o.complete_current("Dr. Chen")
    assert o.collected_answers() == {
        "original_procedure": "a filling",
        "original_provider": "Dr. Chen",
    }
    o.complete_current("August 15th")
    assert o.collected_answers() == {
        "original_procedure": "a filling",
        "original_provider": "Dr. Chen",
        "original_visit_date": "August 15th",
    }


def test_collected_answers_excludes_regressed_tasks():
    """When a task is regressed, its answer clears — it's no longer
    'collected' until re-answered."""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("filling")
    o.complete_current("Dr. Chen")
    o.regress_to(["original_procedure"])
    assert o.collected_answers() == {
        "original_provider": "Dr. Chen",
    }


# ── as_notes_prefix ──────────────────────────────────────


def test_notes_prefix_empty_before_completions():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    assert o.as_notes_prefix() == ""


def test_notes_prefix_partial_only_includes_present_fields():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("a filling")
    prefix = o.as_notes_prefix()
    assert prefix == "Follow-up to a filling"


def test_notes_prefix_full_reads_naturally():
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("a filling")
    o.complete_current("Dr. Chen")
    o.complete_current("August 15th")
    prefix = o.as_notes_prefix()
    assert prefix == "Follow-up to a filling with Dr. Chen on August 15th"


def test_notes_prefix_procedure_and_date_only():
    """Missing middle answer (provider) — reads as 'Follow-up to X on Y.'"""
    o = ContextDiscoveryOrchestrator.for_service("Follow-up visit")
    o.complete_current("root canal")
    # Skip provider by regressing it back to pending.
    # For this test, manually finalize the third task.
    o.tasks["original_visit_date"].status = (
        o.tasks["original_visit_date"].__class__.__annotations__.get(
            "status", None,
        )
    )
    # Direct set — the enum used by ContextTask.
    from packages.dialogue.context_discovery import ContextTaskStatus
    o.tasks["original_visit_date"].status = ContextTaskStatus.COMPLETED
    o.tasks["original_visit_date"].result = "August 15th"
    prefix = o.as_notes_prefix()
    assert "root canal" in prefix
    assert "August 15th" in prefix
    assert "Dr." not in prefix   # no provider


def test_notes_prefix_from_non_followup_service_empty():
    """Cleaning has no context tasks → for_service returns None →
    no orchestrator, no prefix."""
    o = ContextDiscoveryOrchestrator.for_service("Adult cleaning")
    assert o is None


# ── brain integration: augmenter runs before booking ─────


class _ScriptedLLM:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls_made = []
        self.tool_args_seen = []

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        self.calls_made.append(site)
        from apps.api.app.providers.base import LLMResponse
        from packages.schemas import ToolCall
        if not self.script:
            return LLMResponse(text="ok", tool_calls=[],
                                finish_reason="stop", raw={})
        item = self.script.pop(0)
        if isinstance(item, dict) and "tool" in item:
            tc = ToolCall(
                id=f"call_{len(self.calls_made)}",
                name=item["tool"],
                arguments=item.get("args", {}),
            )
            return LLMResponse(
                text="", tool_calls=[tc],
                finish_reason="tool_calls", raw={},
            )
        return LLMResponse(
            text=item if isinstance(item, str) else str(item),
            tool_calls=[], finish_reason="stop", raw={},
        )


async def _capturing_handler(call, captured):
    """Records the tool call's args and returns a stub receipt."""
    from packages.schemas import ToolResult
    captured.append({
        "name": call.name,
        "arguments": dict(call.arguments or {}),
    })
    return ToolResult(
        tool_call_id=call.id, name=call.name,
        result={"booked": True, "id": "evt_test"},
    )


def _brain(llm, tool_handler):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import (
        BusinessProfile, BusinessHours, ServiceOffering,
    )
    business = BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="",
            ),
        ],
    )
    return ReceptionistBrain(
        llm=llm, business=business,
        tools=build_clinic_tools(),
        tool_handler=tool_handler,
        extractor_llm=llm,
    )


@pytest.mark.asyncio
async def test_book_notes_augmented_when_discovery_answers_stashed():
    """State has _discovery_notes_prefix populated → LLM calls
    book_appointment(notes='') → brain augments notes with prefix
    before dispatch → tool_handler sees the prefix in args."""
    from packages.schemas import CallState
    captured = []
    async def _handler(call):
        return await _capturing_handler(call, captured)
    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
            # notes intentionally omitted — augmenter should fill.
        }},
        "You're booked.",
    ])
    brain = _brain(llm, _handler)
    state = CallState(session_id="CAaug1", business_id="biz1")
    # Simulate a completed discovery orchestrator's teardown.
    state._discovery_notes_prefix = (
        "Follow-up to a filling with Dr. Chen on August 15th"
    )
    # Bypass write-guard for this test (validate_write requires
    # transcript context; here we're testing augmentation only).
    # Patch validate_write at the write_guard module level — brain
    # imports it locally per call site so we need to patch the
    # underlying module, not the re-export.
    from packages.core_agent.classifiers import write_guard as _wg
    orig_validate = _wg.validate_write

    async def _ok(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg.validate_write = _ok
    try:
        await brain.handle_user_turn(state, "book me a follow-up")
    finally:
        _wg.validate_write = orig_validate
    # Booking tool handler saw the augmented notes.
    booking_calls = [
        c for c in captured if c["name"] == "book_appointment"
    ]
    assert booking_calls
    notes = booking_calls[0]["arguments"].get("notes", "")
    assert "Follow-up to a filling" in notes
    assert "Dr. Chen" in notes
    assert "August 15th" in notes


@pytest.mark.asyncio
async def test_book_existing_notes_preserved_after_prefix():
    """LLM-supplied notes are NOT clobbered — prefix goes first, LLM
    notes preserved after."""
    from packages.schemas import CallState
    captured = []
    async def _handler(call):
        return await _capturing_handler(call, captured)
    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
            "notes": "Caller mentioned tenderness at the site.",
        }},
        "Booked.",
    ])
    brain = _brain(llm, _handler)
    state = CallState(session_id="CAaug2", business_id="biz1")
    state._discovery_notes_prefix = (
        "Follow-up to filling with Dr. Chen on Aug 15"
    )
    from packages.core_agent.classifiers import write_guard as _wg
    orig = _wg.validate_write
    async def _ok(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg.validate_write = _ok
    try:
        await brain.handle_user_turn(state, "book me a follow-up")
    finally:
        _wg.validate_write = orig
    notes = [
        c["arguments"].get("notes", "")
        for c in captured if c["name"] == "book_appointment"
    ][0]
    # Both present, prefix first.
    assert "Follow-up to filling with Dr. Chen" in notes
    assert "Caller mentioned tenderness" in notes
    assert notes.index("Follow-up to filling") < notes.index("tenderness")


@pytest.mark.asyncio
async def test_book_no_augment_when_no_discovery_prefix():
    """Regular booking (no follow-up, no orchestrator ran) → notes
    stay whatever the LLM sent."""
    from packages.schemas import CallState
    captured = []
    async def _handler(call):
        return await _capturing_handler(call, captured)
    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
            "notes": "just a routine cleaning",
        }},
        "Booked.",
    ])
    brain = _brain(llm, _handler)
    state = CallState(session_id="CAaug3", business_id="biz1")
    # No _discovery_notes_prefix on state.
    from packages.core_agent.classifiers import write_guard as _wg
    orig = _wg.validate_write
    async def _ok(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg.validate_write = _ok
    try:
        await brain.handle_user_turn(state, "book cleaning")
    finally:
        _wg.validate_write = orig
    notes = [
        c["arguments"].get("notes", "")
        for c in captured if c["name"] == "book_appointment"
    ][0]
    assert notes == "just a routine cleaning"
    assert "Follow-up to" not in notes


@pytest.mark.asyncio
async def test_prefix_consumed_after_augment_not_reapplied():
    """After the augmenter fires, state._discovery_notes_prefix
    should be cleared so a retry within the same turn doesn't
    double-apply it."""
    from packages.schemas import CallState
    captured = []
    async def _handler(call):
        return await _capturing_handler(call, captured)
    llm = _ScriptedLLM(script=[
        {"tool": "book_appointment", "args": {
            "caller_name": "Abbas",
            "phone": "+15551234567",
            "service": "Follow-up visit",
            "start_iso": "2026-09-02T10:00",
        }},
        "Booked.",
    ])
    brain = _brain(llm, _handler)
    state = CallState(session_id="CAaug4", business_id="biz1")
    state._discovery_notes_prefix = "Follow-up to X"
    from packages.core_agent.classifiers import write_guard as _wg
    orig = _wg.validate_write
    async def _ok(*a, **k):
        class _V:
            approved = True
            reason = ""
            detail = ""
        return _V()
    _wg.validate_write = _ok
    try:
        await brain.handle_user_turn(state, "book me a follow-up")
    finally:
        _wg.validate_write = orig
    # Prefix consumed.
    assert getattr(state, "_discovery_notes_prefix", "") == ""
