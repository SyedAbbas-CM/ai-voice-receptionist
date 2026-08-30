"""LLM-as-judge module (LK-steal, task #96).

Ported from LiveKit Agents' `evals/judge.py`. Runs canned or custom
judges over a call transcript and returns pass/fail/maybe verdicts +
reasoning. Auto-labels land in `call_annotations.auto_labels` next
to the human tags.

## Why this shape

LK's key insight: force the judge LLM to call a **mandatory
`submit_verdict(verdict, reasoning)` tool** (temp=0, tool_choice=
required). Parsing free-text verdicts is unreliable — LLMs hedge,
qualify, or refuse. A required tool call with an enum-constrained
verdict field is a hard schema check.

## Canned judges

Each returns pass|fail|maybe + one-sentence reasoning:

- `task_completion` — did the agent do what the caller wanted?
- `accuracy`         — is what the agent said factually correct
                       given the tool results in the transcript?
- `tool_use`         — did the agent use tools when it should have?
- `coherence`        — does the conversation flow logically?
- `relevancy`        — are the agent's responses on-topic?

Additional judges are trivial to add: subclass `Judge`, define
`instructions`. All prompts are lifted verbatim from LK's file — they
are battle-tested against production traffic.

## Not in v1

- No mid-call judging — runs post-call only. Real-time gating would
  need a different shape.
- No golden-corpus reference-comparison — Phase 4 (task #97) adds a
  `reference_chat_ctx` param that walks the diff.
- No aggregate scoring across many calls — that's a dashboard feature
  built on top of `auto_labels`.
- No LLM cost tracking per judge — add if judge spend becomes real.

## Wire status

Module + tests only. Wire-up in a follow-up commit:
1. New background worker `packages/evals/background_judge_runner.py`
   listens for CallEndedEvent, loads transcript, runs JudgeGroup,
   writes auto_labels to call_annotations.
2. Feature-flag `ENABLE_AUTO_JUDGES=true` in .env.
3. Judges use the tenant's LLM router with model override to
   gpt-4o-mini (or configured `EVAL_JUDGE_MODEL`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ─── verdict + result types ─────────────────────────────────────────────────

Verdict = str  # "pass" | "fail" | "maybe"


@dataclass
class JudgmentResult:
    """One judge's output for one call."""
    judge_name: str
    verdict: Verdict
    reasoning: str
    # None on LLM error; string with error message otherwise. Judges
    # never raise — a failed judge returns verdict="maybe" +
    # reasoning="judge_error: ..." so the batch keeps going.
    error: Optional[str] = None


@dataclass
class EvaluationResult:
    """Aggregate of many judges' output for one call."""
    call_id: str
    judgments: dict[str, JudgmentResult] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """0.0-1.0. pass=1, maybe=0.5, fail=0. Mean across judges."""
        if not self.judgments:
            return 0.0
        vals = {"pass": 1.0, "maybe": 0.5, "fail": 0.0}
        s = sum(vals.get(j.verdict, 0.0) for j in self.judgments.values())
        return s / len(self.judgments)

    @property
    def all_passed(self) -> bool:
        # Empty set != "all passed" — Python's all() returns True on
        # empty iterables, which would be misleading here. If nothing
        # was judged, there is no basis to claim anything passed.
        if not self.judgments:
            return False
        return all(j.verdict == "pass" for j in self.judgments.values())

    @property
    def any_failed(self) -> bool:
        return any(j.verdict == "fail" for j in self.judgments.values())

    @property
    def majority_passed(self) -> bool:
        if not self.judgments:
            return False
        passed = sum(1 for j in self.judgments.values() if j.verdict == "pass")
        return passed > len(self.judgments) / 2

    def to_dict(self) -> dict:
        """Serialize to the `auto_labels` JSON column shape."""
        return {
            name: {
                "verdict": j.verdict,
                "reasoning": j.reasoning,
                "error": j.error,
            }
            for name, j in self.judgments.items()
        }


# ─── transcript formatting ──────────────────────────────────────────────────


