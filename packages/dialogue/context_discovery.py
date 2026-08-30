"""ContextDiscoveryOrchestrator — DISCOVER_CONTEXT branch of the audit.

2026-08-30 (task #150, from audit at docs/product/journey-audit-follow-up-clinic-2026-08-29.md):
audit's Gap 2 said "NextActionPolicy has no discovery branch for
ambiguous-context services." Root of the Christiaan / Abbas follow-up
problem: `resolve_service` returns MATCH_EXACT for 'A follow-up' →
policy fires ASK_SLOT → agent asks for phone → books a slot with no
context. In a real practice this is a false-complete: front desk
doesn't know which procedure the follow-up is for, which doctor,
whether the 30-day free window applies.

Adapted from LiveKit's `beta/workflows/task_group.py` — same shape,
different substrate. Key ideas ported:

  * Sequential task stack. Each "task" = one context slot to collect
    (original_procedure, original_provider, original_visit_date for
    a follow-up).
  * Regression tool: once at least one task is complete, LLM sees an
    auto-generated `regress_to(task_ids)` tool. Description enumerates
    visited task IDs. Caller changes their mind → LLM jumps back
    without our code needing to detect the pivot.
  * Local-scope prompt per task (LK's `AgentTask.instructions`) — we
    reuse the LK slot-capture prompt pattern from
    `packages/slot_parsers/slot_capture_prompts.py` for consistency.

## Not ported yet

  * chat_ctx summarization + merge back (LK's `_summarize` + merge)
    — our conversation state already carries the full transcript;
    downstream code doesn't need a compacted summary today. Deferred
    until we hit a token-budget wall.
  * `on_task_completed` async callback — YAGNI for now; observability
    events cover the same signal.

## Integration point

Brain checks `discovery.needs_context_for(service)` before firing
ASK_SLOT for booking-required slots. When context tasks are open,
the DISCOVER_CONTEXT branch of the policy renders + the LLM sees
per-task narrow scope. When all context tasks complete, the
orchestrator hands control back and normal ASK_SLOT resumes.

## Never raises

Every method is defensive. On any error → falls through to the
non-discovery path so calls don't crash on the discovery layer.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── task shapes ────────────────────────────────────────────────


class ContextTaskStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass
class ContextTask:
    """One discovery task (e.g. collect original_procedure)."""
    task_id: str
    description: str
    slot_key: str      # what we're collecting into known_slots
    ask_prompt: str    # what the agent SAYS to ask this question
    status: ContextTaskStatus = ContextTaskStatus.PENDING
    result: Optional[str] = None  # canonical answer, or None until collected

    @property
    def is_complete(self) -> bool:
        return self.status == ContextTaskStatus.COMPLETED


# ── registry of context requirements per service ─────────────────


# Per-service context requirements. Keys are canonical service names
# from the tenant's business profile; values are ordered lists of
# ContextTask templates.
#
# Extending: add a new entry here + the audit's fixture-side
# requires_context field will start reading from this map in a
# future commit. For now this is the single source of truth.
_SERVICE_CONTEXT_TASKS: dict[str, list[dict]] = {
    "Follow-up visit": [
        {
            "task_id": "original_procedure",
            "description": (
                "What procedure was this follow-up for (filling, "
                "cleaning, extraction, implant, root canal, etc.)"
            ),
            "slot_key": "original_procedure",
            "ask_prompt": (
                "Quick — a follow-up to what? Was that after a "
                "filling, a cleaning, something else?"
            ),
        },
        {
            "task_id": "original_provider",
            "description": (
                "Which doctor performed the original procedure"
            ),
            "slot_key": "original_provider",
            "ask_prompt": (
                "And who did the original work — do you remember "
                "which dentist you saw?"
            ),
        },
        {
            "task_id": "original_visit_date",
            "description": (
                "Roughly when the original visit was, to check the "
                "free-within-30-days window"
            ),
            "slot_key": "original_visit_date",
            "ask_prompt": (
                "About when was the original visit? Roughly is fine."
            ),
        },
    ],
    # Future: Implant consultation, Invisalign consultation, etc.
}


def context_tasks_for_service(service_name: Optional[str]) -> list[ContextTask]:
    """Return a fresh list of ContextTask templates for a service.

    Returns [] when the service doesn't need context (most bookings),
    or the service is unknown. Never raises.
    """
    if not service_name:
        return []
    try:
        templates = _SERVICE_CONTEXT_TASKS.get(service_name, [])
        return [ContextTask(**t) for t in templates]
    except Exception:
        return []


# ── orchestrator ──────────────────────────────────────────────


@dataclass
class ContextDiscoveryOrchestrator:
    """Sequenced discovery task runner + regression capability.

    Constructed fresh per call. Lives on `state._context_discovery`
    when active — brain checks its presence at each turn.
    """
    service_name: str
    tasks: OrderedDict[str, ContextTask] = field(default_factory=OrderedDict)
    _visited: set[str] = field(default_factory=set)

    @classmethod
    def for_service(cls, service_name: str) -> Optional["ContextDiscoveryOrchestrator"]:
        """Build an orchestrator for `service_name`, or None if it
        needs no discovery."""
        tasks = context_tasks_for_service(service_name)
        if not tasks:
            return None
        task_map = OrderedDict((t.task_id, t) for t in tasks)
        return cls(service_name=service_name, tasks=task_map)

    def current_task(self) -> Optional[ContextTask]:
        """Return the first non-complete task, or None if all done."""
        for task in self.tasks.values():
            if not task.is_complete:
                return task
        return None

    def is_complete(self) -> bool:
        """All discovery tasks complete → orchestrator can hand back
        control to the regular ASK_SLOT flow."""
        return self.current_task() is None

    def complete_current(self, result: str) -> Optional[ContextTask]:
        """Mark the currently-active task complete with `result`.
        Returns the NEXT pending task, or None if none left."""
        current = self.current_task()
        if current is not None:
            current.status = ContextTaskStatus.COMPLETED
            current.result = result
            self._visited.add(current.task_id)
        return self.current_task()

    def regress_to(self, task_ids: list[str]) -> None:
        """Reset the named tasks back to PENDING so the orchestrator
        re-asks them. Caller changed their mind about one of the
        earlier answers.

        Silently drops task_ids we don't recognize (never raises).
        Preserves order — the earliest requested regression fires
        first (matches LK's task_group behavior)."""
        if not task_ids:
            return
        for tid in task_ids:
            task = self.tasks.get(tid)
            if task is None:
                continue
            task.status = ContextTaskStatus.PENDING
            task.result = None
            self._visited.discard(tid)

    def visited_task_repr(self) -> dict[str, str]:
        """For the `regress_to` tool's dynamic description — LK
        pattern. Only tasks the caller has already answered are
        eligible for regression."""
        return {
            tid: task.description
            for tid, task in self.tasks.items()
            if tid in self._visited
        }

    def as_directive_note(self) -> str:
        """Render the current active task as a system-note directive
        the brain can inject into the LLM prompt. Used by
        DISCOVER_CONTEXT rendering.

        Returns empty string when nothing to ask.
        """
        current = self.current_task()
        if current is None:
            return ""
        visited = self.visited_task_repr()
        lines = [
            f"DISCOVERY MODE: You are collecting context for service "
            f"'{self.service_name}' before booking.",
            f"CURRENT TASK: {current.description}",
            f"ASK EXACTLY: '{current.ask_prompt}'",
            "",
            "MANDATORY: as soon as the caller answers, call the "
            "`answer_context_task` tool with their answer.  Do NOT "
            "just acknowledge and move on — the tool call is what "
            "advances the flow.  Do NOT proceed to booking or "
            "slot-selection until every discovery task is answered.",
            "",
            "Do NOT ask other questions until this one is answered.",
        ]
        if visited:
            lines.append(
                "REGRESSION: caller may reference already-answered "
                "context slots: "
                + ", ".join(f"{k} ({v})" for k, v in visited.items())
                + ".  If they want to change an earlier answer, call "
                "`regress_context_tasks` with the affected task IDs."
            )
        return "\n".join(lines)

    def to_summary(self) -> dict:
        """Compact snapshot for humanness event emission."""
        return {
            "service_name": self.service_name,
            "task_count": len(self.tasks),
            "visited_count": len(self._visited),
            "completed_count": sum(
                1 for t in self.tasks.values() if t.is_complete
            ),
            "current_task": (
                self.current_task().task_id if self.current_task() else None
            ),
        }


# ── LLM tool defs for the LK task-group pattern ────────────────


# The tool schemas the brain injects when discovery is active.
# Adapted from LK's beta/workflows/task_group.py — each active
# AgentTask exposes tools; here we mirror that with two callable
# names the LLM can invoke.
#
# `answer_context_task` — advance the current task with the answer
#   the caller just gave.  LLM extracts the answer from the caller's
#   utterance and passes it.  brain intercepts + calls
#   orchestrator.complete_current(answer).  Returns success receipt.
#
# `regress_context_tasks` — LK out_of_scope equivalent.  Caller
#   changed their mind about earlier answers.  LLM passes the task
#   IDs to reopen.  brain intercepts + calls orchestrator.regress_to().
#   Only injected when at least one task has been visited (LK parity).


def build_discovery_tools(orchestrator: "ContextDiscoveryOrchestrator") -> list:
    """Build the tool schemas the brain injects for the active
    discovery turn.  Returns [] when orchestrator is complete or None.

    Schemas match the ToolDefinition shape used elsewhere in this
    codebase (see packages/schemas/tool.py) — flat dicts with
    parameters.type=object.
    """
    if orchestrator is None or orchestrator.is_complete():
        return []
    current = orchestrator.current_task()
    if current is None:
        return []
    from packages.schemas import ToolDefinition
    tools = [
        ToolDefinition(
            name="answer_context_task",
            description=(
                f"Record the caller's answer to the CURRENT discovery "
                f"question.  Current task_id is {current.task_id!r}: "
                f"{current.description}.  Call this tool with the "
                f"caller's answer VERBATIM (or lightly cleaned) as "
                f"soon as they respond.  Do NOT proceed to booking "
                f"or slot-selection until this call succeeds."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "The caller's answer to the current "
                            "discovery question, verbatim or lightly "
                            "normalized (spelled digits → digits, "
                            "'yeah' → 'yes' where obvious, etc.)."
                        ),
                    },
                },
                "required": ["answer"],
            },
        ),
    ]
    # LK's out_of_scope tool: only inject once caller has answered
    # at least one task.  Empty visited set = nothing to regress to.
    visited = orchestrator.visited_task_repr()
    if visited:
        tools.append(ToolDefinition(
            name="regress_context_tasks",
            description=(
                "Reopen already-answered discovery tasks when the "
                "caller changes their mind about an earlier answer. "
                "Available task IDs (with their descriptions): "
                + ", ".join(
                    f"{k}: {v}" for k, v in visited.items()
                )
                + ". Pass the IDs in the order the caller mentioned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(visited.keys()),
                        },
                        "description": (
                            "One or more task IDs from the visited "
                            "set that the caller wants to change."
                        ),
                    },
                },
                "required": ["task_ids"],
            },
        ))
    return tools


