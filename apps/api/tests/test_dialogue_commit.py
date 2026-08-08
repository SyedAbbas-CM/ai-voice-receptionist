"""Sprint 10 Track B tests: Propose → Confirm → Commit protocol.

Coverage:
  * Deterministic action_id — same inputs always yield same id
    (idempotency prerequisite).
  * Proposal build fails CLOSED on missing/insufficient evidence.
  * Confirmation scope check catches partial acknowledgments.
  * Coordinator dedup — same action_id returns cached result.
  * Concurrent commit — two tasks with same action_id serialize.
  * Evidence invalidation after proposal → commit rejected.
  * Adapter that raises → PROVIDER_ERROR result, no exception surfaces.
  * Committed values come from adapter, not from proposal.
"""
from __future__ import annotations

import asyncio

import pytest

from packages.dialogue import (
    ActionArgument,
    ActionKind,
    ActionProposal,
    CallerConfirmation,
    CommitCoordinator,
    CommitOutcome,
    CommitPolicyError,
    CommitResult,
    DialogueState,
    SlotEvidence,
    SlotStatus,
    TaskKind,
    TaskState,
    apply_correction,
    reduce_patch,
)
from packages.dialogue.reducer import (
    AddEvidencePatch,
    AddTaskPatch,
)
from packages.dialogue.state import SourceRole


def _fresh_state() -> DialogueState:
    return DialogueState(
        call_id="CA-test", tenant_id="acme", business_id="biz-1",
    )


def _ev(value, turn, status=SlotStatus.EXPLICIT, conf=0.9):
    return SlotEvidence(
        value=value, source_turn_id=turn, source_text=f"turn:{turn}",
        source_role=SourceRole.CALLER, confidence=conf, status=status,
    )


class _FakeAdapter:
    """Test adapter — records calls, returns configurable result."""

    def __init__(self, outcome=CommitOutcome.SUCCESS, external_id="ext_123",
                 raise_error=None, sleep_ms=0):
        self.outcome = outcome
        self.external_id = external_id
        self.raise_error = raise_error
        self.sleep_ms = sleep_ms
        self.calls_received: list[ActionProposal] = []

    async def commit(self, proposal: ActionProposal) -> CommitResult:
        self.calls_received.append(proposal)
        if self.sleep_ms:
            await asyncio.sleep(self.sleep_ms / 1000)
        if self.raise_error:
            raise self.raise_error
        return CommitResult(
            outcome=self.outcome,
            action_id=proposal.action_id,
            external_id=self.external_id if self.outcome == CommitOutcome.SUCCESS else None,
            committed_values={a.name: a.value for a in proposal.arguments},
        )


# ── determinism ─────────────────────────────────────────────────────

def test_action_id_deterministic_across_builds():
    """Same task+kind+args must always produce the same action_id.
    This is the foundation of idempotency."""
    args = [
        ActionArgument(name="caller_name", value="Sarah", evidence_turn_id="t2"),
        ActionArgument(name="start_iso", value="2026-08-06T10:00", evidence_turn_id="t3"),
    ]
    p1 = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="book_1",
        tenant_id="acme", business_id="biz-1", arguments=args,
    )
    p2 = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="book_1",
        tenant_id="acme", business_id="biz-1", arguments=args,
    )
    assert p1.action_id == p2.action_id
    assert p1.idempotency_key == p2.idempotency_key


def test_action_id_different_when_arg_value_changes():
    args_a = [ActionArgument(name="start_iso", value="2026-08-06T10:00", evidence_turn_id="t")]
    args_b = [ActionArgument(name="start_iso", value="2026-08-06T14:00", evidence_turn_id="t")]
    p_a = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="book_1",
        tenant_id="acme", business_id="biz-1", arguments=args_a,
    )
    p_b = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="book_1",
        tenant_id="acme", business_id="biz-1", arguments=args_b,
    )
    assert p_a.action_id != p_b.action_id


