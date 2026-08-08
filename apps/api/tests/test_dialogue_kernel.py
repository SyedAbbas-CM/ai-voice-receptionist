"""Sprint 10 Track A: Conversation State Kernel tests.

Coverage:
  * Evidence-backed slots — active_evidence picks the most recent
    non-superseded value.
  * Correction handling — "Tuesday, no, Thursday" leaves Tuesday
    SUPERSEDED and Thursday EXPLICIT. (Audit's specific acceptance test.)
  * Task graph — multi-intent scenarios track multiple tasks.
  * TaskStatus transitions gated by TASK_TRANSITIONS.
  * ConfirmAction / RecordCommit lifecycle.
  * Escalation flag propagation.
  * SemanticPlan invariants (validator enforces per-operation rules).

All tests are pure — no LLM, no I/O.  Kernel is deterministic.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.dialogue import (
    ConversationAgenda,
    DialogueState,
    DeliveryIntent,
    PatchRejected,
    PlannedFact,
    PlannedQuestion,
    PlanOperation,
    Reducer,
    SemanticPlan,
    SlotEvidence,
    SlotStatus,
    TaskKind,
    TaskState,
    TaskStatus,
    apply_correction,
    reduce_patch,
)
from packages.dialogue.reducer import (
    AddEvidencePatch,
    AddTaskPatch,
    DeferTaskPatch,
    EscalatePatch,
    RecordCommitPatch,
    RejectEvidencePatch,
    SetActiveTaskPatch,
    TransitionTaskPatch,
)
from packages.dialogue.state import SourceRole


def _fresh_state() -> DialogueState:
    return DialogueState(
        call_id="CA-test", tenant_id="acme", business_id="biz-1",
    )


def _ev(value, turn, role=SourceRole.CALLER, status=SlotStatus.EXPLICIT, conf=0.9):
    return SlotEvidence(
        value=value, source_turn_id=turn, source_text=f"turn:{turn}",
        source_role=role, confidence=conf, status=status,
    )


# ── SlotEvidence + TaskState basics ──────────────────────────────────

def test_active_evidence_returns_most_recent_non_superseded():
    task = TaskState(
        task_id="t1", kind=TaskKind.BOOK,
        required_slots=["start_iso", "caller_name"],
    )
    task.slots["start_iso"] = [
        _ev("2026-08-05T10:00:00", "turn_1", status=SlotStatus.EXPLICIT),
        _ev("2026-08-06T14:00:00", "turn_3", status=SlotStatus.EXPLICIT),
    ]
    assert task.active_value("start_iso") == "2026-08-06T14:00:00"


def test_active_evidence_skips_superseded():
    task = TaskState(task_id="t1", kind=TaskKind.BOOK)
    task.slots["service"] = [
        _ev("cleaning", "turn_1", status=SlotStatus.SUPERSEDED),
        _ev("crown", "turn_2", status=SlotStatus.EXPLICIT),
    ]
    assert task.active_value("service") == "crown"


def test_active_evidence_skips_rejected():
    task = TaskState(task_id="t1", kind=TaskKind.BOOK)
    task.slots["phone"] = [
        _ev("+15550001111", "turn_2", status=SlotStatus.REJECTED),
        _ev("+15559998888", "turn_4", status=SlotStatus.EXPLICIT),
    ]
    assert task.active_value("phone") == "+15559998888"


def test_missing_slots_reports_uncollected_required():
    task = TaskState(
        task_id="t1", kind=TaskKind.BOOK,
        required_slots=["caller_name", "phone", "service", "start_iso"],
    )
    task.slots["caller_name"] = [_ev("Sarah", "turn_2")]
    task.slots["service"] = [_ev("cleaning", "turn_2")]
    assert set(task.missing_slots()) == {"phone", "start_iso"}


def test_is_ready_to_commit_requires_confirmed_by_default():
    task = TaskState(
        task_id="t1", kind=TaskKind.BOOK,
        required_slots=["service", "start_iso"],
    )
    task.slots["service"] = [_ev("cleaning", "turn_1", status=SlotStatus.EXPLICIT)]
    task.slots["start_iso"] = [_ev("2026-08-06T10:00", "turn_1", status=SlotStatus.EXPLICIT)]
    # EXPLICIT is not enough for commit
    assert not task.is_ready_to_commit()
    # But is_ready_to_commit(require_confirmation=False) accepts EXPLICIT
    assert task.is_ready_to_commit(require_confirmation=False)


def test_is_ready_to_commit_true_when_all_confirmed():
    task = TaskState(
        task_id="t1", kind=TaskKind.BOOK,
        required_slots=["service"],
    )
    task.slots["service"] = [_ev("cleaning", "turn_1", status=SlotStatus.CONFIRMED)]
    assert task.is_ready_to_commit()


# ── the audit's specific acceptance test ─────────────────────────────

def test_correction_supersedes_prior_slot_no_commit_until_confirmed():
    """Audit acceptance criterion (verbatim):

        'Tuesday at ten — no, scratch that, Thursday at four.'
        leaves Tuesday marked superseded, Thursday marked explicit,
        and no booking occurs until confirmation.
    """
    state = _fresh_state()

    # Task starts on turn 1 (caller asks to book)
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["start_iso"],
    ))

    # Turn 3: caller says "Tuesday at ten"
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="start_iso",
        evidence=_ev("2026-08-04T10:00:00", "turn_3"),
    ))
    task = state.agenda.tasks["book_1"]
    assert task.active_value("start_iso") == "2026-08-04T10:00:00"
    assert not task.is_ready_to_commit()   # never confirmed

    # Turn 4: caller corrects — "no, scratch that, Thursday at four"
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="start_iso",
        evidence=_ev("2026-08-06T16:00:00", "turn_4"),
    ))
    task = state.agenda.tasks["book_1"]

    # Assertion 1: Tuesday evidence marked SUPERSEDED
    tuesday = next(
        e for e in task.slots["start_iso"]
        if e.value == "2026-08-04T10:00:00"
    )
    assert tuesday.status == SlotStatus.SUPERSEDED

    # Assertion 2: Thursday evidence is EXPLICIT (fresh, not yet confirmed)
    thursday = next(
        e for e in task.slots["start_iso"]
        if e.value == "2026-08-06T16:00:00"
    )
    assert thursday.status == SlotStatus.EXPLICIT

    # Assertion 3: active value is Thursday
    assert task.active_value("start_iso") == "2026-08-06T16:00:00"

    # Assertion 4: no commit possible — needs CONFIRMED status
    assert not task.is_ready_to_commit()


def test_apply_correction_helper_matches_manual_patch():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=["service"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="service",
        evidence=_ev("cleaning", "turn_2"),
    ))
    # Use the convenience helper for the correction
    apply_correction(
        state, task_id="t", slot_name="service",
        new_value="crown", source_turn_id="turn_4",
        source_text="Actually a crown",
    )
    task = state.agenda.tasks["t"]
    assert task.active_value("service") == "crown"
    superseded = [
        e for e in task.slots["service"]
        if e.status == SlotStatus.SUPERSEDED
    ]
    assert len(superseded) == 1 and superseded[0].value == "cleaning"


# ── rejection ──────────────────────────────────────────────────────────

def test_reject_evidence_marks_it_rejected():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=["phone"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="phone",
        evidence=_ev("+15550009999", "turn_3"),
    ))
    reduce_patch(state, RejectEvidencePatch(
        task_id="t", slot_name="phone", source_turn_id="turn_3",
    ))
    task = state.agenda.tasks["t"]
    assert task.active_value("phone") is None
    assert task.slots["phone"][0].status == SlotStatus.REJECTED


def test_reject_evidence_raises_when_turn_not_found():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    with pytest.raises(PatchRejected, match="evidence_not_found"):
        reduce_patch(state, RejectEvidencePatch(
            task_id="t", slot_name="phone", source_turn_id="nonexistent",
        ))


# ── task transitions ─────────────────────────────────────────────────

def test_valid_transition_discovered_to_collecting():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    reduce_patch(state, TransitionTaskPatch(
        task_id="t", to_status=TaskStatus.COLLECTING,
    ))
    assert state.agenda.tasks["t"].status == TaskStatus.COLLECTING


def test_invalid_transition_rejected():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    # DISCOVERED -> COMMITTING is not allowed (must go through
    # COLLECTING -> READY_TO_PROPOSE -> AWAITING_CONFIRMATION)
    with pytest.raises(PatchRejected, match="invalid_transition"):
        reduce_patch(state, TransitionTaskPatch(
            task_id="t", to_status=TaskStatus.COMMITTING,
        ))


def test_completed_task_leaves_active_slot_empty():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    for status in (TaskStatus.COLLECTING, TaskStatus.READY_TO_PROPOSE,
                   TaskStatus.AWAITING_CONFIRMATION, TaskStatus.COMMITTING):
        reduce_patch(state, TransitionTaskPatch(task_id="t", to_status=status))
    reduce_patch(state, RecordCommitPatch(
        task_id="t", action_id="ext_evt_123",
    ))
    assert state.agenda.active_task_id is None
    assert "t" in state.agenda.completed_task_ids
    assert state.agenda.tasks["t"].committed_action_id == "ext_evt_123"


def test_record_commit_requires_committing_status():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    # Skip to READY_TO_PROPOSE without going through COMMITTING
    reduce_patch(state, TransitionTaskPatch(task_id="t", to_status=TaskStatus.COLLECTING))
    reduce_patch(state, TransitionTaskPatch(task_id="t", to_status=TaskStatus.READY_TO_PROPOSE))
    with pytest.raises(PatchRejected, match="commit_from_wrong_status"):
        reduce_patch(state, RecordCommitPatch(task_id="t", action_id="x"))


# ── task graph / multi-intent ────────────────────────────────────────

def test_multi_intent_book_plus_faq_plus_claim():
    """The exact adversarial scenario the audit called out:
    'I need to book a cleaning, does Delta cover fillings, and did my
    last claim go through?' — three tasks."""
    state = _fresh_state()

    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["service", "start_iso"],
    ))
    reduce_patch(state, AddTaskPatch(
        task_id="faq_1", task_kind=TaskKind.FAQ,
        required_slots=["question_topic"],
        make_active=False,
    ))
    reduce_patch(state, AddTaskPatch(
        task_id="claim_1", task_kind=TaskKind.HANDOFF,
        required_slots=[], make_active=False,
    ))

    assert state.agenda.active_task_id == "book_1"
    assert state.agenda.open_task_count() == 3
    assert len(state.all_open_tasks()) == 3


