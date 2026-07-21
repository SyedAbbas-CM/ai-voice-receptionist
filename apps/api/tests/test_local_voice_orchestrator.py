"""Tests for the local Vapi emulator.

Verifies the orchestrator:
- returns a queued DispatchResult with the same shape as VapiClient
- runs a scripted conversation end-to-end
- variable_values are injected into the resolved system prompt
- an end-of-call event gets posted to the events webhook so the same
  disposition handler + Sheets writeback path executes

The Qwen3-TTS provider is stubbed with a fake that just returns bytes —
we're testing orchestration, not synthesis quality."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from packages.integrations.local_voice_orchestrator import (
    LocalVoiceOrchestrator,
    _resolve_variables,
)
from packages.schemas import BusinessProfile


# ---------------- helpers ----------------

def _wholesaler_business() -> BusinessProfile:
    repo_root = Path(__file__).resolve().parents[3]
    data = json.loads((repo_root / "sample-data" / "subtodealz" / "business.json").read_text())
    return BusinessProfile(**data)


class ScriptedLLM:
    """Duck-types LLMProvider for the brain."""
    name = "scripted-orchestrator"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        from app.providers.base import LLMResponse
        from packages.schemas import ToolCall
        self.calls += 1
        # 4th brain call -> capture disposition (ends the loop)
        # 5th call -> extractor JSON
        # 6th call -> lead classifier
        # (There's a greeting turn, then the caller script has 4 lines -> 4 brain
        #  loops. Between LLM messages the extractor also runs once per turn.)
        if self.calls == 4:
            return LLMResponse(text="", tool_calls=[ToolCall(
                id="call_disp_1",
                name="capture_disposition",
                arguments={"disposition": "CALLBACK_REQUESTED", "notes": "Wants a callback"},
            )])
        return LLMResponse(text="Yeah okay, that makes sense. Umm, I hear you.")


class FakeTTS:
    name = "fake"

    async def synthesize(self, text, voice=None):
        return b"WAV" + text.encode()[:12], "audio/wav"


# ---------------- variable resolution ----------------

def test_resolve_variables_replaces_placeholders():
    tpl = "Hey {{ lead_name }}, calling about {{property_address}} at ${{ rent_amount }}."
    out = _resolve_variables(tpl, {
        "lead_name": "Bob",
        "property_address": "123 Elm",
        "rent_amount": "1500",
    })
    assert "Hey Bob" in out
    assert "123 Elm" in out
    assert "$1500" in out


def test_resolve_variables_tolerates_missing_vars():
    out = _resolve_variables("Hey {{ lead_name }}", {})
    # If nothing to substitute, the placeholder stays — caller can decide
    assert "{{ lead_name }}" in out


# ---------------- orchestrator ----------------

@pytest.mark.asyncio
async def test_dispatch_call_returns_queued_immediately(monkeypatch, tmp_path):
    """dispatch_call must return within milliseconds (the actual call runs in
    a background task), and the result shape must match VapiClient."""
    business = _wholesaler_business()
    scripted = ScriptedLLM()
    monkeypatch.setattr("packages.integrations.local_voice_orchestrator.get_llm", lambda: scripted)
    monkeypatch.setattr("packages.integrations.local_voice_orchestrator.get_tts", lambda: FakeTTS())

    posts: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None, headers=None):
            posts.append({"url": url, "json": json})
            return MagicMock(status_code=200, text="")

    monkeypatch.setattr(
        "packages.integrations.local_voice_orchestrator.httpx.AsyncClient",
        FakeAsyncClient,
    )

    orch = LocalVoiceOrchestrator(
        business=business,
        output_dir=tmp_path,
        events_webhook_url="http://testserver/vapi/events",
    )

    result = await orch.dispatch_call(
        assistant_id="asst_local",
        phone_number_id="pn_local",
        customer_number="+15551234567",
        variable_values={"lead_name": "Bob", "property_address": "123 Elm", "rent_amount": "1500"},
    )

    assert result.id.startswith("local_")
    assert result.status == "queued"

    # Give the background task time to run and post to the webhook
    for _ in range(50):
        if posts:
            break
        await asyncio.sleep(0.05)

    assert posts, "orchestrator never posted an end-of-call event"
    msg = posts[0]["json"]["message"]
    assert msg["type"] == "end-of-call-report"
    assert msg["call"]["id"] == result.id
    assert msg["call"]["customer"]["number"] == "+15551234567"

    # transcript wrote to disk
    transcript = tmp_path / result.id / "transcript.jsonl"
    assert transcript.exists()
    lines = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    assert any(l.get("role") == "assistant" for l in lines)
    assert any(l.get("role") == "user" for l in lines)


@pytest.mark.asyncio
async def test_dispatch_call_signature_matches_vapi_client():
    """LocalVoiceOrchestrator and VapiClient must be interchangeable in the
    outbound router — same method name, same params, same result attrs."""
    import inspect

    from packages.integrations.local_voice_orchestrator import LocalVoiceOrchestrator
    from packages.integrations.vapi_client import VapiClient

    vapi_sig = inspect.signature(VapiClient.dispatch_call)
    local_sig = inspect.signature(LocalVoiceOrchestrator.dispatch_call)

    # Both accept the same 4 named args (assistant_id, phone_number_id,
    # customer_number, variable_values)
    vapi_params = set(vapi_sig.parameters) - {"self"}
    local_params = set(local_sig.parameters) - {"self"}
    required = {"assistant_id", "phone_number_id", "customer_number", "variable_values"}
    assert required.issubset(vapi_params)
    assert required.issubset(local_params)

    # Result has .id and .status
    from packages.integrations.local_voice_orchestrator import LocalDispatchResult
    from packages.integrations.vapi_client import DispatchResult
    for cls in (LocalDispatchResult, DispatchResult):
        assert hasattr(cls, "__annotations__")
        assert "id" in cls.__annotations__
        assert "status" in cls.__annotations__