def format_chat_ctx(transcript: list[dict]) -> str:
    """Flatten a transcript row-list into the LK-style text the judge
    prompt reads. Handles the shapes we persist:

      * `{"role": "user"|"assistant", "text": "..."}`  → `role: text`
      * `{"role": "tool", "tool_name": "X", "tool_args": {...},
         "tool_result": {...}}` → `[tool call: X(args)]`,
         `[tool output: ...]`
      * `{"role": "assistant", "agent_instructions_delta": "..."}`
         → `[agent config: instructions=...]` on the LAST such row
         (judges want current effective instructions, not history).

    Unknown roles are printed literally so the judge sees them.
    """
    lines: list[str] = []
    for row in transcript:
        role = row.get("role", "?")
        text = row.get("text", "") or ""
        tool_name = row.get("tool_name")
        tool_args = row.get("tool_args")
        tool_result = row.get("tool_result")
        tool_error = row.get("tool_error")
        instr_delta = row.get("agent_instructions_delta")

        if role == "tool" or tool_name:
            name = tool_name or "?"
            args_str = json.dumps(tool_args, default=str) if tool_args else "{}"
            lines.append(f"[tool call: {name}({args_str})]")
            if tool_error:
                lines.append(f"[tool error: {tool_error}]")
            elif tool_result is not None:
                result_str = json.dumps(tool_result, default=str)
                lines.append(f"[tool output: {result_str[:500]}]")
        else:
            role_tag = "caller" if role == "user" else "agent" if role == "assistant" else role
            if text.strip():
                lines.append(f"{role_tag}: {text}")

        if instr_delta:
            # Instruction change during the call — judge should know
            # about scope changes when grading turns.
            lines.append(f"[agent config: instructions_delta={instr_delta[:200]}...]")

    return "\n".join(lines)


# ─── the mandatory-tool-call trick ──────────────────────────────────────────
# LK's reliability win. We tell the LLM "you MUST call submit_verdict"
# and give it exactly one tool. Its response is 100% required to
# contain a tool_call, and the tool's args schema is an enum-constrained
# verdict + free reasoning. No free-text parsing.

_SUBMIT_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Submit your final verdict on the criteria. You MUST call this exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "fail", "maybe"],
                    "description": (
                        "'pass' when criteria clearly met. 'fail' when clearly not met. "
                        "'maybe' when ambiguous — evidence in both directions or the "
                        "transcript doesn't contain enough signal to judge."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "One or two sentences citing the specific turn(s) that support "
                        "your verdict. Reference caller/agent lines by content, not turn "
                        "number."
                    ),
                },
            },
            "required": ["verdict", "reasoning"],
            "additionalProperties": False,
        },
    },
}


# ─── canned judge instructions ──────────────────────────────────────────────
# Verbatim from LK's evals/judge.py, adapted where the phrasing was
# LK-specific.

_INSTRUCTIONS: dict[str, str] = {
    "task_completion": (
        "Judge whether the agent RESOLVED the caller's request during this call. "
        "Use the LiveKit definition of 'First Call Resolution': the caller got "
        "what they came for in ONE call, without needing to call back. "
        "PASS if the request was clearly resolved (booking made, question "
        "answered, transfer completed). FAIL if the caller hung up frustrated, "
        "or if the request was clearly not addressed. MAYBE if the call ended "
        "ambiguously (caller said 'I'll think about it', agent promised "
        "follow-up we can't verify)."
    ),
    "accuracy": (
        "Judge whether every factual statement the AGENT made is supported by "
        "the tool outputs, agent instructions, or caller-provided information "
        "visible in this transcript. PASS if all agent statements check out. "
        "FAIL if the agent hallucinated a fact (invented a phone number, "
        "quoted a service that isn't in the business's offerings, promised a "
        "time slot that tool output showed as unavailable). MAYBE if a claim "
        "was made that isn't disprovable from the transcript."
    ),
    "tool_use": (
        "Judge whether the agent used TOOLS appropriately. PASS if tools were "
        "called when the situation required them (check_availability before "
        "quoting slots, book_appointment when the caller committed, "
        "resolve_service when a service name was ambiguous). FAIL if the agent "
        "invented tool-worthy information without calling the tool (quoted "
        "slots without check_availability, claimed a booking without "
        "book_appointment). MAYBE if the tool wasn't strictly needed but its "
        "use would have improved the answer."
    ),
    "coherence": (
        "Judge whether the conversation FLOWS logically turn-to-turn. PASS if "
        "each agent response follows from the caller's last utterance + prior "
        "context. FAIL if the agent contradicted itself, ignored the caller's "
        "last question, or looped on the same question multiple times (a "
        "classic 'stuck agent' failure). MAYBE for minor awkwardness that "
        "didn't derail the call."
    ),
    "relevancy": (
        "Judge whether each agent response is ON-TOPIC for the caller's "
        "request. PASS if responses stayed relevant to the booking / question / "
        "service inquiry throughout. FAIL if the agent drifted into unrelated "
        "topics, or offered information the caller didn't ask for that "
        "confused the flow. MAYBE if a tangent was brief and returned to "
        "topic."
    ),
}


# ─── judge base + factory functions ─────────────────────────────────────────


