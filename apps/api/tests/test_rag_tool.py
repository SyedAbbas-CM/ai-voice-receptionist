"""lookup_answer tool + ComposeHandler tests."""
from __future__ import annotations

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.integrations.rag_tool import (
    ComposeHandler,
    LookupAnswerHandler,
    build_lookup_answer_tool,
)
from packages.rag import Chunk, ChunkKind, RetrievalHit
from packages.schemas import ToolCall, ToolResult


class ScriptedLLM(LLMProvider):
    name = "scripted"

    def __init__(self, response_text: str = "Yes, we take Aetna PPO plans."):
        self.response_text = response_text
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        return LLMResponse(text=self.response_text)


class StubRetriever:
    """A retriever we control precisely — no DB, no embedder."""

    def __init__(self, hits: list[RetrievalHit]):
        self.hits = hits
        self.last_query = None

    async def search(self, query: str, business_id: str, top_k: int = 3):
        self.last_query = query
        return self.hits


def _chunk(text: str, source: str = "business.json:faqs.insurance") -> Chunk:
    return Chunk(text=text, business_id="c1", source=source, kind=ChunkKind.FAQ)


def test_build_lookup_answer_tool_shape():
    tool = build_lookup_answer_tool()
    assert tool.name == "lookup_answer"
    assert "question" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["question"]


@pytest.mark.asyncio
async def test_returns_answer_on_high_confidence():
    hits = [RetrievalHit(chunk=_chunk("Q: Insurance?\nA: Yes Aetna."), score=1.0, confidence=0.9)]
    handler = LookupAnswerHandler(
        business_id="c1",
        retriever=StubRetriever(hits),
        shaper_llm=ScriptedLLM("Yes, we take Aetna PPO."),
    )
    result = await handler(ToolCall(id="t1", name="lookup_answer", arguments={"question": "Do you take Aetna?"}))
    assert result.error is None
    assert result.result["answer"] == "Yes, we take Aetna PPO."
    assert result.result["source"].startswith("business.json:")
    assert result.result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_returns_none_on_low_confidence():
    hits = [RetrievalHit(chunk=_chunk("Q: Parking\nA: In lot"), score=0.2, confidence=0.35)]
    handler = LookupAnswerHandler(
        business_id="c1",
        retriever=StubRetriever(hits),
        shaper_llm=ScriptedLLM(),
        confidence_threshold=0.7,
    )
    result = await handler(ToolCall(id="t1", name="lookup_answer", arguments={"question": "Do you take Aetna?"}))
    assert result.result["answer"] is None
    assert result.result["reason"] == "low_confidence"
    assert result.result["confidence"] == 0.35


@pytest.mark.asyncio
async def test_returns_none_on_no_hits():
    handler = LookupAnswerHandler(
        business_id="c1",
        retriever=StubRetriever([]),
        shaper_llm=ScriptedLLM(),
    )
    result = await handler(ToolCall(id="t1", name="lookup_answer", arguments={"question": "?"}))
    assert result.result["answer"] is None
    assert result.result["reason"] == "no_match"


@pytest.mark.asyncio
async def test_returns_none_when_shaper_returns_unspeakable():
    hits = [RetrievalHit(chunk=_chunk("KB text"), score=1.0, confidence=0.9)]
    handler = LookupAnswerHandler(
        business_id="c1",
        retriever=StubRetriever(hits),
        shaper_llm=ScriptedLLM("Visit https://example.com for details"),   # URL -> rejected
    )
    result = await handler(ToolCall(id="t1", name="lookup_answer", arguments={"question": "?"}))
    assert result.result["answer"] is None
    assert result.result["reason"] == "unspeakable_or_no_answer"


@pytest.mark.asyncio
async def test_empty_question_short_circuits():
    handler = LookupAnswerHandler(
        business_id="c1",
        retriever=StubRetriever([]),
        shaper_llm=ScriptedLLM(),
    )
    result = await handler(ToolCall(id="t1", name="lookup_answer", arguments={"question": "   "}))
    assert result.result["reason"] == "empty_question"


@pytest.mark.asyncio
async def test_wrong_tool_name_returns_error():
    handler = LookupAnswerHandler(
        business_id="c1", retriever=StubRetriever([]), shaper_llm=ScriptedLLM(),
    )
    result = await handler(ToolCall(id="t1", name="some_other_tool", arguments={}))
    assert result.error is not None


# ---- ComposeHandler ----

async def _rag_handler_yes(call: ToolCall) -> ToolResult:
    if call.name == "lookup_answer":
        return ToolResult(tool_call_id=call.id, name=call.name, result={"answer": "rag-answered"})
    return ToolResult(tool_call_id=call.id, name=call.name, result=None, error="unknown tool: rag")


async def _vertical_handler(call: ToolCall) -> ToolResult:
    if call.name in ("check_availability", "book_appointment"):
        return ToolResult(tool_call_id=call.id, name=call.name, result={"handled": "vertical"})
    return ToolResult(tool_call_id=call.id, name=call.name, result=None, error=f"unknown tool: {call.name}")


@pytest.mark.asyncio
async def test_compose_routes_lookup_answer_to_rag():
    compose = ComposeHandler([_rag_handler_yes, _vertical_handler])
    result = await compose(ToolCall(id="t1", name="lookup_answer", arguments={"question": "hi"}))
    assert result.result == {"answer": "rag-answered"}


@pytest.mark.asyncio
async def test_compose_routes_booking_to_vertical():
    compose = ComposeHandler([_rag_handler_yes, _vertical_handler])
    result = await compose(ToolCall(id="t1", name="book_appointment", arguments={}))
    assert result.result == {"handled": "vertical"}


@pytest.mark.asyncio
async def test_compose_returns_last_error_when_no_handler_matches():
    compose = ComposeHandler([_rag_handler_yes, _vertical_handler])
    result = await compose(ToolCall(id="t1", name="never_heard_of_it", arguments={}))
    assert result.error is not None