def test_defer_task_moves_to_deferred_list():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="faq_1", task_kind=TaskKind.FAQ, required_slots=[],
    ))
    reduce_patch(state, DeferTaskPatch(task_id="faq_1"))
    assert state.agenda.tasks["faq_1"].status == TaskStatus.DEFERRED
    assert "faq_1" in state.agenda.deferred_task_ids
    assert state.agenda.active_task_id is None


def test_set_active_pulls_task_out_of_deferred():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t1", task_kind=TaskKind.FAQ, required_slots=[],
    ))
    reduce_patch(state, DeferTaskPatch(task_id="t1"))
    reduce_patch(state, SetActiveTaskPatch(task_id="t1"))
    assert state.agenda.active_task_id == "t1"
    assert "t1" not in state.agenda.deferred_task_ids


def test_cannot_defer_completed_task():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    for s in (TaskStatus.COLLECTING, TaskStatus.READY_TO_PROPOSE,
              TaskStatus.AWAITING_CONFIRMATION, TaskStatus.COMMITTING):
        reduce_patch(state, TransitionTaskPatch(task_id="t", to_status=s))
    reduce_patch(state, RecordCommitPatch(task_id="t", action_id="x"))
    with pytest.raises(PatchRejected, match="cannot_defer_terminal"):
        reduce_patch(state, DeferTaskPatch(task_id="t"))


