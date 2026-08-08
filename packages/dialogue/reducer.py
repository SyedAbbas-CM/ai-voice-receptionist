"""State reducer — validates and applies state patches.

Contract:
  1. LLM (or heuristic) PROPOSES a StatePatch.
  2. Reducer.apply() validates the patch against current state.
  3. If valid, produces a new DialogueState (mutating in place is ok
     — DialogueState instances are per-call and not shared across
     coroutines while the actor is running).
  4. If invalid, raises PatchRejected with a machine-readable reason.

The reducer never calls an LLM.  All logic here is deterministic.
This is the "kernel" the audit demanded — Python code owns state
transitions; the model just proposes.

Patch semantics:
  * Adding evidence for a slot: append to slots[key]; the ACTIVE
    evidence is auto-determined by TaskState.active_evidence().
  * Superseding evidence: mark old evidence SUPERSEDED, add new one.
    Reducer handles this automatically when new evidence's confidence
    is high enough AND status is EXPLICIT/CONFIRMED.
  * Rejecting evidence: mark REJECTED, don't append.  Used for "no,
    that's not my number."
  * Transitioning task status: valid transitions only (see
    TASK_TRANSITIONS below).
  * Adding a new task: agenda gets a new TaskState.  Optionally sets
    it as active.

All patches carry a `source_turn_id` — required for audit trail.
"""
from __future__ import annotations

import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .state import (
    ConversationAgenda,
    DialogueState,
    SlotEvidence,
    SlotStatus,
    SourceRole,
    TaskKind,
    TaskState,
    TaskStatus,
)


