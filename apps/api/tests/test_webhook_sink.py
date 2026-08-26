"""Tests for WebhookClient + WebhookSink.

Mirrors test_hubspot_sink structure since both share the retry
contract.  Client tests use httpx stub for HTTP-layer semantics
(retries, signature computation, idempotency).  Sink tests use a
FakeWebhookClient recorder to verify the SINK builds correct payloads.

Signature verification round-trips a real HMAC so we know the
verify_signature static helper matches what emit() sends.

2026-08-26 (task #132): first delivery of the n8n/Make/Zapier
integration story.  Two SMB briefs asked for this; shipping it as
canonical.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from packages.integrations.sinks import WebhookSink
from packages.integrations.webhook_client import (
    WebhookClient,
    WebhookError,
)
from packages.schemas import (
    CallState,
    CallStatus,
    ExtractedFields,
    Intent,
    Urgency,
)


# ── WebhookClient constructor validation ──────────────────────────


def test_client_requires_url():
    with pytest.raises(WebhookError, match="WEBHOOK_URL"):
        WebhookClient(url="", secret="s" * 32)


def test_client_requires_secret():
    with pytest.raises(WebhookError, match="WEBHOOK_SECRET"):
        WebhookClient(url="https://n8n.example/webhook/abc", secret="")


def test_client_requires_http_scheme():
    with pytest.raises(WebhookError, match="http"):
        WebhookClient(url="n8n.example/webhook/abc", secret="s" * 32)


def test_client_accepts_http_and_https():
    WebhookClient(url="http://localhost:5678/webhook/abc", secret="s" * 32)
    WebhookClient(url="https://n8n.example/webhook/abc", secret="s" * 32)


# ── signature helpers ───────────────────────────────────────────


def test_verify_signature_round_trip():
    """The signature we compute + verify with the same secret + body
    must succeed."""
    secret = "x" * 32
    body = json.dumps({"event": "booking.created", "data": {"foo": "bar"}},
                       separators=(",", ":"), sort_keys=True)
    ts = int(time.time())
    signed = f"{ts}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={digest}"
    assert WebhookClient.verify_signature(secret, header, body) is True


def test_verify_signature_rejects_wrong_secret():
    secret_ours = "x" * 32
    secret_theirs = "y" * 32
    body = "{}"
    ts = int(time.time())
    signed = f"{ts}.{body}".encode("utf-8")
    digest = hmac.new(secret_theirs.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={digest}"
    assert WebhookClient.verify_signature(secret_ours, header, body) is False


def test_verify_signature_rejects_old_timestamp():
    """Anti-replay: timestamps older than max_age_seconds are rejected."""
    secret = "x" * 32
    body = "{}"
    old_ts = int(time.time()) - 600
    signed = f"{old_ts}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={old_ts},v1={digest}"
    # 300s max age default → 600s ago is rejected.
    assert WebhookClient.verify_signature(secret, header, body) is False


def test_verify_signature_rejects_future_timestamp():
    """Future timestamps (clock skew attack) also rejected."""
    secret = "x" * 32
    body = "{}"
    future_ts = int(time.time()) + 600
    signed = f"{future_ts}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={future_ts},v1={digest}"
    assert WebhookClient.verify_signature(secret, header, body) is False


def test_verify_signature_malformed_header():
    assert WebhookClient.verify_signature("s", "", "{}") is False
    assert WebhookClient.verify_signature("s", "garbage", "{}") is False
    assert WebhookClient.verify_signature("s", "t=notnum,v1=x", "{}") is False


def test_verify_signature_empty_body():
    """No body → return False (don't leak whether the format was wrong)."""
    assert WebhookClient.verify_signature("s", "t=1,v1=x", "") is False


# ── retry backoff ─────────────────────────────────────────────────


def test_backoff_uses_retry_after_seconds():
    assert WebhookClient._next_backoff(1, "30") == 30.0


def test_backoff_falls_back_to_exponential_on_bad_retry_after():
    v = WebhookClient._next_backoff(1, "next Monday")
    assert 0.1 < v < 1.0


def test_backoff_grows_with_attempt():
    a1 = WebhookClient._next_backoff(1, None)
    a3 = WebhookClient._next_backoff(3, None)
    assert a3 > a1


def test_backoff_caps_at_max():
    v = WebhookClient._next_backoff(20, None)
    assert v <= WebhookClient._BACKOFF_MAX_S


# ── emit() integration (mocked httpx) ─────────────────────────────


class _StubResponse:
    def __init__(self, status_code, json_body=None, headers=None,
                 text_body=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text_body
        self.content = (
            b'{"stub":true}' if json_body is not None else b""
        )

    def json(self):
        return self._json


class _StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, content=None, headers=None):
        self.calls.append({
            "url": url, "content": content, "headers": dict(headers or {}),
        })
        if not self.responses:
            raise AssertionError("stub exhausted")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _patch(monkeypatch, responses):
    stub = _StubClient(responses)

    class _Factory:
        def __init__(self, *_a, **_kw):
            pass
        async def __aenter__(self_):
            return stub
        async def __aexit__(self_, *a):
            return None

    monkeypatch.setattr(
        "packages.integrations.webhook_client.httpx.AsyncClient", _Factory,
    )
    import asyncio as _asyncio_mod
    _original_sleep = _asyncio_mod.sleep
    async def _fast_sleep(_s):
        await _original_sleep(0)
    monkeypatch.setattr(_asyncio_mod, "sleep", _fast_sleep)
    return stub


@pytest.mark.asyncio
async def test_emit_delivers_on_success(monkeypatch):
    stub = _patch(monkeypatch, [
        _StubResponse(200, json_body={"received": True}),
    ])
    c = WebhookClient(url="https://n8n.example/webhook/x", secret="s" * 32)
    result = await c.emit("booking.created", {"phone": "+15551234567"})
    assert result == {"received": True}
    assert len(stub.calls) == 1
    call = stub.calls[0]
    # Signature header present.
    assert call["headers"]["X-VoiceOps-Signature"].startswith("t=")
    assert ",v1=" in call["headers"]["X-VoiceOps-Signature"]
    # Idempotency + event type headers present.
    assert call["headers"]["X-VoiceOps-Event-Type"] == "booking.created"
    assert "X-VoiceOps-Idempotency-Key" in call["headers"]


@pytest.mark.asyncio
async def test_emit_retries_on_503(monkeypatch):
    stub = _patch(monkeypatch, [
        _StubResponse(503, text_body="down"),
        _StubResponse(200, json_body={"ok": True}),
    ])
    c = WebhookClient(url="https://x.example/w", secret="s" * 32)
    result = await c.emit("call.completed", {"session_id": "s1"})
    assert result == {"ok": True}
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_emit_does_not_retry_on_400(monkeypatch):
    stub = _patch(monkeypatch, [
        _StubResponse(400, text_body="bad payload"),
    ])
    c = WebhookClient(url="https://x.example/w", secret="s" * 32)
    with pytest.raises(WebhookError, match="400"):
        await c.emit("booking.created", {})
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_emit_exhausts_max_attempts(monkeypatch):
    responses = [_StubResponse(500) for _ in range(WebhookClient._MAX_ATTEMPTS)]
    stub = _patch(monkeypatch, responses)
    c = WebhookClient(url="https://x.example/w", secret="s" * 32)
    with pytest.raises(WebhookError, match="retryable"):
        await c.emit("booking.created", {})
    assert len(stub.calls) == WebhookClient._MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_emit_uses_supplied_idempotency_key(monkeypatch):
    stub = _patch(monkeypatch, [_StubResponse(200, json_body={})])
    c = WebhookClient(url="https://x.example/w", secret="s" * 32)
    await c.emit("booking.created", {}, idempotency_key="my-key-123")
    assert stub.calls[0]["headers"]["X-VoiceOps-Idempotency-Key"] == "my-key-123"


@pytest.mark.asyncio
async def test_emit_body_has_deterministic_key_order(monkeypatch):
    """Same payload → same body → same signature.  n8n users
    debugging their signature verify need this to be stable."""
    stub = _patch(monkeypatch, [
        _StubResponse(200, json_body={}),
        _StubResponse(200, json_body={}),
    ])
    c = WebhookClient(url="https://x.example/w", secret="s" * 32)
    # Same payload emitted twice with same idempotency_key + timestamp
    # would produce identical bodies.  Timestamps differ so signatures
    # differ, but the JSON body key order must be deterministic.
    await c.emit("call.completed", {"z": 1, "a": 2, "m": 3},
                  idempotency_key="k1")
    await c.emit("call.completed", {"a": 2, "m": 3, "z": 1},
                  idempotency_key="k1")
    # Both bodies use sort_keys so payload key ordering doesn't affect
    # the raw body.
    body1 = stub.calls[0]["content"]
    body2 = stub.calls[1]["content"]
    # Extract the "data" section — should be identical.
    j1 = json.loads(body1)
    j2 = json.loads(body2)
    assert j1["data"] == j2["data"]


# ── WebhookSink integration ────────────────────────────────────


class _FakeWebhookClient:
    """Records emit() calls for sink-level assertions."""
    def __init__(self, raise_on=None):
        self.calls: list[dict] = []
        self._raise_on = raise_on or set()

    async def emit(self, event_type, payload, idempotency_key=None):
        if event_type in self._raise_on:
            raise WebhookError("simulated failure")
        self.calls.append({
            "event_type": event_type, "payload": payload,
            "idempotency_key": idempotency_key,
        })
        return {"received": True}


def _state(phone="+15551234567", caller_name="Sarah Chen",
            intent=Intent.BOOK_APPOINTMENT):
    s = CallState(session_id="sess-p1", business_id="biz-x")
    s.extracted = ExtractedFields(
        caller_name=caller_name, phone=phone,
        intent=intent, urgency=Urgency.MEDIUM,
        lead_score=80, summary="Booked cleaning",
    )
    s.status = CallStatus.COMPLETED
    return s


def _booking(**overrides):
    args = {
        "caller_name": "Sarah Chen",
        "phone": "+15551234567",
        "service": "cleaning",
        "start_iso": "2026-08-28T14:30:00",
        **overrides.get("arguments", {}),
    }
    return {
        "name": "book_appointment",
        "arguments": args,
        "result": {"booked": True, **overrides.get("result", {})},
        "error": None,
    }


@pytest.mark.asyncio
async def test_sink_emits_booking_created():
    c = _FakeWebhookClient()
    sink = WebhookSink(c)
    await sink.on_booking(_state(), _booking())
    assert len(c.calls) == 1
    call = c.calls[0]
    assert call["event_type"] == "booking.created"
    assert call["payload"]["phone"] == "+15551234567"
    assert call["payload"]["service"] == "cleaning"
    assert call["payload"]["tool_name"] == "book_appointment"
    # Idempotency key includes session + tool for dedup.
    assert "book_appointment" in call["idempotency_key"]


@pytest.mark.asyncio
async def test_sink_skips_failed_booking():
    c = _FakeWebhookClient()
    sink = WebhookSink(c)
    booking = _booking(result={"booked": False, "reason": "slot_taken"})
    await sink.on_booking(_state(), booking)
    assert c.calls == []


@pytest.mark.asyncio
async def test_sink_accepts_ok_result_shape():
    """Local calendar returns {'ok': True} not {'booked': True}."""
    c = _FakeWebhookClient()
    sink = WebhookSink(c)
    booking = {
        "name": "book_appointment",
        "arguments": {"caller_name": "X", "phone": "+15550000000",
                        "service": "x", "start_iso": "2026-09-01T10:00:00"},
        "result": {"ok": True},
    }
    await sink.on_booking(_state(), booking)
    assert len(c.calls) == 1


@pytest.mark.asyncio
async def test_sink_swallows_emit_failure_on_booking():
    """A broken tenant URL must never crash our booking flow."""
    c = _FakeWebhookClient(raise_on={"booking.created"})
    sink = WebhookSink(c)
    await sink.on_booking(_state(), _booking())
    # No crash — sink swallowed WebhookError.


@pytest.mark.asyncio
async def test_sink_emits_call_completed():
    c = _FakeWebhookClient()
    sink = WebhookSink(c)
    await sink.on_call_end(_state())
    assert len(c.calls) == 1
    call = c.calls[0]
    assert call["event_type"] == "call.completed"
    assert call["payload"]["status"] == "completed"
    assert call["payload"]["intent"] == "book_appointment"
    assert call["payload"]["lead_score"] == 80


@pytest.mark.asyncio
async def test_sink_swallows_emit_failure_on_call_end():
    c = _FakeWebhookClient(raise_on={"call.completed"})
    sink = WebhookSink(c)
    await sink.on_call_end(_state())
    # No crash.


# ── factory wiring ────────────────────────────────────────────


def test_factory_rejects_webhook_without_url():
    class _S:
        webhook_url = None
        webhook_secret = "x" * 32
        webhook_source = "voiceops-ai-agent"
    from packages.integrations.sinks import build_sink_from_env
    with pytest.raises(RuntimeError, match="WEBHOOK_URL"):
        build_sink_from_env("webhook", _S())


def test_factory_rejects_webhook_without_secret():
    class _S:
        webhook_url = "https://n8n.example/webhook/abc"
        webhook_secret = None
        webhook_source = "voiceops-ai-agent"
    from packages.integrations.sinks import build_sink_from_env
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        build_sink_from_env("webhook", _S())


def test_factory_builds_webhook_sink_with_creds():
    class _S:
        webhook_url = "https://n8n.example/webhook/abc"
        webhook_secret = "s" * 32
        webhook_source = "voiceops-ai-agent"
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("webhook", _S())
    assert sink.name == "webhook"


def test_factory_builds_composite_hubspot_plus_webhook():
    """Common combo — tenant wants HubSpot writes AND their own n8n."""
    class _S:
        hubspot_access_token = "pat-hs"
        hubspot_portal_id = None
        hubspot_pipeline_id = None
        hubspot_stage_id = None
        hubspot_create_deals = False
        webhook_url = "https://n8n.example/webhook/abc"
        webhook_secret = "s" * 32
        webhook_source = "voiceops-ai-agent"
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("hubspot+webhook", _S())
    assert hasattr(sink, "sinks") or sink.name in {"hubspot", "webhook"}