def test_action_id_argument_order_independent():
    """Argument list order shouldn't affect the hash — args are keyed
    by name, not position."""
    a1 = ActionArgument(name="phone", value="+15550001111", evidence_turn_id="t")
    a2 = ActionArgument(name="name", value="Sarah", evidence_turn_id="t")
    p_forward = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t", tenant_id="a",
        business_id="b", arguments=[a1, a2],
    )
    p_reverse = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t", tenant_id="a",
        business_id="b", arguments=[a2, a1],
    )
    assert p_forward.action_id == p_reverse.action_id


def test_idempotency_key_tenant_scoped():
    """Same action from two different tenants must produce distinct
    idempotency keys — no cross-tenant collision."""
    args = [ActionArgument(name="x", value="v", evidence_turn_id="t")]
    p_a = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t",
        tenant_id="tenant-a", business_id="biz-1", arguments=args,
    )
    p_b = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t",
        tenant_id="tenant-b", business_id="biz-1", arguments=args,
    )
    assert p_a.idempotency_key != p_b.idempotency_key
    assert p_a.action_id != p_b.action_id


# ── proposal fails closed on bad evidence ───────────────────────────

def test_propose_fails_closed_on_missing_evidence():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name", "phone", "start_iso"],
    ))
    task = state.agenda.tasks["book_1"]
    # Fill only one slot; propose needs three
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    coord = CommitCoordinator(adapters={})
    with pytest.raises(CommitPolicyError, match="missing_evidence"):
        coord.propose(
            kind=ActionKind.BOOK_APPOINTMENT,
            task=state.agenda.tasks["book_1"], state=state,
            argument_map={
                "caller_name": "caller_name",
                "phone": "phone",
                "start_iso": "start_iso",
            },
        )


def test_propose_fails_closed_on_proposed_evidence():
    """Evidence at PROPOSED status (never spoken back) is not enough."""
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1", status=SlotStatus.PROPOSED),
    ))
    coord = CommitCoordinator(adapters={})
    with pytest.raises(CommitPolicyError, match="argument_status_insufficient"):
        coord.propose(
            kind=ActionKind.BOOK_APPOINTMENT,
            task=state.agenda.tasks["book_1"], state=state,
            argument_map={"caller_name": "caller_name"},
        )


def test_propose_ok_with_explicit_evidence():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1", status=SlotStatus.EXPLICIT),
    ))
    coord = CommitCoordinator(adapters={})
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT,
        task=state.agenda.tasks["book_1"], state=state,
        argument_map={"caller_name": "caller_name"},
    )
    assert proposal.arguments[0].value == "Sarah"
    assert proposal.arguments[0].evidence_turn_id == "t1"


# ── confirmation scope ─────────────────────────────────────────────

def test_confirmation_covers_all_arguments():
    coord = CommitCoordinator(adapters={})
    proposal = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t",
        tenant_id="a", business_id="b",
        arguments=[
            ActionArgument(name="caller_name", value="Sarah", evidence_turn_id="t1"),
            ActionArgument(name="start_iso", value="2026-08-06T10:00", evidence_turn_id="t2"),
        ],
    )
    confirmations = [
        CallerConfirmation(
            action_id=proposal.action_id, caller_turn_id="t3",
            scope=["caller_name"], confidence=0.9,
        ),
        CallerConfirmation(
            action_id=proposal.action_id, caller_turn_id="t5",
            scope=["start_iso"], confidence=0.95,
        ),
    ]
    covered, missing = coord.check_confirmation_covers_all_arguments(
        proposal, confirmations,
    )
    assert covered is True
    assert missing == []


def test_confirmation_partial_flags_missing_scope():
    coord = CommitCoordinator(adapters={})
    proposal = ActionProposal.build(
        kind=ActionKind.BOOK_APPOINTMENT, task_id="t",
        tenant_id="a", business_id="b",
        arguments=[
            ActionArgument(name="caller_name", value="Sarah", evidence_turn_id="t1"),
            ActionArgument(name="phone", value="+15550009999", evidence_turn_id="t2"),
        ],
    )
    # Caller confirmed name but never confirmed phone
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.9,
    )]
    covered, missing = coord.check_confirmation_covers_all_arguments(
        proposal, confirmations,
    )
    assert covered is False
    assert missing == ["phone"]