# ── escalation ───────────────────────────────────────────────────────

def test_escalate_sets_top_level_flag():
    state = _fresh_state()
    reduce_patch(state, EscalatePatch(reason="caller asked for manager"))
    assert state.escalated is True
    assert state.escalation_reason == "caller asked for manager"


# ── reducer determinism ──────────────────────────────────────────────

def test_replay_produces_identical_state():
    """Applying the same patch sequence to a fresh state should yield
    an identical final state (modulo evidence timestamps, which come
    from an injected clock)."""
    counter = iter(range(1, 1000))
    fake_clock = lambda: next(counter)

    def build():
        s = _fresh_state()
        r = Reducer(clock_ns=fake_clock)
        r.apply(s, AddTaskPatch(task_id="t", task_kind=TaskKind.BOOK,
                                required_slots=["service"]))
        r.apply(s, AddEvidencePatch(
            task_id="t", slot_name="service",
            evidence=_ev("cleaning", "turn_1"),
        ))
        r.apply(s, AddEvidencePatch(
            task_id="t", slot_name="service",
            evidence=_ev("crown", "turn_3"),
        ))
        return s

    counter = iter(range(1, 1000))
    s1 = build()
    counter = iter(range(1, 1000))
    s2 = build()
    assert s1.model_dump() == s2.model_dump()


