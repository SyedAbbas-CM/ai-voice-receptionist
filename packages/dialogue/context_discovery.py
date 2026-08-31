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
            "",
            "TURN LOGIC — READ CAREFULLY:",
            f"  * If the caller has NOT YET answered this task in this "
            f"turn: ASK: '{current.ask_prompt}' AND WAIT for them to "
            f"respond. Do NOT call any tool this turn.",
            f"  * If the caller JUST answered this task in this turn "
            f"(their utterance contains the answer): call "
            f"`answer_context_task(answer=<their answer>)` and then "
            f"either move on to the NEXT discovery question the tool "
            f"receipt tells you (via `next_task_id`) OR — if the tool "
            f"receipt says `discovery_complete: true` — proceed to "
            f"booking.",
            "",
            "CRITICAL — never do BOTH in the same turn:",
            f"  BAD:  say '{current.ask_prompt}' AND call "
            f"answer_context_task in the same turn — the caller hears "
            f"the question RE-ASKED even though you just recorded "
            f"their answer.",
            f"  GOOD: EITHER ask (waiting) OR record+advance (moving "
            f"on). One or the other, never both.",
            "",
            "Do NOT ask other questions until this one is answered. "
            "Do NOT proceed to booking or slot-selection until every "
            "discovery task is answered.",
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

    def collected_answers(self) -> dict[str, str]:
        """Return {task_id: answer} for every completed task.
        Empty dict if nothing collected yet.

        Used by brain's booking-tool arg augmenter (task #144) to
        populate notes= on book_appointment with the discovery
        context so front-desk staff see procedure/provider/date on
        follow-up bookings.
        """
        return {
            tid: task.result
            for tid, task in self.tasks.items()
            if task.is_complete and task.result
        }

    def as_notes_prefix(self) -> str:
        """Render collected answers as a compact string suitable for
        prepending to book_appointment(notes=).

        Format matches what a real receptionist would type in the
        notes field: 'Follow-up to filling by Dr. Chen on August 15th.'

        Returns empty string if no answers collected.
        """
        answers = self.collected_answers()
        if not answers:
            return ""
        parts = []
        procedure = answers.get("original_procedure")
        provider = answers.get("original_provider")
        date = answers.get("original_visit_date")
        if procedure:
            parts.append(f"Follow-up to {procedure}")
        if provider:
            parts.append(f"with {provider}")
        if date:
            parts.append(f"on {date}")
        return " ".join(parts).strip()

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


# ── grounding guard (BUG #157 fix, from CAc66749) ──────────────

# Common English stopwords ignored during grounding check.  Answers
# like 'the crown' should ground on 'crown' alone, not require 'the'
# to also appear in the caller utterance.
_GROUNDING_STOPWORDS = frozenset({
    "the", "a", "an", "of", "on", "in", "at", "to", "for", "with",
    "by", "and", "or", "it", "was", "were", "is", "are", "be", "been",
    "my", "your", "his", "her", "our", "their", "some", "any", "that",
    "this", "these", "those", "there", "then", "so", "just", "also",
    "as", "if", "not", "no", "yes", "one", "two", "actually",
    "kind", "sort", "type", "thing", "stuff", "you", "know",
    # 2026-08-31 CALL-BUG-13: honorific / title words are never
    # grounding signals — caller may say "it was doctor whitfield"
    # or just "whitfield". Requiring "doctor" to appear in the
    # utterance is a false-negative source.
    "doctor", "dr", "mr", "mrs", "ms", "miss", "dentist", "hygienist",
    "provider", "person",
})


def _grounding_tokens(text: str) -> set[str]:
    """Lowercased word tokens minus stopwords.  Words are stripped of
    non-alphanumeric trailing chars and stemmed loosely (trailing 's'
    dropped) so 'crown' / 'Crown' / 'crowns' all match."""
    if not text:
        return set()
    import re as _re
    words = _re.findall(r"[A-Za-z][A-Za-z0-9]*", text.lower())
    out: set[str] = set()
    for w in words:
        if w in _GROUNDING_STOPWORDS:
            continue
        # Loose stem: drop trailing s so plurals collapse.  Keep
        # 2-letter minimum so we don't match single chars.
        if len(w) >= 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) >= 2:
            out.add(w)
    return out


