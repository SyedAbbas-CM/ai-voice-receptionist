"""Sprint 10 WIRING: bridge between ReceptionistBrain and dialogue kernel.

Design: additive.  Nothing here replaces the brain's tool-loop or
system prompt.  The brain calls into KernelWiring at three points:

  1. on_call_start(state) — creates a DialogueState alongside CallState
  2. on_user_turn(state, user_text, turn_id) — emits StatePatches to
     capture caller slots + task discovery
  3. before_book(state, call, arguments) — validates through
     TemporalResolver + CommitCoordinator; returns args to actually
     execute OR a "we need to confirm X first" refusal

Gated by settings.dialogue_kernel_enabled.  When False, all methods
no-op — brain runs as pre-Sprint-10.

Kept intentionally small.  Full brain rewrite would be a Sprint 11
project.  This is the surgical stitch that makes the tested kernel
observable in a live call.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from packages.dialogue import (
    ActionArgument,
    ActionKind,
    ActionProposal,
    CallerConfirmation,
    CommitCoordinator,
    CommitOutcome,
    CommitPolicyError,
    ConversationAgenda,
    DialogueState,
    ResolvedRange,
    Resolution,
    SlotEvidence,
    SlotStatus,
    SourceRole,
    TaskKind,
    TaskState,
    TaskStatus,
    TemporalContext,
    TemporalResolver,
    reduce_patch,
)
from packages.dialogue.reducer import (
    AddEvidencePatch,
    AddTaskPatch,
    TransitionTaskPatch,
)
from packages.observability.call_event_log import (
    CallEvent as CallLogEvent,
    EventSourceKind,
    get_call_event_log,
)
from packages.schemas import CallState

log = logging.getLogger(__name__)


# Heuristic patterns to catch booking-related utterances without an LLM.
# Kept intentionally coarse — the LLM tool-call layer does the real
# extraction; these just decide "is a booking task active?" so we can
# start tracking slots.
_BOOK_INTENT_RE = re.compile(
    r"\b(book|schedule|make (?:an )?appointment|come in|see (?:you|the doctor|a dentist)|"
    r"reserve|set up (?:an )?appointment|check\s+in|new patient)\b",
    re.IGNORECASE,
)
_CANCEL_INTENT_RE = re.compile(
    r"\b(cancel|call off|not gonna make|can'?t make it)\b",
    re.IGNORECASE,
)
_RESCHEDULE_INTENT_RE = re.compile(
    r"\b(reschedule|move (?:my|the) appointment|change (?:my|the) appointment|"
    r"push (?:back|out)|change the time)\b",
    re.IGNORECASE,
)


# Tool arg → kernel slot mapping.  When the LLM emits a book_appointment
# call with these arguments, the kernel records evidence for the active
# BOOK task.  Same principle for other tool kinds.
_TOOL_ARG_TO_SLOT: dict[str, dict[str, str]] = {
    "book_appointment": {
        "caller_name": "caller_name",
        "phone": "phone",
        "service": "service",
        "start_iso": "start_iso",
        "notes": "notes",
    },
    "check_availability": {
        "service": "service",
        "date": "requested_date",
    },
    "cancel_appointment": {
        "appointment_id": "appointment_id",
        "reason": "cancel_reason",
    },
    "reschedule_appointment": {
        "appointment_id": "appointment_id",
        "new_start_iso": "start_iso",
    },
    "find_existing_appointment": {
        "phone": "phone",
    },
}

# Which task kind does each tool most naturally service?
_TOOL_TO_TASK_KIND: dict[str, TaskKind] = {
    "book_appointment": TaskKind.BOOK,
    "check_availability": TaskKind.BOOK,
    "cancel_appointment": TaskKind.CANCEL,
    "reschedule_appointment": TaskKind.RESCHEDULE,
    "find_existing_appointment": TaskKind.FIND_EXISTING,
}


class KernelWiring:
    """Adapter that keeps the dialogue kernel in sync with a live call.

    One instance per call session.  Owns a CommitCoordinator so the
    idempotency + per-action lock lives for the whole call."""

    def __init__(
        self,
        call_state: CallState,
        business_id: str,
        tenant_id: str,
        business_timezone: str = "America/Chicago",
        business_hours=None,
        commit_adapters: Optional[dict] = None,
    ) -> None:
        self._enabled = _kernel_enabled()
        self._call_state = call_state
        self._resolver = TemporalResolver()
        self._temporal_ctx_factory = lambda: TemporalContext.now_in(
            business_timezone, business=type("B", (), {"hours": business_hours})(),
        )
        self._coordinator = CommitCoordinator(adapters=commit_adapters or {})

        # Initialize DialogueState alongside CallState (mutation of the
        # legacy schema is via the JSON-safe `dialogue` field).
        if self._enabled and call_state.dialogue is None:
            state = DialogueState(
                call_id=call_state.session_id,
                tenant_id=tenant_id,
                business_id=business_id,
            )
            call_state.dialogue = state.model_dump()

    # ── public API ──────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self._enabled

    def dialogue_state(self) -> Optional[DialogueState]:
        """Hydrate the DialogueState from the JSON-serialized copy."""
        if not self._enabled or self._call_state.dialogue is None:
            return None
        return DialogueState.model_validate(self._call_state.dialogue)

    def _save_state(self, state: DialogueState) -> None:
        self._call_state.dialogue = state.model_dump()

    def coordinator(self) -> CommitCoordinator:
        return self._coordinator

    # ── event log ──────────────────────────────────────────────────

    def _log_event(
        self, source: EventSourceKind, kind: str, payload: dict,
    ) -> None:
        """Fire-and-forget durable log write.  Best-effort; never raises."""
        try:
            state = self.dialogue_state()
            turn_gen = state.turn_counter if state else 0
            get_call_event_log().write(CallLogEvent(
                call_id=self._call_state.session_id,
                tenant_id=self._call_state.tenant_id,
                source=source, kind=kind, payload=payload,
                turn_generation=turn_gen,
            ))
        except Exception as e:
            log.debug("kernel event log write failed: %s", e)

    # ── phase hooks ─────────────────────────────────────────────────

    def on_user_turn(self, user_text: str, turn_id: str) -> None:
        """Called after the caller's turn is transcribed.  Discovers
        or advances tasks based on intent detection.  Cheap — regex
        only; the brain's tool loop does the real work."""
        if not self._enabled:
            return
        state = self.dialogue_state()
        if state is None:
            return
        state.turn_counter += 1

        # Detect intents; add tasks that don't yet exist
        detected: list[tuple[TaskKind, list[str]]] = []
        if _BOOK_INTENT_RE.search(user_text):
            detected.append((TaskKind.BOOK, [
                "caller_name", "phone", "service", "start_iso",
            ]))
        if _CANCEL_INTENT_RE.search(user_text):
            detected.append((TaskKind.CANCEL, ["appointment_id"]))
        if _RESCHEDULE_INTENT_RE.search(user_text):
            detected.append((TaskKind.RESCHEDULE, [
                "appointment_id", "new_start_iso",
            ]))

        for kind, required in detected:
            existing = [t for t in state.agenda.tasks.values() if t.kind == kind]
            if existing:
                continue   # don't duplicate
            task_id = f"{kind.value}_{state.turn_counter}"
            try:
                reduce_patch(state, AddTaskPatch(
                    task_id=task_id, task_kind=kind,
                    required_slots=required,
                    make_active=(state.agenda.active_task_id is None),
                ))
                self._log_event(EventSourceKind.STATE, "task_added", {
                    "task_id": task_id, "kind": kind.value,
                    "required_slots": required, "trigger_text": user_text[:200],
                })
            except Exception as e:
                log.warning("kernel: AddTask failed for %s: %s", kind, e)
                self._log_event(EventSourceKind.ERROR, "add_task_failed", {
                    "task_kind": kind.value, "error": str(e),
                })

        self._save_state(state)

    def record_slots_from_tool_call(
        self, tool_name: str, arguments: dict, turn_id: str,
    ) -> None:
        """Sprint 10 WIRING: when the LLM emits a book/reschedule/etc
        tool_call, capture the arguments into the active task's slots.

        This is where the kernel gains observability of what the LLM
        actually did.  Called from brain.py right after tool_calls are
        emitted (before the tool handler runs)."""
        if not self._enabled:
            return
        state = self.dialogue_state()
        if state is None:
            return
        # Find the target task by kind + active bit
        target_task_id = self._pick_task_for_tool(state, tool_name)
        if target_task_id is None:
            return
        # Map tool args → kernel slots (per-tool schema)
        slot_map = _TOOL_ARG_TO_SLOT.get(tool_name, {})
        for arg_name, slot_name in slot_map.items():
            val = arguments.get(arg_name)
            if val is None or val == "":
                continue
            self.record_slot(target_task_id, slot_name, val, turn_id)

    def _pick_task_for_tool(self, state, tool_name: str) -> Optional[str]:
        """Pick the task this tool_call is most likely servicing.

        Preference order:
          1. Active task if its kind matches the tool
          2. Any open task of matching kind
          3. Active task regardless of kind (best-effort — the tool
             fires means SOMETHING wanted it)
          4. None if the agenda is empty
        """
        tool_kind = _TOOL_TO_TASK_KIND.get(tool_name)
        active = state.agenda.active_task()
        if active is not None and tool_kind is not None and active.kind == tool_kind:
            return active.task_id
        if tool_kind is not None:
            for t in state.agenda.tasks.values():
                if t.kind == tool_kind and t.status not in (
                    TaskStatus.COMPLETED, TaskStatus.FAILED,
                ):
                    return t.task_id
        if active is not None:
            return active.task_id
        return None

    def normalize_date_time(
        self, utterance: str,
    ) -> Optional[ResolvedRange]:
        """Ask the temporal resolver to parse a natural date/time
        utterance.  Returns None on no-parse.  Enabled independently
        of the full kernel — even without dialogue state, temporal
        parsing is a pure win."""
        if not utterance:
            return None
        try:
            ctx = self._temporal_ctx_factory()
            return self._resolver.resolve(utterance, ctx)
        except Exception as e:
            log.warning("temporal resolver failed on %r: %s", utterance, e)
            return None

    def record_slot(
        self,
        task_id: str,
        slot_name: str,
        value: Any,
        turn_id: str,
        source_role: SourceRole = SourceRole.CALLER,
        confidence: float = 0.85,
        status: SlotStatus = SlotStatus.EXPLICIT,
    ) -> None:
        """Add an evidence entry for a slot on a task."""
        if not self._enabled:
            return
        state = self.dialogue_state()
        if state is None or task_id not in state.agenda.tasks:
            return
        try:
            reduce_patch(state, AddEvidencePatch(
                task_id=task_id, slot_name=slot_name,
                evidence=SlotEvidence(
                    value=value, source_turn_id=turn_id,
                    source_text=f"turn:{turn_id}",
                    source_role=source_role, confidence=confidence,
                    status=status,
                ),
            ))
            self._save_state(state)
            self._log_event(EventSourceKind.STATE, "slot_recorded", {
                "task_id": task_id, "slot": slot_name,
                "value": str(value)[:200], "status": status.value,
                "confidence": confidence,
            })
        except Exception as e:
            log.warning("record_slot failed task=%s slot=%s: %s",
                        task_id, slot_name, e)
            self._log_event(EventSourceKind.ERROR, "slot_record_failed", {
                "task_id": task_id, "slot": slot_name, "error": str(e),
            })

    async def try_commit_booking(
        self,
        task_id: str,
        argument_map: dict[str, str],
        caller_confirmations: list[CallerConfirmation],
    ):
        """Attempt Propose → Confirm → Commit for a booking task.

        Returns CommitResult.  Fails CLOSED (REJECTED) on any policy
        violation, PROVIDER_ERROR on adapter fault, SUCCESS with
        committed_values on success.

        Idempotency + evidence-invalidation checks live inside
        CommitCoordinator."""
        if not self._enabled:
            return None
        state = self.dialogue_state()
        if state is None or task_id not in state.agenda.tasks:
            return None
        task = state.agenda.tasks[task_id]

        try:
            proposal = self._coordinator.propose(
                kind=ActionKind.BOOK_APPOINTMENT,
                task=task, state=state,
                argument_map=argument_map,
            )
        except CommitPolicyError as e:
            log.info("propose rejected: %s", e)
            return None

        # Mark task as committing before we call the adapter
        try:
            for step in (TaskStatus.COLLECTING, TaskStatus.READY_TO_PROPOSE,
                         TaskStatus.AWAITING_CONFIRMATION, TaskStatus.COMMITTING):
                if task.status == step:
                    continue   # already past this step
                try:
                    reduce_patch(state, TransitionTaskPatch(
                        task_id=task_id, to_status=step,
                    ))
                except Exception:
                    pass   # transition may be a no-op or invalid; keep going
        except Exception:
            pass

        result = await self._coordinator.commit(
            proposal, caller_confirmations, task,
        )
        self._save_state(state)
        self._log_event(EventSourceKind.COMMIT, "book_result", {
            "task_id": task_id,
            "action_id": proposal.action_id,
            "outcome": result.outcome.value,
            "external_id": result.external_id,
            "error": result.error,
        })
        return result


# ── helper ──────────────────────────────────────────────────────────

def _kernel_enabled() -> bool:
    """Read settings lazily so tests can flip the flag between
    fixtures without a module-level snapshot."""
    try:
        from app.core.config import settings
        return bool(getattr(settings, "dialogue_kernel_enabled", False))
    except Exception:
        return False
