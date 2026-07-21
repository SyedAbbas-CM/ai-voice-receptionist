"""Regression tests for the tool_call_id serialization bug.

Bug history: Groq (and any OpenAI-spec LLM) returned 400 Bad Request after
the FIRST tool call in a session, because our transcript-to-messages
serializer emitted:

    {"role": "tool", "name": "book_appointment", "content": "..."}

instead of the required:

    {"role": "assistant", "content": "", "tool_calls": [{"id": ..., ...}]}
    {"role": "tool", "tool_call_id": ..., "content": "..."}

This test suite locks the correct shape in place. If it breaks, the demo
dies mid-call. Run: pytest apps/api/tests/test_tool_call_serialization.py
"""
from __future__ import annotations

import json

import pytest

from packages.schemas import (
    CallState,
    ToolCall,
    TranscriptTurn,
    TurnRole,
)


def _empty_state() -> CallState:
    return CallState(session_id="s1", business_id="biz1")


def test_plain_user_and_assistant_turns_serialize_openai_shape():
    state = _empty_state()
    state.add_turn(TranscriptTurn(role=TurnRole.USER, text="hi"))
    state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text="hello"))
    msgs = state.to_llm_messages()
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_assistant_tool_call_turn_emits_tool_calls_array():
    """When the LLM returns tool_calls, the assistant turn must carry them
    so the next round-trip serializes valid tool_call_id pairing."""
    state = _empty_state()
    state.add_turn(TranscriptTurn(role=TurnRole.USER, text="book me at 10am"))
    state.add_turn(TranscriptTurn(
        role=TurnRole.ASSISTANT,
        text="",
        tool_calls=[ToolCall(
            id="call_abc123",
            name="book_appointment",
            arguments={"time": "10:00", "name": "John"},
        )],
    ))
    msgs = state.to_llm_messages()
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == ""
    assert "tool_calls" in msgs[-1]
    tc = msgs[-1]["tool_calls"][0]
    assert tc["id"] == "call_abc123"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "book_appointment"
    # OpenAI/Groq expect arguments as a JSON STRING
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"time": "10:00", "name": "John"}


def test_tool_turn_carries_tool_call_id_binding_to_prior_assistant():
    """Groq specifically 400s if a tool message lacks tool_call_id or if the
    id doesn't match a preceding assistant tool_calls[i].id."""
    state = _empty_state()
    state.add_turn(TranscriptTurn(role=TurnRole.USER, text="book"))
    state.add_turn(TranscriptTurn(
        role=TurnRole.ASSISTANT, text="",
        tool_calls=[ToolCall(id="call_XYZ", name="book_appointment", arguments={})],
    ))
    state.add_turn(TranscriptTurn(
        role=TurnRole.TOOL,
        text="ok",
        tool_call_id="call_XYZ",
        tool_name="book_appointment",
        tool_result={"booked": True},
    ))
    msgs = state.to_llm_messages()
    tool_msg = msgs[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_XYZ"
    # Content must be JSON-serializable string, per OpenAI spec
    assert isinstance(tool_msg["content"], str)
    assert json.loads(tool_msg["content"]) == {"booked": True}


def test_tool_call_ids_pair_up_across_full_booking_flow():
    """End-to-end shape: user -> assistant(tool_calls) -> tool(tool_call_id) -> assistant(reply).
    Every tool message's tool_call_id must appear in a prior assistant's tool_calls[i].id."""
    state = _empty_state()
    state.add_turn(TranscriptTurn(role=TurnRole.USER, text="book Tuesday 10am John Smith 5551234"))
    state.add_turn(TranscriptTurn(
        role=TurnRole.ASSISTANT, text="",
        tool_calls=[
            ToolCall(id="call_1", name="check_availability", arguments={"date": "Tue", "time": "10:00"}),
        ],
    ))
    state.add_turn(TranscriptTurn(
        role=TurnRole.TOOL, text="", tool_call_id="call_1",
        tool_name="check_availability", tool_result={"available": True},
    ))
    state.add_turn(TranscriptTurn(
        role=TurnRole.ASSISTANT, text="",
        tool_calls=[
            ToolCall(id="call_2", name="book_appointment", arguments={"name": "John Smith"}),
        ],
    ))
    state.add_turn(TranscriptTurn(
        role=TurnRole.TOOL, text="", tool_call_id="call_2",
        tool_name="book_appointment", tool_result={"booked": True},
    ))
    state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text="Booked, see you Tuesday."))

    msgs = state.to_llm_messages()
    # Extract every id emitted by an assistant tool_calls block
    assistant_ids: set[str] = set()
    for m in msgs:
        if m["role"] == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                assistant_ids.add(tc["id"])
    # Every tool message must reference one of those ids
    for m in msgs:
        if m["role"] == "tool":
            assert m["tool_call_id"] in assistant_ids, (
                f"tool msg refers to unbound id {m['tool_call_id']!r}, "
                f"known assistant ids: {assistant_ids}"
            )


def test_tool_result_dict_gets_json_encoded_not_repr():
    """Prior serializer used str(dict) which produces "{'a': 1}" — that's not
    valid JSON. LLMs sometimes cope, sometimes not. Force real JSON."""
    state = _empty_state()
    state.add_turn(TranscriptTurn(
        role=TurnRole.TOOL,
        text="", tool_call_id="c1", tool_name="x",
        tool_result={"foo": "bar", "n": 3, "nested": {"k": [1, 2]}},
    ))
    content = state.to_llm_messages()[-1]["content"]
    # Must be JSON, not a python repr
    parsed = json.loads(content)
    assert parsed == {"result": {"foo": "bar", "n": 3, "nested": {"k": [1, 2]}}} \
        or parsed == {"foo": "bar", "n": 3, "nested": {"k": [1, 2]}}


def test_assistant_null_text_serializes_as_empty_string_not_none():
    """OpenAI spec: content must be a string. `None` triggers 400."""
    state = _empty_state()
    state.add_turn(TranscriptTurn(
        role=TurnRole.ASSISTANT, text="",
        tool_calls=[ToolCall(id="c1", name="foo", arguments={})],
    ))
    msg = state.to_llm_messages()[-1]
    assert msg["content"] == ""
    assert msg["content"] is not None