class Judge:
    """Base class. Subclass + override `instructions` + optionally
    `should_run(transcript)` if the judge has a prerequisite check
    (e.g. handoff judge auto-passes when no handoff occurred)."""

    name: str = "base"
    instructions: str = ""

    def should_run(self, transcript: list[dict]) -> bool:
        """Return False to auto-pass without hitting the LLM. Default
        runs always."""
        return True

    async def evaluate(
        self,
        transcript: list[dict],
        llm_caller: Callable,
    ) -> JudgmentResult:
        """Score the transcript. `llm_caller` is an async fn:
            async def caller(messages: list[dict], tools: list[dict]) -> dict
        Returns a JudgmentResult; never raises."""
        if not self.should_run(transcript):
            return JudgmentResult(
                judge_name=self.name,
                verdict="pass",
                reasoning="prerequisite not met — auto-pass",
            )

        formatted = format_chat_ctx(transcript)
        prompt = (
            f"Criteria: {self.instructions}\n\n"
            f"Conversation:\n{formatted}\n\n"
            f"Evaluate if the conversation meets the criteria and call "
            f"submit_verdict."
        )
        messages = [
            {"role": "system", "content": "You are an evaluator. Follow the criteria strictly."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await llm_caller(messages, [_SUBMIT_VERDICT_TOOL])
        except Exception as e:  # network, rate limit, whatever
            log.warning("judge %s LLM call failed: %r", self.name, e)
            return JudgmentResult(
                judge_name=self.name,
                verdict="maybe",
                reasoning=f"judge_error: {type(e).__name__}",
                error=str(e),
            )

        return _parse_verdict_response(self.name, response)


def _parse_verdict_response(judge_name: str, response: dict) -> JudgmentResult:
    """Extract verdict + reasoning from the LLM response. Fail-open to
    maybe with a diagnostic reasoning if extraction fails."""
    tool_calls = response.get("tool_calls") or []
    if not tool_calls:
        # LLM refused the mandatory tool call — treat as maybe.
        return JudgmentResult(
            judge_name=judge_name,
            verdict="maybe",
            reasoning="judge_error: LLM did not call submit_verdict",
            error="no_tool_call",
        )
    call = tool_calls[0]
    if isinstance(call, dict):
        args_raw = (call.get("function") or {}).get("arguments") or call.get("arguments") or "{}"
    else:
        args_raw = "{}"
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            return JudgmentResult(
                judge_name=judge_name,
                verdict="maybe",
                reasoning=f"judge_error: could not parse tool args: {args_raw[:100]}",
                error="json_parse",
            )
    else:
        args = args_raw or {}

    verdict = str(args.get("verdict", "maybe")).lower()
    if verdict not in ("pass", "fail", "maybe"):
        verdict = "maybe"
    reasoning = str(args.get("reasoning", "")).strip() or "no reasoning provided"
    return JudgmentResult(
        judge_name=judge_name,
        verdict=verdict,
        reasoning=reasoning[:500],
    )


class _CannedJudge(Judge):
    """Instances built from the _INSTRUCTIONS dict."""

    def __init__(self, name: str, instructions: str) -> None:
        self.name = name
        self.instructions = instructions


def task_completion_judge() -> Judge:
    return _CannedJudge("task_completion", _INSTRUCTIONS["task_completion"])


def accuracy_judge() -> Judge:
    return _CannedJudge("accuracy", _INSTRUCTIONS["accuracy"])


def tool_use_judge() -> Judge:
    return _CannedJudge("tool_use", _INSTRUCTIONS["tool_use"])


def coherence_judge() -> Judge:
    return _CannedJudge("coherence", _INSTRUCTIONS["coherence"])


def relevancy_judge() -> Judge:
    return _CannedJudge("relevancy", _INSTRUCTIONS["relevancy"])


def default_judge_panel() -> list[Judge]:
    """The 5 canned judges. Callers can add custom ones by appending
    to this list before calling JudgeGroup.evaluate."""
    return [
        task_completion_judge(),
        accuracy_judge(),
        tool_use_judge(),
        coherence_judge(),
        relevancy_judge(),
    ]


# ─── group runner ───────────────────────────────────────────────────────────


class JudgeGroup:
    """Run many judges concurrently against one transcript."""

    def __init__(self, judges: list[Judge]) -> None:
        self._judges = judges

    async def evaluate(
        self,
        call_id: str,
        transcript: list[dict],
        llm_caller: Callable,
    ) -> EvaluationResult:
        """Run all judges concurrently. Each judge's error is isolated
        — one bad judge doesn't cancel the batch."""
        tasks = [j.evaluate(transcript, llm_caller) for j in self._judges]
        judgments = await asyncio.gather(*tasks, return_exceptions=False)
        return EvaluationResult(
            call_id=call_id,
            judgments={j.judge_name: j for j in judgments},
        )