def test_confirmation_scope_empty_rejected_at_construction():
    """CallerConfirmation with empty scope is meaningless — caller has
    to have acknowledged SOMETHING specific."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="scope cannot be empty"):
        CallerConfirmation(
            action_id="act_x", caller_turn_id="t", scope=[], confidence=0.9,
        )


# ── coordinator commit — happy path ────────────────────────────────

@pytest.mark.asyncio
async def test_commit_success_calls_adapter_once():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name", "start_iso"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="start_iso",
        evidence=_ev("2026-08-06T10:00", "t3"),
    ))
    adapter = _FakeAdapter()
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["book_1"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name", "start_iso": "start_iso"},
    )
    confirmations = [
        CallerConfirmation(action_id=proposal.action_id, caller_turn_id="t5",
                           scope=["caller_name", "start_iso"], confidence=0.95),
    ]
    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.SUCCESS
    assert result.external_id == "ext_123"
    assert result.committed_values == {"caller_name": "Sarah",
                                       "start_iso": "2026-08-06T10:00"}
    assert len(adapter.calls_received) == 1


# ── idempotency: dedup ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commit_dedup_second_call_returns_cached():
    """Calling commit twice with the same proposal must call the
    adapter exactly ONCE.  Second call returns the cached result.
    This is what prevents duplicate bookings on network retry."""
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    adapter = _FakeAdapter()
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["book_1"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]

    r1 = await coord.commit(proposal, confirmations, task)
    r2 = await coord.commit(proposal, confirmations, task)

    assert r1.outcome == CommitOutcome.SUCCESS
    assert r2.outcome == CommitOutcome.SUCCESS
    assert r1.external_id == r2.external_id
    assert len(adapter.calls_received) == 1, \
        "adapter must be called ONCE, not twice"


@pytest.mark.asyncio
async def test_concurrent_commit_same_action_serializes():
    """Two coroutines committing the same proposal at the same time
    must collapse to ONE adapter call.  Second waits on the first."""
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    # Adapter with a real 20ms delay so we can prove serialization
    adapter = _FakeAdapter(sleep_ms=20)
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["book_1"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]

    r1, r2 = await asyncio.gather(
        coord.commit(proposal, confirmations, task),
        coord.commit(proposal, confirmations, task),
    )
    assert r1.outcome == CommitOutcome.SUCCESS
    assert r2.outcome == CommitOutcome.SUCCESS
    assert r1.external_id == r2.external_id
    assert len(adapter.calls_received) == 1


# ── evidence invalidation after proposal ───────────────────────────

@pytest.mark.asyncio
async def test_commit_rejected_when_evidence_superseded_between_propose_and_commit():
    """The dangerous scenario: agent proposes a booking, caller
    corrects mid-way ("wait, actually Thursday not Tuesday"), commit
    fires anyway → booking on wrong day.  Coordinator must catch this."""
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["start_iso"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="start_iso",
        evidence=_ev("2026-08-04T10:00", "t3"),
    ))
    adapter = _FakeAdapter()
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["book_1"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"start_iso": "start_iso"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t5",
        scope=["start_iso"], confidence=0.95,
    )]

    # Caller corrects BETWEEN propose and commit
    apply_correction(
        state, task_id="book_1", slot_name="start_iso",
        new_value="2026-08-06T16:00", source_turn_id="t7",
    )

    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.REJECTED
    assert "evidence_invalidated" in (result.error or "")
    assert len(adapter.calls_received) == 0, \
        "adapter must NOT be called when evidence is stale"


# ── confirmation policy ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commit_rejected_when_confirmation_incomplete():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="book_1", task_kind=TaskKind.BOOK,
        required_slots=["caller_name", "phone"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="book_1", slot_name="phone",
        evidence=_ev("+15550009999", "t2"),
    ))
    adapter = _FakeAdapter()
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["book_1"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name", "phone": "phone"},
    )
    # Caller confirmed name only, never confirmed phone
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]
    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.REJECTED
    assert "confirmation_scope_incomplete" in (result.error or "")
    assert len(adapter.calls_received) == 0


@pytest.mark.asyncio
async def test_commit_low_stakes_can_skip_confirmation_scope():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.FAQ,
        required_slots=["question_topic"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="question_topic",
        evidence=_ev("insurance", "t1"),
    ))
    adapter = _FakeAdapter()
    # Reusing BOOK_APPOINTMENT here just as a stand-in kind
    coord = CommitCoordinator(adapters={ActionKind.RECORD_DISPOSITION: adapter})
    task = state.agenda.tasks["t"]
    proposal = coord.propose(
        kind=ActionKind.RECORD_DISPOSITION, task=task, state=state,
        argument_map={"question_topic": "question_topic"},
    )
    result = await coord.commit(
        proposal, confirmations=[], task=task,
        require_full_confirmation=False,
    )
    assert result.outcome == CommitOutcome.SUCCESS


# ── adapter errors ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adapter_exception_becomes_provider_error():
    """Coordinator must catch adapter exceptions and turn them into
    a PROVIDER_ERROR outcome — never surface the exception."""
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    adapter = _FakeAdapter(raise_error=RuntimeError("google calendar 503"))
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["t"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]
    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.PROVIDER_ERROR
    assert "google calendar 503" in (result.error or "")


@pytest.mark.asyncio
async def test_commit_no_adapter_registered_rejected():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK,
        required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    coord = CommitCoordinator(adapters={})  # no adapter
    task = state.agenda.tasks["t"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]
    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.REJECTED
    assert "no_adapter" in (result.error or "")


# ── committed values come from adapter, not proposal ───────────────

@pytest.mark.asyncio
async def test_committed_values_are_authoritative():
    """If the provider normalized a phone number or truncated a name,
    the spoken confirmation should use the provider's version, not
    what the caller said.  Contract check for that."""

    class NormalizingAdapter:
        async def commit(self, proposal):
            # Provider normalizes phone
            values = {a.name: a.value for a in proposal.arguments}
            if "phone" in values:
                values["phone"] = "+15550009999"   # normalized E.164
            return CommitResult(
                outcome=CommitOutcome.SUCCESS,
                action_id=proposal.action_id,
                external_id="ext_999",
                committed_values=values,
            )

    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK,
        required_slots=["phone"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="phone",
        evidence=_ev("555 000 9999", "t1"),   # unnormalized
    ))
    coord = CommitCoordinator(
        adapters={ActionKind.BOOK_APPOINTMENT: NormalizingAdapter()},
    )
    task = state.agenda.tasks["t"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"phone": "phone"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["phone"], confidence=0.95,
    )]
    result = await coord.commit(proposal, confirmations, task)
    assert result.outcome == CommitOutcome.SUCCESS
    # Committed value is the normalized one — this is what the
    # deterministic spoken template should use.
    assert result.committed_values["phone"] == "+15550009999"


# ── prior_result debug helper ──────────────────────────────────────

@pytest.mark.asyncio
async def test_prior_result_returns_cached_after_commit():
    state = _fresh_state()
    reduce_patch(state, AddTaskPatch(
        task_id="t", task_kind=TaskKind.BOOK, required_slots=["caller_name"],
    ))
    reduce_patch(state, AddEvidencePatch(
        task_id="t", slot_name="caller_name",
        evidence=_ev("Sarah", "t1"),
    ))
    adapter = _FakeAdapter()
    coord = CommitCoordinator(adapters={ActionKind.BOOK_APPOINTMENT: adapter})
    task = state.agenda.tasks["t"]
    proposal = coord.propose(
        kind=ActionKind.BOOK_APPOINTMENT, task=task, state=state,
        argument_map={"caller_name": "caller_name"},
    )
    confirmations = [CallerConfirmation(
        action_id=proposal.action_id, caller_turn_id="t3",
        scope=["caller_name"], confidence=0.95,
    )]

    assert coord.prior_result(proposal.action_id) is None
    await coord.commit(proposal, confirmations, task)
    prior = coord.prior_result(proposal.action_id)
    assert prior is not None
    assert prior.outcome == CommitOutcome.SUCCESS