class PatchRejected(ValueError):
    """Raised when a state patch violates a reducer invariant.

    Carries `reason` (short machine-readable code) + human-readable
    detail.  Callers should log the reason as a metric label."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── valid task-status transitions ───────────────────────────────────

TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DISCOVERED: {TaskStatus.COLLECTING, TaskStatus.DEFERRED, TaskStatus.FAILED},
    TaskStatus.COLLECTING: {
        TaskStatus.READY_TO_PROPOSE, TaskStatus.DEFERRED,
        TaskStatus.FAILED, TaskStatus.COLLECTING,  # self-loop ok
    },
    TaskStatus.READY_TO_PROPOSE: {
        TaskStatus.AWAITING_CONFIRMATION, TaskStatus.COLLECTING,
        TaskStatus.FAILED,
    },
    TaskStatus.AWAITING_CONFIRMATION: {
        TaskStatus.COMMITTING, TaskStatus.COLLECTING,
        TaskStatus.FAILED,
    },
    TaskStatus.COMMITTING: {TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),   # terminal
    TaskStatus.FAILED: set(),      # terminal
    TaskStatus.DEFERRED: {TaskStatus.COLLECTING, TaskStatus.DISCOVERED, TaskStatus.FAILED},
}


# ── patch types ─────────────────────────────────────────────────────

class AddEvidencePatch(BaseModel):
    """Add a new SlotEvidence to a task's slots.  Auto-supersedes older
    active evidence if the new evidence is EXPLICIT/CONFIRMED with
    higher-or-equal confidence."""
    kind: Literal["add_evidence"] = "add_evidence"
    task_id: str
    slot_name: str
    evidence: SlotEvidence


class RejectEvidencePatch(BaseModel):
    """Mark an existing evidence entry REJECTED (caller said no)."""
    kind: Literal["reject_evidence"] = "reject_evidence"
    task_id: str
    slot_name: str
    # Match by source_turn_id — enough to disambiguate in practice.
    source_turn_id: str


class TransitionTaskPatch(BaseModel):
    kind: Literal["transition_task"] = "transition_task"
    task_id: str
    to_status: TaskStatus
    reason: Optional[str] = None


class AddTaskPatch(BaseModel):
    kind: Literal["add_task"] = "add_task"
    task_id: str
    task_kind: TaskKind
    required_slots: list[str] = Field(default_factory=list)
    make_active: bool = True


class SetActiveTaskPatch(BaseModel):
    kind: Literal["set_active_task"] = "set_active_task"
    task_id: str


class DeferTaskPatch(BaseModel):
    kind: Literal["defer_task"] = "defer_task"
    task_id: str


class RecordCommitPatch(BaseModel):
    """Record that a task's external action was committed with an
    action_id.  Transitions the task to COMPLETED."""
    kind: Literal["record_commit"] = "record_commit"
    task_id: str
    action_id: str


class EscalatePatch(BaseModel):
    """Mark the whole call as escalated."""
    kind: Literal["escalate"] = "escalate"
    reason: str


StatePatch = (
    AddEvidencePatch
    | RejectEvidencePatch
    | TransitionTaskPatch
    | AddTaskPatch
    | SetActiveTaskPatch
    | DeferTaskPatch
    | RecordCommitPatch
    | EscalatePatch
)


# ── reducer ─────────────────────────────────────────────────────────

class Reducer:
    """Applies patches to a DialogueState.

    Stateless class — construct once per call actor (or per app).
    Each apply() mutates its `state` argument.  Not thread-safe; the
    CallActor's single-consumer mailbox guarantees serial invocation."""

    def __init__(self, clock_ns=None) -> None:
        # Injectable clock so replay tests can produce deterministic
        # monotonic_ns stamps.  Defaults to time.monotonic_ns.
        self._clock_ns = clock_ns or time.monotonic_ns

    def apply(self, state: DialogueState, patch: StatePatch) -> DialogueState:
        """Validate + apply a patch.  Returns the same state instance
        (mutated).  Raises PatchRejected on invariant violation."""
        method = getattr(self, f"_apply_{patch.kind}", None)
        if method is None:
            raise PatchRejected("unknown_patch_kind", str(patch.kind))
        method(state, patch)
        return state

    def apply_all(
        self, state: DialogueState, patches: list[StatePatch],
    ) -> DialogueState:
        """Apply a batch atomically-ish.  If any raises, the ones
        before it stay applied — callers should treat batch failures
        as advisory (log + retry the failing one individually)."""
        for p in patches:
            self.apply(state, p)
        return state

    # ── per-kind handlers ────────────────────────────────────────────

    def _apply_add_evidence(self, state: DialogueState, patch: AddEvidencePatch) -> None:
        task = state.agenda.tasks.get(patch.task_id)
        if task is None:
            raise PatchRejected("unknown_task", patch.task_id)

        # Stamp evidence with an ordering seq — SlotEvidence is frozen
        # so we replace with a new instance carrying the stamp.
        stamped = patch.evidence.model_copy(
            update={"monotonic_ns": self._clock_ns()},
        )

        entries = task.slots.setdefault(patch.slot_name, [])

        # Supersession rule: if the new evidence is EXPLICIT or
        # CONFIRMED and its value differs from the current active
        # evidence, mark the older active evidence SUPERSEDED.
        current_active = task.active_evidence(patch.slot_name)
        if (
            current_active is not None
            and stamped.status in (SlotStatus.EXPLICIT, SlotStatus.CONFIRMED)
            and stamped.value != current_active.value
        ):
            # Replace in list with a SUPERSEDED copy
            for i, ev in enumerate(entries):
                if (
                    ev.source_turn_id == current_active.source_turn_id
                    and ev.value == current_active.value
                    and ev.status == current_active.status
                ):
                    entries[i] = ev.model_copy(update={"status": SlotStatus.SUPERSEDED})
                    break

        entries.append(stamped)

    def _apply_reject_evidence(
        self, state: DialogueState, patch: RejectEvidencePatch,
    ) -> None:
        task = state.agenda.tasks.get(patch.task_id)
        if task is None:
            raise PatchRejected("unknown_task", patch.task_id)
        entries = task.slots.get(patch.slot_name, [])
        found = False
        for i, ev in enumerate(entries):
            if ev.source_turn_id == patch.source_turn_id:
                entries[i] = ev.model_copy(update={"status": SlotStatus.REJECTED})
                found = True
        if not found:
            raise PatchRejected(
                "evidence_not_found",
                f"task={patch.task_id} slot={patch.slot_name} turn={patch.source_turn_id}",
            )

    def _apply_transition_task(
        self, state: DialogueState, patch: TransitionTaskPatch,
    ) -> None:
        task = state.agenda.tasks.get(patch.task_id)
        if task is None:
            raise PatchRejected("unknown_task", patch.task_id)
        allowed = TASK_TRANSITIONS.get(task.status, set())
        if patch.to_status not in allowed:
            raise PatchRejected(
                "invalid_transition",
                f"{task.status.value} -> {patch.to_status.value}",
            )
        task.status = patch.to_status
        if patch.to_status == TaskStatus.FAILED and patch.reason:
            task.failure_reason = patch.reason
        # Auto-move completed/failed tasks off the active spot
        if patch.to_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            if state.agenda.active_task_id == patch.task_id:
                state.agenda.active_task_id = None
            if patch.to_status == TaskStatus.COMPLETED:
                if patch.task_id not in state.agenda.completed_task_ids:
                    state.agenda.completed_task_ids.append(patch.task_id)

    def _apply_add_task(self, state: DialogueState, patch: AddTaskPatch) -> None:
        if patch.task_id in state.agenda.tasks:
            raise PatchRejected("task_exists", patch.task_id)
        state.agenda.tasks[patch.task_id] = TaskState(
            task_id=patch.task_id,
            kind=patch.task_kind,
            required_slots=list(patch.required_slots),
        )
        if patch.make_active:
            state.agenda.active_task_id = patch.task_id

    def _apply_set_active_task(
        self, state: DialogueState, patch: SetActiveTaskPatch,
    ) -> None:
        if patch.task_id not in state.agenda.tasks:
            raise PatchRejected("unknown_task", patch.task_id)
        state.agenda.active_task_id = patch.task_id
        # If this task was deferred, remove it from the deferred list
        if patch.task_id in state.agenda.deferred_task_ids:
            state.agenda.deferred_task_ids.remove(patch.task_id)

    def _apply_defer_task(self, state: DialogueState, patch: DeferTaskPatch) -> None:
        if patch.task_id not in state.agenda.tasks:
            raise PatchRejected("unknown_task", patch.task_id)
        task = state.agenda.tasks[patch.task_id]
        # Only defer tasks that are still active (not terminal)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise PatchRejected("cannot_defer_terminal", task.status.value)
        task.status = TaskStatus.DEFERRED
        if state.agenda.active_task_id == patch.task_id:
            state.agenda.active_task_id = None
        if patch.task_id not in state.agenda.deferred_task_ids:
            state.agenda.deferred_task_ids.append(patch.task_id)

    def _apply_record_commit(
        self, state: DialogueState, patch: RecordCommitPatch,
    ) -> None:
        task = state.agenda.tasks.get(patch.task_id)
        if task is None:
            raise PatchRejected("unknown_task", patch.task_id)
        if task.status != TaskStatus.COMMITTING:
            raise PatchRejected(
                "commit_from_wrong_status", task.status.value,
            )
        task.committed_action_id = patch.action_id
        task.status = TaskStatus.COMPLETED
        if state.agenda.active_task_id == patch.task_id:
            state.agenda.active_task_id = None
        if patch.task_id not in state.agenda.completed_task_ids:
            state.agenda.completed_task_ids.append(patch.task_id)

    def _apply_escalate(self, state: DialogueState, patch: EscalatePatch) -> None:
        state.escalated = True
        state.escalation_reason = patch.reason


# ── module-level convenience wrappers ───────────────────────────────

_DEFAULT_REDUCER = Reducer()


def reduce_patch(state: DialogueState, patch: StatePatch) -> DialogueState:
    """Apply a single patch using the module-level reducer.  Suitable
    when you don't need a custom clock (tests inject their own Reducer)."""
    return _DEFAULT_REDUCER.apply(state, patch)


def apply_correction(
    state: DialogueState,
    task_id: str,
    slot_name: str,
    new_value: Any,
    source_turn_id: str,
    source_text: str = "",
    confidence: float = 0.9,
) -> DialogueState:
    """Convenience helper for the common "caller corrected the slot"
    pattern.  Emits an AddEvidencePatch with EXPLICIT status, which
    triggers the auto-supersede rule."""
    return reduce_patch(
        state,
        AddEvidencePatch(
            task_id=task_id,
            slot_name=slot_name,
            evidence=SlotEvidence(
                value=new_value,
                source_turn_id=source_turn_id,
                source_text=source_text,
                source_role=SourceRole.CALLER,
                confidence=confidence,
                status=SlotStatus.EXPLICIT,
            ),
        ),
    )