def test_add_task_with_duplicate_id_rejected():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=[],
    ))
    with pytest.raises(PatchRejected, match="task_exists"):
        reduce_patch(state, AddTaskPatch(
            task_id="t", task_kind=TaskKind.FAQ, required_slots=[],
        ))


# ── SemanticPlan invariants ──────────────────────────────────────────

def test_semantic_plan_confirm_action_requires_active_task():
    with pytest.raises(ValidationError, match="active_task_id"):
        SemanticPlan(operation=PlanOperation.CONFIRM_ACTION)


def test_semantic_plan_ask_slot_requires_question():
    with pytest.raises(ValidationError, match="question"):
        SemanticPlan(operation=PlanOperation.ASK_SLOT)


def test_semantic_plan_answer_faq_requires_facts():
    with pytest.raises(ValidationError, match="requires at least one"):
        SemanticPlan(operation=PlanOperation.ANSWER_FAQ)


def test_semantic_plan_forbidden_claim_contradicts_stated_fact_rejected():
    with pytest.raises(ValidationError, match="contradicts"):
        SemanticPlan(
            operation=PlanOperation.ANSWER_FAQ,
            facts=[PlannedFact(claim="We accept Delta Dental",
                               source="business_profile:insurance")],
            forbidden_claims=["We accept Delta Dental"],
        )


def test_semantic_plan_valid_confirm_action():
    plan = SemanticPlan(
        operation=PlanOperation.CONFIRM_ACTION,
        active_task_id="book_1",
        facts=[PlannedFact(
            claim="Booked Sarah for cleaning Thursday August 6 at 10:30 AM",
            source="tool:booking_result_abc",
            critical=True,
        )],
        forbidden_claims=["Your insurance will cover this"],
        delivery_intent=DeliveryIntent.WARM,
    )
    assert plan.requires_deterministic_template() is True
    assert len(plan.critical_facts()) == 1


def test_semantic_plan_ask_slot_valid():
    plan = SemanticPlan(
        operation=PlanOperation.ASK_SLOT,
        question=PlannedQuestion(
            purpose="ask_phone", text_goal="Ask for a callback number",
        ),
    )
    assert plan.question is not None


def test_critical_and_optional_facts_partition():
    plan = SemanticPlan(
        operation=PlanOperation.OFFER_SLOTS,
        facts=[
            PlannedFact(claim="10:30 AM Thursday", source="tool:1", critical=True),
            PlannedFact(claim="45 minutes", source="profile:1", critical=False),
        ],
    )
    assert len(plan.critical_facts()) == 1
    assert len(plan.optional_facts()) == 1
    assert plan.requires_deterministic_template() is False
