"""Tests for HubSpotClient._request retry policy.

2026-08-25 (ChatGPT audit P1): transient failures (429, 5xx, network)
must retry with exponential backoff; validation failures (400, 401,
403) must NOT retry.  Honors Retry-After when HubSpot sends it.
"""
from __future__ import annotations

import pytest

from packages.integrations.hubspot_client import HubSpotClient, HubSpotError


# ── backoff computation ──────────────────────────────────────────


def test_backoff_uses_retry_after_seconds_when_present():
    """Retry-After: "30" → sleep 30s exactly."""
    assert HubSpotClient._next_backoff(1, "30") == 30.0
    assert HubSpotClient._next_backoff(3, "5") == 5.0


def test_backoff_ignores_invalid_retry_after():
    """Retry-After: non-numeric → fall back to exponential."""
    v = HubSpotClient._next_backoff(1, "Mon, 25 Aug 2026 12:00:00 GMT")
    # Falls through to exponential — attempt 1 base ~0.25s * jitter.
    assert 0.1 < v < 1.0


def test_backoff_rejects_absurdly_large_retry_after():
    """A malicious upstream could send Retry-After: 999999.  Cap it."""
    v = HubSpotClient._next_backoff(1, "999999")
    assert v <= HubSpotClient._BACKOFF_MAX_S * 4  # rejected → falls to exp
    # Attempt 1 exp is ~0.25s * jitter.
    assert v < 1.0


def test_backoff_exponential_growth():
    """Each attempt roughly doubles the base delay."""
    a1 = HubSpotClient._next_backoff(1, None)
    a2 = HubSpotClient._next_backoff(2, None)
    a3 = HubSpotClient._next_backoff(3, None)
    # Approximately doubling (jitter creates variance).
    assert a2 > a1
    assert a3 > a2


def test_backoff_caps_at_max():
    """Very high attempt numbers shouldn't produce 10-minute sleeps."""
    v = HubSpotClient._next_backoff(20, None)
    assert v <= HubSpotClient._BACKOFF_MAX_S


# ── request retry behavior (mocked httpx) ─────────────────────────


class _StubResponse:
    def __init__(self, status_code, json_body=None, headers=None, text_body=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers or {}
        self.text = text_body
        self.content = b'{"stub":true}' if json_body is not None else b""

    def json(self):
        return self._json


class _StubClient:
    """Async context-manager stub that returns predetermined responses
    for each `.request()` call.  Tracks call count for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def request(self, method, url, **kwargs):
        self.call_count += 1
        if not self.responses:
            raise AssertionError("stub exhausted")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _patch_httpx(monkeypatch, responses):
    """Replace httpx.AsyncClient with the stub for one test."""
    stub = _StubClient(responses)

    class _Factory:
        def __init__(self, *_a, **_kw):
            pass
        async def __aenter__(self_):
            return stub
        async def __aexit__(self_, *a):
            return None

    monkeypatch.setattr(
        "packages.integrations.hubspot_client.httpx.AsyncClient", _Factory,
    )
    # Sleep monkeypatch: the client does `import asyncio` inside
    # _request.  We save the original before patching so our fast
    # replacement can still yield to the event loop (without recursing
    # into itself).  monkeypatch.setattr restores automatically after
    # each test — no bleed-through to co-running tests.
    import asyncio as _asyncio_mod
    _original_sleep = _asyncio_mod.sleep
    async def _fast_sleep(_s):
        # Yield to event loop but return immediately, using the
        # unpatched original to avoid recursing into our own patch.
        await _original_sleep(0)
    monkeypatch.setattr(_asyncio_mod, "sleep", _fast_sleep)
    return stub


@pytest.mark.asyncio
async def test_request_succeeds_first_try(monkeypatch):
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(200, json_body={"ok": True}),
    ])
    c = HubSpotClient(access_token="pat-x")
    result = await c._request("GET", "/crm/v3/objects/contacts/1")
    assert result == {"ok": True}
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_request_retries_on_429(monkeypatch):
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(429, headers={"Retry-After": "0"},
                       text_body="rate limited"),
        _StubResponse(429, headers={"Retry-After": "0"},
                       text_body="rate limited"),
        _StubResponse(200, json_body={"ok": True}),
    ])
    c = HubSpotClient(access_token="pat-x")
    result = await c._request("POST", "/crm/v3/objects/contacts",
                                json={"properties": {}})
    assert result == {"ok": True}
    assert stub.call_count == 3


@pytest.mark.asyncio
async def test_request_retries_on_503(monkeypatch):
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(503, text_body="Service unavailable"),
        _StubResponse(200, json_body={"contact": "ok"}),
    ])
    c = HubSpotClient(access_token="pat-x")
    result = await c._request("PATCH", "/crm/v3/objects/contacts/42",
                                json={"properties": {}})
    assert result == {"contact": "ok"}
    assert stub.call_count == 2


@pytest.mark.asyncio
async def test_request_does_not_retry_on_400(monkeypatch):
    """Validation failures fail fast — retrying is quota waste."""
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(400, text_body="Invalid email format"),
    ])
    c = HubSpotClient(access_token="pat-x")
    with pytest.raises(HubSpotError, match="400"):
        await c._request("POST", "/crm/v3/objects/contacts",
                          json={"properties": {}})
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_request_does_not_retry_on_401(monkeypatch):
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(401, text_body="Invalid token"),
    ])
    c = HubSpotClient(access_token="pat-x")
    with pytest.raises(HubSpotError, match="401"):
        await c._request("GET", "/crm/v3/objects/contacts/1")
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_request_exhausts_max_attempts(monkeypatch):
    """Persistent 500 → give up after MAX_ATTEMPTS."""
    responses = [_StubResponse(500, text_body="upstream oops")
                 for _ in range(HubSpotClient._MAX_ATTEMPTS)]
    stub = _patch_httpx(monkeypatch, responses)
    c = HubSpotClient(access_token="pat-x")
    with pytest.raises(HubSpotError, match="retryable"):
        await c._request("POST", "/crm/v3/objects/deals",
                          json={"properties": {}})
    assert stub.call_count == HubSpotClient._MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_request_retries_on_network_error(monkeypatch):
    """Network-layer failures are retryable."""
    import httpx
    stub = _patch_httpx(monkeypatch, [
        httpx.ConnectError("connection refused"),
        _StubResponse(200, json_body={"ok": True}),
    ])
    c = HubSpotClient(access_token="pat-x")
    result = await c._request("GET", "/crm/v3/objects/contacts/1")
    assert result == {"ok": True}
    assert stub.call_count == 2


@pytest.mark.asyncio
async def test_request_gives_up_on_persistent_network_error(monkeypatch):
    import httpx
    stub = _patch_httpx(monkeypatch, [
        httpx.ConnectError("no route")
        for _ in range(HubSpotClient._MAX_ATTEMPTS)
    ])
    c = HubSpotClient(access_token="pat-x")
    with pytest.raises(HubSpotError, match="network error"):
        await c._request("GET", "/crm/v3/objects/contacts/1")
    assert stub.call_count == HubSpotClient._MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_request_404_returns_not_found_signal(monkeypatch):
    """404 is not retryable — it's a lookup-miss signal to the sink."""
    stub = _patch_httpx(monkeypatch, [
        _StubResponse(404, text_body="Not found"),
    ])
    c = HubSpotClient(access_token="pat-x")
    result = await c._request("GET", "/crm/v3/objects/contacts/missing")
    assert result == {"_not_found": True}
    assert stub.call_count == 1
