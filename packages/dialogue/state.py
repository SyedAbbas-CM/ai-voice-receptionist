"""Core dialogue state types.

The evidence-backed slot is the key primitive.  Instead of
`state.extracted.name = "Sarah"`, we say:

    SlotEvidence(
        value="Sarah Khan",
        source_turn_id="turn_7",
        source_text="Yeah, this is Sarah, Sarah Khan",
        source_role=SourceRole.CALLER,
        confidence=0.92,
        status=SlotStatus.EXPLICIT,
    )

That lets downstream tools ask:
  * Is this value confirmed or just proposed?
  * Where did it come from?  (caller / tool result / business profile)
  * Was it superseded by a later correction?
  * Should we re-confirm before booking?

A TaskState holds slots for one caller goal (book, cancel, FAQ...).
A ConversationAgenda holds multiple TaskStates when the caller has
multiple asks in one call.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class SlotStatus(str, Enum):
    """Lifecycle of a single slot value.

    Ordering matters — a proposal becomes explicit becomes confirmed.
    Supersession and rejection are terminal within a task; they don't
    delete the evidence, they just mark it inactive."""
    PROPOSED = "proposed"          # inferred / guessed, not yet spoken back
    INFERRED = "inferred"          # tool or profile fill; caller hasn't heard it
    EXPLICIT = "explicit"          # caller stated it clearly
    CONFIRMED = "confirmed"        # agent read it back, caller acknowledged
    SUPERSEDED = "superseded"      # caller corrected — replaced by newer value
    REJECTED = "rejected"          # caller explicitly said no to this


class SourceRole(str, Enum):
    """Who supplied this evidence."""
    CALLER = "caller"
    TOOL = "tool"
    BUSINESS_PROFILE = "business_profile"
    AGENT_INFERENCE = "agent_inference"   # semantic planner guessed


class TaskKind(str, Enum):
    """What kind of goal this task represents.

    Closed enum — resist inflation.  Additions require a policy update
    (see policy.py in a follow-up commit)."""
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    FIND_EXISTING = "find_existing"
    FAQ = "faq"
    COMPLAINT = "complaint"
    HANDOFF = "handoff"
    EMERGENCY = "emergency"
    OTHER = "other"


class TaskStatus(str, Enum):
    """Lifecycle of a single task within a call."""
    DISCOVERED = "discovered"                    # agent noticed the caller wants this
    COLLECTING = "collecting"                    # gathering slots
    READY_TO_PROPOSE = "ready_to_propose"        # all required slots collected
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMMITTING = "committing"                    # tool call in flight
    COMPLETED = "completed"
    FAILED = "failed"
    DEFERRED = "deferred"                        # caller asked to come back to it


class SlotEvidence(BaseModel):
    """One piece of evidence for a slot value.

    Multiple pieces of evidence can exist for the same slot key
    (`phone`, `service`, `start_iso`).  The reducer picks the ACTIVE
    one — most recent EXPLICIT / CONFIRMED that hasn't been SUPERSEDED
    or REJECTED.

    Kept frozen so the reducer's job is "produce a new evidence and
    mark old ones superseded", not "mutate in place"."""
    value: Any
    source_turn_id: str
    source_text: str = ""
    """The actual words the caller/tool used.  For audit trail + write
    guard.  Empty string ok for BUSINESS_PROFILE evidence."""
    source_role: SourceRole
    confidence: float = Field(ge=0.0, le=1.0)
    status: SlotStatus = SlotStatus.PROPOSED
    monotonic_ns: int = 0
    """Set by the reducer on apply.  0 sentinel = not-yet-applied."""

    model_config = {"frozen": True}


