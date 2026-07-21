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

    def __init__(
        self,
        business_id: str,
        retriever: Retriever,
        shaper_llm: "LLMProvider",
        confidence_threshold: float = 0.7,
    ):
        self.business_id = business_id
        self.retriever = retriever
        self.shaper_llm = shaper_llm
        self.confidence_threshold = confidence_threshold

    async def __call__(self, call: ToolCall) -> ToolResult:
        if call.name != "lookup_answer":
            return ToolResult(
                tool_call_id=call.id, name=call.name, result=None,
                error="LookupAnswerHandler only handles lookup_answer",
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
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                result={"answer": None, "reason": "no_match", "confidence": 0.0},
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

        try:
            shaped = await shape_for_voice(
                self.shaper_llm,
                question=question,
                retrieved_text=top.chunk.text,
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
                "confidence": round(top.confidence, 2),
            },
        )


class ComposeHandler:
    """Dispatch a ToolCall to the first handler that accepts it.

    Handlers can be sync-or-async callables that take a ToolCall and return
    a ToolResult. On no match, the last handler's error is returned."""

    def __init__(self, handlers: list):
        self.handlers = handlers

    async def __call__(self, call: ToolCall) -> ToolResult:
        last_error: Optional[ToolResult] = None
        for handler in self.handlers:
            try:
                result = await handler(call)
            except Exception as e:
                last_error = ToolResult(
                    tool_call_id=call.id, name=call.name, result=None, error=str(e),
                )
                continue
            # If the handler returned an "unknown tool" error, try the next one.
            if result.error and "unknown tool" in (result.error or "").lower():
                last_error = result
                continue
            return result
        return last_error or ToolResult(
            tool_call_id=call.id, name=call.name, result=None,
            error=f"no handler matched {call.name}",
        )
