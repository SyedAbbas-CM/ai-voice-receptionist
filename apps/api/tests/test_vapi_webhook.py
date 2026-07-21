from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_vapi_chat_completions_shape_matches_openai(monkeypatch):
    """The Vapi custom-LLM webhook must return an OpenAI-compatible payload."""
    from app.routes.vapi import _openai_response

    resp = _openai_response("Hi, how can I help?", "voiceops-brain")

    assert resp["object"] == "chat.completion"
    assert resp["choices"][0]["message"]["role"] == "assistant"
    assert resp["choices"][0]["message"]["content"] == "Hi, how can I help?"
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["model"] == "voiceops-brain"
    assert resp["id"].startswith("chatcmpl-")
    assert abs(resp["created"] - int(time.time())) < 5


@pytest.mark.asyncio
async def test_vapi_session_id_from_call_id():
    from app.routes.vapi import VapiCompletionRequest, _session_id_from_request

    req = VapiCompletionRequest(messages=[], call={"id": "vapi-abc-123"})
    assert _session_id_from_request(req) == "vapi_vapi-abc-123"

    req2 = VapiCompletionRequest(messages=[], metadata={"session_id": "custom-id"})
    assert _session_id_from_request(req2) == "custom-id"

    req3 = VapiCompletionRequest(messages=[])
    sid = _session_id_from_request(req3)
    assert sid.startswith("vapi_")


@pytest.mark.asyncio
async def test_last_user_text_picks_most_recent():
    from app.routes.vapi import ChatMessage, _last_user_text

    msgs = [
        ChatMessage(role="system", content="you are a receptionist"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
        ChatMessage(role="user", content="book me tomorrow"),
    ]
    assert _last_user_text(msgs) == "book me tomorrow"
    assert _last_user_text([]) == ""
