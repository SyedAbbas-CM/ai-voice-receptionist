"""Task #247: cross-family structured-output helper tests.
Verifies each family's translation from schema → native request field."""
from pydantic import BaseModel

from app.providers.llm.structured_output import (
    _to_json_schema,
    _schema_name,
    openai_response_format,
    openai_json_object_mode,
    anthropic_tool_for_schema,
    anthropic_tool_choice_for_schema,
    gemini_generation_config,
    prompt_wrap_for_schema,
    parse_and_validate,
)


class ReactiveReply(BaseModel):
    should_speak: bool
    backchannel: str | None = None
    committed_reply: str | None = None


def test_to_json_schema_from_pydantic():
    s = _to_json_schema(ReactiveReply)
    assert s["type"] == "object"
    assert "should_speak" in s["properties"]


def test_to_json_schema_from_dict_passthrough():
    d = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert _to_json_schema(d) == d


def test_to_json_schema_none():
    assert _to_json_schema(None) is None


def test_openai_strict_json_schema():
    rf = openai_response_format(ReactiveReply, strict=True)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "ReactiveReply"


def test_openai_loose_json_object():
    rf = openai_response_format(ReactiveReply, strict=False)
    assert rf == {"type": "json_object"}


def test_openai_no_schema_returns_none():
    assert openai_response_format(None) is None


def test_anthropic_tool_shape():
    tool = anthropic_tool_for_schema(ReactiveReply)
    assert tool["name"] == "emit_reply"
    assert "input_schema" in tool
    assert tool["input_schema"]["type"] == "object"


def test_anthropic_tool_choice_forces_the_tool():
    tc = anthropic_tool_choice_for_schema(ReactiveReply)
    assert tc == {"type": "tool", "name": "emit_reply"}


def test_gemini_generation_config_injects_schema():
    cfg = gemini_generation_config(ReactiveReply, base={"temperature": 0.3})
    assert cfg["temperature"] == 0.3
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseSchema"]["type"] == "object"


def test_gemini_no_schema_returns_base_unchanged():
    cfg = gemini_generation_config(None, base={"temperature": 0.3})
    assert cfg == {"temperature": 0.3}


def test_prompt_wrap_prepends_json_instruction():
    msgs = [{"role": "user", "content": "hi"}]
    wrapped = prompt_wrap_for_schema(msgs, ReactiveReply)
    assert wrapped[0]["role"] == "system"
    assert "JSON Schema" in wrapped[0]["content"]
    assert wrapped[1] == msgs[0]


def test_parse_and_validate_success():
    r = parse_and_validate('{"should_speak": true, "backchannel": null, "committed_reply": "hi"}', ReactiveReply)
    assert isinstance(r, ReactiveReply)
    assert r.should_speak is True


def test_parse_and_validate_strips_markdown():
    r = parse_and_validate('```json\n{"should_speak": false}\n```', ReactiveReply)
    assert r.should_speak is False


def test_parse_and_validate_strips_prose_prefix():
    r = parse_and_validate('Sure, here: {"should_speak": true, "committed_reply": "ok"}', ReactiveReply)
    assert r.committed_reply == "ok"


def test_parse_and_validate_raises_on_empty():
    import pytest
    with pytest.raises(ValueError):
        parse_and_validate("", ReactiveReply)


def test_parse_and_validate_raises_on_garbage():
    import pytest
    with pytest.raises(ValueError):
        parse_and_validate("not json at all", ReactiveReply)


def test_dict_schema_returns_dict_unvalidated():
    d = {"type": "object", "properties": {"x": {"type": "string"}}}
    r = parse_and_validate('{"x": "hello"}', d)
    assert r == {"x": "hello"}