def _answer_is_grounded(
    answer: str, recent_caller_texts: Optional[list[str]],
) -> bool:
    """True when every substantive word in `answer` also appears in
    the caller's recent utterances (exact OR phonetically similar).

    2026-08-31 (BUG #157 fix from CAc66749): LLM under time pressure
    hallucinated 'Chen' when caller said only 'It was doctor.'  The
    tool receipt then advanced the discovery orchestrator with a
    made-up provider name.  This guard rejects answers whose content
    words aren't in the caller's transcript.

    2026-08-31 (CALL-BUG-13 from CA255fe8c231): strict subset was
    too tight for proper nouns. Real trace: caller said "the upgrade
    field" (STT mishear of "Doctor Whitfield"), LLM correctly
    normalized to "Whitfield" from context, guard refused because
    'whitfield' not in {'upgrade', 'field'}. Now: fall back to
    metaphone-equivalence or Jaro-Winkler >= 0.85 for the last word
    of the answer — that word is usually the surname and the phonetic
    match against the STT-garbled version proves the LLM isn't
    hallucinating from thin air.

    Falls back to permissive (True) when recent_caller_texts is None
    or empty — never let the guard break brain on missing context.
    """
    if not recent_caller_texts:
        return True  # permissive fallback
    answer_tokens = _grounding_tokens(answer)
    if not answer_tokens:
        return True  # all stopwords / empty after tokenization
    haystack: set[str] = set()
    for t in recent_caller_texts:
        haystack.update(_grounding_tokens(t))
    # Fast path: exact-word subset.
    if answer_tokens.issubset(haystack):
        return True
    # Phonetic fallback: for each answer token missing from haystack,
    # accept if any utterance token is phonetically similar. Guards
    # against STT-mangled proper nouns (Whitfield → "upgrade field",
    # Ravi → "raw view", Syed → "seth").
    try:
        import jellyfish as _jf
        missing = answer_tokens - haystack
        for miss in missing:
            miss_meta = _jf.metaphone(miss)
            miss_lower = miss.lower()
            phonetic_hit = False
            for tok in haystack:
                # Metaphone equivalence (e.g. "whitfield"/"WTFLT"
                # matches "field"/"FLT" as a suffix)
                tok_meta = _jf.metaphone(tok)
                if miss_meta and tok_meta and (
                    miss_meta == tok_meta
                    or miss_meta.endswith(tok_meta)
                    or tok_meta.endswith(miss_meta)
                ):
                    phonetic_hit = True
                    break
                # Jaro-Winkler at 0.85+ catches "chen"/"gwen",
                # "whitfield"/"wittfield" style near-matches
                if _jf.jaro_winkler_similarity(miss_lower, tok.lower()) >= 0.85:
                    phonetic_hit = True
                    break
            if not phonetic_hit:
                return False
        return True
    except Exception:
        # Jellyfish missing or errored — fall back to strict subset
        # (already computed above as False).
        return False


def handle_discovery_tool_call(
    orchestrator: "ContextDiscoveryOrchestrator",
    tool_name: str,
    arguments: dict,
    recent_caller_texts: Optional[list[str]] = None,
) -> Optional[dict]:
    """Intercept a discovery tool call and advance/regress the
    orchestrator.  Returns a synthetic tool receipt dict on match,
    or None when the tool_name is not a discovery tool (brain then
    falls through to normal tool_handler).

    `recent_caller_texts` (BUG #157): when provided, the answer must
    be grounded in the caller's actual utterance to be accepted.
    Rejects LLM hallucinations like 'Chen' when caller only said
    'It was doctor.'  Missing → permissive fallback for backcompat.

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
            # BUG #157 grounding guard.
            if not _answer_is_grounded(answer, recent_caller_texts):
                return {
                    "ok": False,
                    "error": (
                        "answer is not grounded in the caller's "
                        "utterance; do not invent details — ask the "
                        "caller to clarify"
                    ),
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