class TaskState(BaseModel):
    """One goal being worked on for the caller.

    `slots` is a dict of slot_name → list-of-evidence (most recent last).
    The reducer never removes evidence; it appends new evidence and
    marks old evidence SUPERSEDED.  This preserves the correction
    history for tests, audits, and post-hoc analysis."""
    task_id: str
    kind: TaskKind
    status: TaskStatus = TaskStatus.DISCOVERED
    required_slots: list[str] = Field(default_factory=list)
    slots: dict[str, list[SlotEvidence]] = Field(default_factory=dict)
    unresolved_questions: list[str] = Field(default_factory=list)
    proposed_action_id: Optional[str] = None
    committed_action_id: Optional[str] = None
    failure_reason: Optional[str] = None

    # ── read helpers ────────────────────────────────────────────────

    def active_evidence(self, slot_name: str) -> Optional[SlotEvidence]:
        """Return the currently-active evidence for a slot, or None.

        Active = most recent evidence whose status is not SUPERSEDED or
        REJECTED.  Handles the "caller said Tuesday then Thursday"
        case: Tuesday's SlotEvidence sits in the list marked SUPERSEDED,
        Thursday's sits after it marked EXPLICIT/CONFIRMED."""
        entries = self.slots.get(slot_name, [])
        for ev in reversed(entries):
            if ev.status not in (SlotStatus.SUPERSEDED, SlotStatus.REJECTED):
                return ev
        return None

    def active_value(self, slot_name: str) -> Any:
        """Convenience: active_evidence's value, or None."""
        ev = self.active_evidence(slot_name)
        return ev.value if ev is not None else None

    def missing_slots(self) -> list[str]:
        """Required slots with no active evidence."""
        return [
            s for s in self.required_slots
            if self.active_evidence(s) is None
        ]

    def is_ready_to_commit(self, require_confirmation: bool = True) -> bool:
        """True when all required slots have active evidence at the
        appropriate status level.

        require_confirmation=True (default): every required slot must be
        CONFIRMED.  This is what a booking commit should demand.
        require_confirmation=False: EXPLICIT is enough.  Useful for
        low-stakes tasks (e.g., FAQ)."""
        for s in self.required_slots:
            ev = self.active_evidence(s)
            if ev is None:
                return False
            if require_confirmation:
                if ev.status != SlotStatus.CONFIRMED:
                    return False
            else:
                if ev.status not in (SlotStatus.EXPLICIT, SlotStatus.CONFIRMED):
                    return False
        return True


class ConversationAgenda(BaseModel):
    """The caller's asks, tracked as a list of tasks with one active.

    Multi-intent scenarios: caller says "I want to book a cleaning AND
    ask about insurance AND check my claim."  The reducer creates three
    tasks; the semantic planner picks one to work on now
    (`active_task_id`) and defers the others."""
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    active_task_id: Optional[str] = None
    deferred_task_ids: list[str] = Field(default_factory=list)
    completed_task_ids: list[str] = Field(default_factory=list)

    def active_task(self) -> Optional[TaskState]:
        if self.active_task_id is None:
            return None
        return self.tasks.get(self.active_task_id)

    def open_task_count(self) -> int:
        """Tasks not yet completed or failed."""
        return sum(
            1 for t in self.tasks.values()
            if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        )


class DialogueState(BaseModel):
    """Top-level per-call dialogue state.

    Wraps the agenda + call-scoped metadata that isn't task-specific
    (business/tenant identity, escalation flag, current turn counter).
    Held alongside the legacy CallState during the Sprint 10 migration
    — brain writes to both; consumers migrate one at a time."""
    call_id: str
    tenant_id: str
    business_id: str
    agenda: ConversationAgenda = Field(default_factory=ConversationAgenda)
    turn_counter: int = 0
    escalated: bool = False
    escalation_reason: Optional[str] = None
    # Sequence counter used to stamp SlotEvidence.monotonic_ns via a
    # deterministic clock injected at the reducer boundary.  Kept in
    # the state so replays produce identical evidence order.
    _seq: int = 0

    # ── read helpers ────────────────────────────────────────────────

    def active_task(self) -> Optional[TaskState]:
        return self.agenda.active_task()

    def all_open_tasks(self) -> list[TaskState]:
        return [
            t for t in self.agenda.tasks.values()
            if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]

    def next_seq(self) -> int:
        """Reducer calls this to stamp evidence.  Bump-then-return."""
        self._seq += 1
        return self._seq
