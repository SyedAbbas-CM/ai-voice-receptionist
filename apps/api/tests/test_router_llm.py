"""RouterLLM tests — verify provider iteration + cool-down behavior.

No network calls. Uses fake LLMProvider instances so we can inject
timeouts, errors, and successes deterministically.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self, name: str, behavior: str, delay: float = 0.0):
        self.name = name
        self.model = f"fake-{name}"
        self.behavior = behavior  # "ok" | "error" | "timeout" | "empty"
        self.delay = delay
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.behavior == "ok":
            return LLMResponse(text=f"reply from {self.name}", tool_calls=[])
        if self.behavior == "empty":
            return LLMResponse(text="", tool_calls=[])
        if self.behavior == "error":
            raise RuntimeError(f"{self.name} exploded")
        raise ValueError(f"unknown behavior: {self.behavior}")


def _build_router_with(providers, cooldown_s=1.0, timeout_s=0.5):
    """Bypass env-parsing + factory — construct the router with hand-picked fakes."""
    from app.providers.llm.router_llm import RouterLLM
    import app.providers.llm.router_llm as _rl
    with patch.object(RouterLLM, "__init__", lambda self: None):
        r = RouterLLM()
    r.providers = [(p.name, p) for p in providers]
    r.cooldown_s = cooldown_s
    r.timeout_s = timeout_s
    r._cool_until = {}
    r.model = f"router({providers[0].name})"
    # 2026-08-08: stub _PROVIDER_ALTERNATES so router doesn't try to build
    # real alt-model providers when the fake gives empty/error.  Without
    # this the router hits real Mistral/Groq (now that .env auto-loads).
    for name, _ in r.providers:
        _rl._PROVIDER_ALTERNATES.pop(name, None)
    return r


@pytest.mark.asyncio
async def test_first_provider_wins_when_healthy():
    p1 = _FakeProvider("groq", "ok")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2])
    resp = await r.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "reply from groq"
    assert p1.calls == 1
    assert p2.calls == 0


@pytest.mark.asyncio
async def test_falls_over_to_next_on_error():
    p1 = _FakeProvider("groq", "error")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2])
    resp = await r.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "reply from gemini"
    assert p1.calls == 1
    assert p2.calls == 1


@pytest.mark.asyncio
async def test_falls_over_on_timeout():
    p1 = _FakeProvider("groq", "ok", delay=2.0)   # will time out at 0.5s
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2], timeout_s=0.5)
    resp = await r.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "reply from gemini"


@pytest.mark.asyncio
async def test_falls_over_on_empty_response():
    p1 = _FakeProvider("groq", "empty")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2])
    resp = await r.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "reply from gemini"


@pytest.mark.asyncio
async def test_cooldown_skips_failed_provider_on_next_call():
    p1 = _FakeProvider("groq", "error")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2], cooldown_s=60)
    await r.complete([{"role": "user", "content": "hi"}])
    # Second call: p1 should be in cool-down, skipped immediately.
    p1.behavior = "ok"   # would work now, but router doesn't know
    await r.complete([{"role": "user", "content": "hi"}])
    assert p1.calls == 1, "p1 should have been skipped on the 2nd call"
    assert p2.calls == 2


@pytest.mark.asyncio
async def test_cooldown_expires_and_provider_retries():
    p1 = _FakeProvider("groq", "error")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2], cooldown_s=0.1)
    await r.complete([{"role": "user", "content": "hi"}])
    await asyncio.sleep(0.15)
    p1.behavior = "ok"
    resp = await r.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "reply from groq"
    assert p1.calls == 2


@pytest.mark.asyncio
async def test_all_providers_fail_raises():
    p1 = _FakeProvider("groq", "error")
    p2 = _FakeProvider("gemini", "error")
    r = _build_router_with([p1, p2])
    with pytest.raises(RuntimeError, match="all providers failed"):
        await r.complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_success_clears_cooldown():
    p1 = _FakeProvider("groq", "error")
    p2 = _FakeProvider("gemini", "ok")
    r = _build_router_with([p1, p2], cooldown_s=60)
    await r.complete([{"role": "user", "content": "hi"}])
    assert "groq" in r._cool_until
    assert "gemini" not in r._cool_until
