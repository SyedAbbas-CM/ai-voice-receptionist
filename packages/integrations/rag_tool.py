"""lookup_answer tool: shared across every vertical.

Wire it into a vertical's tool set with:
    tools = build_clinic_tools() + [build_lookup_answer_tool()]
    handler = ComposeHandler([ClinicToolHandler(...), LookupAnswerHandler(business, ...)])

The tool tells the brain: "here's a knowledge base for this business — call me
when the caller asks something I might know but isn't in the tool list already."

Return shape:
    {"answer": "Yes, we take Aetna PPO.", "source": "business.json:faqs.insurance",
     "confidence": 0.87}
or on low confidence / no match:
    {"answer": null, "reason": "no_confident_match", "confidence": 0.31}
which the brain uses to decide whether to escalate.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from packages.rag import Retriever, shape_for_voice
from packages.schemas import ToolCall, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


log = logging.getLogger(__name__)


def build_lookup_answer_tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup_answer",
        description=(
            "Look up an answer from the business knowledge base. "
            "Use this when the caller asks a factual question about the business "
            "(insurance, hours, services, policies, pricing, address, etc.) "
            "AND the answer isn't already in your system prompt. "
            "Do NOT use for booking / scheduling / escalation actions — those have their own tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The caller's question, in their own words.",
                },
            },
            "required": ["question"],
        },
    )


class LookupAnswerHandler:
    """Handles lookup_answer tool calls against a Retriever + Voice Shaper."""

    # Audit-3 fix (2026-08-04): explicit tool-name declaration so
    # ComposeHandler can route deterministically instead of string-matching
    # error messages.
    TOOL_NAMES = frozenset({"lookup_answer"})

    def __init__(
        self,
        business_id: str,
        retriever: Retriever,
        shaper_llm: "LLMProvider",
        confidence_threshold: float = 0.7,
        emit_evidence_bundle: bool = False,
    ):
        """Sprint 11b: `emit_evidence_bundle=True` switches output from
        prose ('answer' string) to structured EvidenceBundle
        ({answerability, claims, unsupported_parts, top_confidence}).
        Prose stays default for back-compat.  Once semantic planner is
        live end-to-end, flip default to True."""
        self.business_id = business_id
        self.retriever = retriever
        self.shaper_llm = shaper_llm
        self.confidence_threshold = confidence_threshold
        self.emit_evidence_bundle = emit_evidence_bundle

    def can_handle(self, tool_name: str) -> bool:
        """Explicit routing check for ComposeHandler."""
        return tool_name in self.TOOL_NAMES

    async def __call__(self, call: ToolCall) -> ToolResult:
        if call.name != "lookup_answer":
            return ToolResult(
                tool_call_id=call.id, name=call.name, result=None,
                error=f"unknown tool {call.name}: LookupAnswerHandler only handles lookup_answer",
            )
        question = (call.arguments.get("question") or "").strip()
        if not question:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={"answer": None, "reason": "empty_question", "confidence": 0.0},
            )
        try:
            hits = await self.retriever.search(question, business_id=self.business_id, top_k=3)
        except Exception as e:
            log.warning("RAG search failed: %s", e)
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={"answer": None, "reason": "search_error", "confidence": 0.0},
            )
        if not hits:
            if self.emit_evidence_bundle:
                from packages.rag import Answerability, EvidenceBundle
                bundle = EvidenceBundle(
                    question=question,
                    answerability=Answerability.UNSUPPORTED,
                    top_confidence=0.0, reason="no_match",
                )
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    result=bundle.model_dump(),
                )
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={"answer": None, "reason": "no_match", "confidence": 0.0},
            )

        # Sprint 11b: evidence-bundle output path.  Semantic planner
        # decides how to speak.  No voice_shaper LLM call here — that's
        # a downstream concern now.
        if self.emit_evidence_bundle:
            from packages.rag import build_bundle_from_hits
            bundle = build_bundle_from_hits(
                question=question, hits=hits,
                confidence_threshold=self.confidence_threshold,
            )
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result=bundle.model_dump(),
            )

        top = hits[0]
        if top.confidence < self.confidence_threshold:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "answer": None,
                    "reason": "low_confidence",
                    "confidence": round(top.confidence, 2),
                    "source": top.chunk.source,
                },
            )

        # Audit-3 fix (2026-08-04): previously the voice shaper only saw
        # hits[0].chunk.text.  Multi-part questions ("hours AND parking",
        # "insurance AND emergency policy") lost the supporting evidence.
        # Now we concatenate the top-K with a delimiter so the shaper can
        # combine or pick between sources.  Confidence stays anchored to
        # the top hit — a low-confidence supporting chunk shouldn't drag
        # the overall confidence up.
        supporting_hits = [
            h for h in hits[:3]
            if h.confidence >= self.confidence_threshold * 0.7
        ]
        combined_evidence = "\n\n---\n\n".join(
            f"[source: {h.chunk.source}]\n{h.chunk.text}"
            for h in supporting_hits
        ) or top.chunk.text

        try:
            shaped = await shape_for_voice(
                self.shaper_llm,
                question=question,
                retrieved_text=combined_evidence,
            )
        except Exception as e:
            log.warning("voice shaper failed: %s", e)
            shaped = ""

        if not shaped:
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={
                    "answer": None,
                    "reason": "unspeakable_or_no_answer",
                    "confidence": round(top.confidence, 2),
                    "source": top.chunk.source,
                },
            )

        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={
                "answer": shaped,
                "source": top.chunk.source,
                "sources": [h.chunk.source for h in supporting_hits],
                "confidence": round(top.confidence, 2),
            },
        )


class ComposeHandler:
    """Dispatch a ToolCall to the handler that OWNS its tool name.

    Audit-3 fix (2026-08-04): the previous version routed by matching
    the string "unknown tool" in a handler's error message.  That
    failed silently when a handler returned a differently-worded error
    (e.g. LookupAnswerHandler said "only handles lookup_answer"),
    dropping ALL non-RAG tool calls after RAG was composed in.

    New dispatch rule:
      1. If a handler exposes `can_handle(tool_name)`, use it.
      2. Otherwise, fall back to trying the handler and accepting any
         non-"unknown tool" result (legacy handlers without can_handle).
      3. Route to the FIRST handler that claims the tool.  Handlers
         later in the list only run if no earlier one claimed it.

    Handlers can be sync-or-async callables that take a ToolCall and
    return a ToolResult.  On no match, returns a structured
    "no handler matched" error so callers can distinguish that path."""

    def __init__(self, handlers: list):
        self.handlers = handlers

    def _claims(self, handler, tool_name: str) -> bool:
        can_handle = getattr(handler, "can_handle", None)
        if callable(can_handle):
            try:
                return bool(can_handle(tool_name))
            except Exception:
                return False
        # Legacy handler without can_handle — assume it might handle
        # the call and let the outer loop try it.  Preserves back-compat
        # with handlers written before the audit-3 refactor.
        return True

    async def __call__(self, call: ToolCall) -> ToolResult:
        last_error: Optional[ToolResult] = None
        legacy_tried = False
        for handler in self.handlers:
            claims = self._claims(handler, call.name)
            if not claims:
                continue
            try:
                result = await handler(call)
            except Exception as e:
                last_error = ToolResult(
                    tool_call_id=call.id, name=call.name, result=None, error=str(e),
                )
                continue
            # If handler exposed can_handle and claimed the tool, the
            # result is authoritative — don't try later handlers even
            # on error.  Only legacy handlers (no can_handle) fall
            # through on "unknown tool" so we keep back-compat.
            legacy = not callable(getattr(handler, "can_handle", None))
            if legacy:
                legacy_tried = True
                if result.error and "unknown tool" in (result.error or "").lower():
                    last_error = result
                    continue
            return result
        return last_error or ToolResult(
            tool_call_id=call.id, name=call.name, result=None,
            error=f"no handler matched {call.name}",
        )
