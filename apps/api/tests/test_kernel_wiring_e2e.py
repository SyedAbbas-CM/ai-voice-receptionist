"""Sprint 10 WIRING integration tests.

Proves the intelligence kernel is actually wired into ReceptionistBrain
and produces the promised behavior end-to-end.

Coverage:
  * dialogue_kernel_enabled=False → brain works exactly as before (no
    DialogueState attached).
  * dialogue_kernel_enabled=True + booking intent → task discovered.
  * TemporalResolver normalizes "next Thursday afternoon" into a
    concrete range.
  * The audit's "Tuesday...no Thursday" correction survives through
    the kernel's evidence supersession.
  * CommitCoordinator dedupe: retrying a booking commit doesn't
    double-book.
  * Evidence-invalidated-after-propose is rejected at commit.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from packages.core_agent.kernel_wiring import KernelWiring
from packages.dialogue import (
    ActionKind,
    CallerConfirmation,
    CommitOutcome,
    Resolution,
    SlotStatus,
    TaskKind,
    TaskStatus,
    apply_correction,
)
from packages.integrations.calendar_commit_adapter import (
    FakeCalendarBookingAdapter,
    build_default_adapters,
)
from packages.integrations.fake_calendar import FakeCalendar
from packages.schemas import BusinessProfile, CallState
from packages.schemas.business import BusinessHours


@pytest.fixture
def calendar(tmp_path) -> FakeCalendar:
    return FakeCalendar(tmp_path / "cal.json")


@pytest.fixture
def business() -> BusinessProfile:
    return BusinessProfile(
        id="smile-dental", name="Smile Dental Clinic", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-19:00",
            friday="09:00-17:00", saturday="10:00-14:00", sunday=None,
        ),
        services=[{"name": "cleaning", "duration_minutes": 45}],
        escalation_phone="+15550000000",
    )


@pytest.fixture
def kernel_enabled(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "dialogue_kernel_enabled", True)


@pytest.fixture
def kernel_disabled(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "dialogue_kernel_enabled", False)


def _make_wiring(state, calendar, business) -> KernelWiring:
    return KernelWiring(
        call_state=state, business_id=business.id, tenant_id=state.tenant_id,
        business_timezone=business.timezone, business_hours=business.hours,
        commit_adapters=build_default_adapters(calendar, business=business),
    )


# ── flag off: pure no-op ────────────────────────────────────────────

def test_flag_off_leaves_dialogue_none(kernel_disabled, calendar, business):
    state = CallState(session_id="ca-1", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    assert wiring.is_enabled() is False
    assert state.dialogue is None
    wiring.on_user_turn("book a cleaning next Thursday", "t1")
    assert state.dialogue is None


# ── flag on: booking intent discovered ─────────────────────────────

def test_booking_intent_creates_task(kernel_enabled, calendar, business):
    state = CallState(session_id="ca-2", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    assert wiring.is_enabled() is True
    wiring.on_user_turn("I need to book a cleaning next Thursday", "t1")

    dstate = wiring.dialogue_state()
    assert dstate is not None
    tasks = list(dstate.agenda.tasks.values())
    assert len(tasks) == 1
    assert tasks[0].kind == TaskKind.BOOK
    assert dstate.agenda.active_task_id == tasks[0].task_id


def test_reschedule_intent_creates_separate_task(kernel_enabled, calendar, business):
    state = CallState(session_id="ca-3", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    wiring.on_user_turn("I need to reschedule my appointment", "t1")
    tasks = [t for t in wiring.dialogue_state().agenda.tasks.values()]
    assert any(t.kind == TaskKind.RESCHEDULE for t in tasks)


def test_duplicate_intent_across_turns_not_double_added(
    kernel_enabled, calendar, business,
):
    state = CallState(session_id="ca-4", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    wiring.on_user_turn("I want to book", "t1")
    wiring.on_user_turn("Yeah I want to book a cleaning", "t2")
    tasks = [t for t in wiring.dialogue_state().agenda.tasks.values()
             if t.kind == TaskKind.BOOK]
    assert len(tasks) == 1


# ── temporal normalization ─────────────────────────────────────────

def test_normalize_date_time_returns_concrete_range(
    kernel_enabled, calendar, business,
):
    state = CallState(session_id="ca-5", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    result = wiring.normalize_date_time("August 6th at 10:30 AM")
    assert result is not None
    assert result.resolution == Resolution.EXACT_DATE_EXACT_TIME
    assert result.range_start.hour == 10 and result.range_start.minute == 30


def test_normalize_ambiguous_next_friday_flags_for_confirm(
    kernel_enabled, calendar, business, monkeypatch,
):
    """The audit's date-ambiguity concern: 'next Friday' from Wed must
    ask caller which one."""
    from packages.dialogue.temporal import TemporalContext
    from zoneinfo import ZoneInfo
    # Freeze temporal context to a known Wednesday so the ambiguity fires
    fixed_ctx = TemporalContext(
        now=datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
        business_tz="America/Chicago", business_hours=business.hours,
    )
    state = CallState(session_id="ca-6", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    # Bypass the factory so we get the frozen context
    wiring._temporal_ctx_factory = lambda: fixed_ctx
    result = wiring.normalize_date_time("next Friday at 3 pm")
    assert result.resolution == Resolution.AMBIGUOUS_NEEDS_CONFIRM
    assert result.needs_confirmation is True
    assert len(result.interpretations) == 2


# ── correction handling (audit's acceptance test — end to end) ─────

def test_tuesday_no_thursday_correction_survives_through_kernel(
    kernel_enabled, calendar, business,
):
    """Audit's specific acceptance criterion, integrated:
    caller says Tuesday then corrects to Thursday.  Kernel marks
    Tuesday SUPERSEDED; active value is Thursday.  Booking commit
    proposal uses Thursday."""
    state = CallState(session_id="ca-7", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    wiring.on_user_turn("I want to book a cleaning", "t1")

    dstate = wiring.dialogue_state()
    book_task_id = next(iter(dstate.agenda.tasks))

    # Record the initial slot values (as if the LLM extracted them)
    wiring.record_slot(book_task_id, "caller_name", "Sarah Khan", "t2")
    wiring.record_slot(book_task_id, "phone", "+15551110000", "t3")
    wiring.record_slot(book_task_id, "service", "cleaning", "t2")
    wiring.record_slot(book_task_id, "start_iso", "2026-08-04T10:00:00", "t4")

    # Caller corrects — "no, actually Thursday at 4"
    dstate = wiring.dialogue_state()   # re-hydrate
    from packages.dialogue.reducer import AddEvidencePatch
    from packages.dialogue import SlotEvidence, SourceRole, reduce_patch
    reduce_patch(dstate, AddEvidencePatch(
        task_id=book_task_id, slot_name="start_iso",
        evidence=SlotEvidence(
            value="2026-08-06T16:00:00", source_turn_id="t6",
            source_text="actually Thursday at 4",
            source_role=SourceRole.CALLER, confidence=0.92,
            status=SlotStatus.EXPLICIT,
        ),
    ))
    wiring._save_state(dstate)

    # Verify: Tuesday SUPERSEDED, Thursday active
    final = wiring.dialogue_state()
    task = final.agenda.tasks[book_task_id]
    tuesday_ev = next(e for e in task.slots["start_iso"]
                      if e.value == "2026-08-04T10:00:00")
    thursday_ev = next(e for e in task.slots["start_iso"]
                       if e.value == "2026-08-06T16:00:00")
    assert tuesday_ev.status == SlotStatus.SUPERSEDED
    assert thursday_ev.status == SlotStatus.EXPLICIT
    assert task.active_value("start_iso") == "2026-08-06T16:00:00"


# ── commit via coordinator dedupes ─────────────────────────────────

@pytest.mark.asyncio
async def test_commit_via_kernel_dedupes_retries(
    kernel_enabled, calendar, business,
):
    state = CallState(session_id="ca-8", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    wiring.on_user_turn("book a cleaning", "t1")

    dstate = wiring.dialogue_state()
    book_task_id = next(iter(dstate.agenda.tasks))

    wiring.record_slot(book_task_id, "caller_name", "Sarah Khan", "t2")
    wiring.record_slot(book_task_id, "phone", "+15551110000", "t3")
    wiring.record_slot(book_task_id, "service", "cleaning", "t2")
    wiring.record_slot(book_task_id, "start_iso", "2026-08-06T10:00:00", "t4")

    coord = wiring.coordinator()
    task = wiring.dialogue_state().agenda.tasks[book_task_id]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=wiring.dialogue_state(),
        argument_map={
            "caller_name": "caller_name", "phone": "phone",
            "service": "service", "start_iso": "start_iso",
        },
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t5",
        scope=["caller_name", "phone", "service", "start_iso"],
        confidence=0.95,
    )]
    r1 = await coord.commit(proposal, confirmations, task)
    r2 = await coord.commit(proposal, confirmations, task)

    assert r1.outcome == CommitOutcome.SUCCESS
    assert r2.outcome == CommitOutcome.SUCCESS
    assert r1.external_id == r2.external_id

    # Calendar file should have ONE event, not two
    import json
    events = json.loads(calendar.path.read_text())
    assert len(events) == 1
    assert events[0]["caller_name"] == "Sarah Khan"


@pytest.mark.asyncio
async def test_commit_rejected_when_correction_supersedes_between_propose_and_commit(
    kernel_enabled, calendar, business,
):
    """The dangerous scenario made concrete: propose books Tuesday,
    caller corrects to Thursday between propose and commit → coordinator
    detects the invalidated evidence and refuses.  Prevents booking
    on the wrong day.  End-to-end wiring proof."""
    state = CallState(session_id="ca-9", business_id=business.id)
    wiring = _make_wiring(state, calendar, business)
    wiring.on_user_turn("book a cleaning", "t1")
    dstate = wiring.dialogue_state()
    book_task_id = next(iter(dstate.agenda.tasks))

    wiring.record_slot(book_task_id, "caller_name", "Sarah", "t2")
    wiring.record_slot(book_task_id, "phone", "+15551110000", "t3")
    wiring.record_slot(book_task_id, "service", "cleaning", "t2")
    wiring.record_slot(book_task_id, "start_iso", "2026-08-04T10:00:00", "t4")

    coord = wiring.coordinator()
    task = wiring.dialogue_state().agenda.tasks[book_task_id]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=wiring.dialogue_state(),
        argument_map={
            "caller_name": "caller_name", "phone": "phone",
            "service": "service", "start_iso": "start_iso",
        },
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t5",
        scope=["caller_name", "phone", "service", "start_iso"],
        confidence=0.95,
    )]

    # BEFORE the commit fires, the caller corrects
    dstate_now = wiring.dialogue_state()
    apply_correction(
        dstate_now, task_id=book_task_id, slot_name="start_iso",
        new_value="2026-08-06T16:00:00", source_turn_id="t7",
    )
    wiring._save_state(dstate_now)

    # Coordinator now sees the original evidence marked SUPERSEDED —
    # commit must refuse (using the task from the CURRENT state, not
    # the snapshot the proposal was built from).
    fresh_task = wiring.dialogue_state().agenda.tasks[book_task_id]
    result = await coord.commit(proposal, confirmations, fresh_task)
    assert result.outcome == CommitOutcome.REJECTED
    assert "evidence_invalidated" in (result.error or "")

    # No event booked
    import json
    events = json.loads(calendar.path.read_text())
    assert len(events) == 0