def handle_discovery_tool_call(
    orchestrator: "ContextDiscoveryOrchestrator",
    tool_name: str,
    arguments: dict,
) -> Optional[dict]:
    """Intercept a discovery tool call and advance/regress the
    orchestrator.  Returns a synthetic tool receipt dict on match,
    or None when the tool_name is not a discovery tool (brain then
    falls through to normal tool_handler).

    Never raises — malformed arguments → error receipt so LLM gets
    feedback, no crash.
    """
    if orchestrator is None:
        return None
    if tool_name == "answer_context_task":
        try:
            answer = str(arguments.get("answer", "")).strip()
            if not answer:
                return {
                    "ok": False,
                    "error": "answer was empty; ask the caller again",
                }
            current = orchestrator.current_task()
            if current is None:
                return {
                    "ok": True,
                    "detail": "no active task; discovery already complete",
                }
            task_id = current.task_id
            orchestrator.complete_current(answer)
            nxt = orchestrator.current_task()
            return {
                "ok": True,
                "task_id": task_id,
                "answer": answer,
                "next_task_id": nxt.task_id if nxt else None,
                "discovery_complete": orchestrator.is_complete(),
            }
        except Exception as e:
            return {"ok": False, "error": f"answer_context_task: {e}"}
    if tool_name == "regress_context_tasks":
        try:
            task_ids = arguments.get("task_ids") or []
            if not isinstance(task_ids, list):
                return {
                    "ok": False,
                    "error": "task_ids must be a list of strings",
                }
            orchestrator.regress_to([str(t) for t in task_ids])
            nxt = orchestrator.current_task()
            return {
                "ok": True,
                "regressed_task_ids": task_ids,
                "next_task_id": nxt.task_id if nxt else None,
            }
        except Exception as e:
            return {"ok": False, "error": f"regress_context_tasks: {e}"}
    return None  # not a discovery tool — fall through to normal handler


__all__ = [
    "ContextTaskStatus",
    "ContextTask",
    "ContextDiscoveryOrchestrator",
    "context_tasks_for_service",
    "build_discovery_tools",
    "handle_discovery_tool_call",
]
