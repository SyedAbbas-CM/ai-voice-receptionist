from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "telnyx_public_url", "https://voice.example.com")
    monkeypatch.setenv("TELNYX_SIGNATURE_ENFORCE", "false")
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    from app.main import create_app
    return TestClient(create_app())


def test_telnyx_voice_returns_texml_with_ws_url(client):
    resp = client.post("/telnyx/voice", data={})
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"].lower()
    body = resp.text
    assert "<Response>" in body
    assert "<Connect>" in body
    assert "<Stream" in body
    assert 'url="wss://voice.example.com/telnyx/stream"' in body


def test_telnyx_voice_unconfigured_says_error(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "telnyx_public_url", None)
    monkeypatch.setenv("TELNYX_SIGNATURE_ENFORCE", "false")
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    from app.main import create_app
    c = TestClient(create_app())
    resp = c.post("/telnyx/voice", data={})
    assert resp.status_code == 200
    assert "TELNYX_PUBLIC_URL" in resp.text
