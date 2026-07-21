"""Verify Anthropic adapter attaches cache_control to system + tool defs
and sends the prompt-caching beta header. Uses a fake httpx client — no
real API call."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from packages.schemas import ToolDefinition


class _CapturingClient:
    """Records the last POST payload + headers so tests can assert on them."""

    captured: dict = {}

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, headers=None, json=None):
        _CapturingClient.captured = {"url": url, "headers": headers, "json": json}
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        return resp


@pytest.mark.asyncio
async def test_system_prompt_wrapped_in_cache_control_block(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-fake")
    monkeypatch.setattr(settings, "anthropic_prompt_caching", True)

    from app.providers.llm.anthropic_llm import AnthropicLLM
    monkeypatch.setattr("app.providers.llm.anthropic_llm.httpx.AsyncClient", _CapturingClient)

    llm = AnthropicLLM()
    await llm.complete(
        messages=[
            {"role": "system", "content": "You are a helpful receptionist."},
            {"role": "user", "content": "Hi."},
        ],
    )

    body = _CapturingClient.captured["json"]
    assert isinstance(body["system"], list), "system should be a structured list when caching is on"
    assert body["system"][0]["type"] == "text"
    assert body["system"][0]["text"] == "You are a helpful receptionist."
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_last_tool_gets_cache_control(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-fake")
    monkeypatch.setattr(settings, "anthropic_prompt_caching", True)

    from app.providers.llm.anthropic_llm import AnthropicLLM
    monkeypatch.setattr("app.providers.llm.anthropic_llm.httpx.AsyncClient", _CapturingClient)

    tools = [
        ToolDefinition(name="one", description="", parameters={"type": "object", "properties": {}}),
        ToolDefinition(name="two", description="", parameters={"type": "object", "properties": {}}),
    ]

    llm = AnthropicLLM()
    await llm.complete(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
    )

    body = _CapturingClient.captured["json"]
    assert len(body["tools"]) == 2
    assert "cache_control" not in body["tools"][0], "only the LAST tool should be marked"
    assert body["tools"][1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_caching_beta_header_sent(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-fake")
    monkeypatch.setattr(settings, "anthropic_prompt_caching", True)

    from app.providers.llm.anthropic_llm import AnthropicLLM
    monkeypatch.setattr("app.providers.llm.anthropic_llm.httpx.AsyncClient", _CapturingClient)

    llm = AnthropicLLM()
    await llm.complete(messages=[{"role": "user", "content": "hi"}])

    headers = _CapturingClient.captured["headers"]
    assert "anthropic-beta" in headers
    assert "prompt-caching" in headers["anthropic-beta"]


@pytest.mark.asyncio
async def test_caching_disabled_sends_plain_system(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-fake")
    monkeypatch.setattr(settings, "anthropic_prompt_caching", False)

    from app.providers.llm.anthropic_llm import AnthropicLLM
    monkeypatch.setattr("app.providers.llm.anthropic_llm.httpx.AsyncClient", _CapturingClient)

    llm = AnthropicLLM()
    await llm.complete(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "hi"},
        ],
    )

    body = _CapturingClient.captured["json"]
    assert body["system"] == "test", "with caching off, system should be plain string"
    headers = _CapturingClient.captured["headers"]
    assert "anthropic-beta" not in headers
