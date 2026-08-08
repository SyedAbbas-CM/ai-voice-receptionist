from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "plivo_public_url", "https://voice.example.com")
    monkeypatch.setenv("PLIVO_SIGNATURE_ENFORCE", "false")
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    from app.main import create_app
    return TestClient(create_app())


def test_plivo_voice_returns_plivoxml_with_ws_url(client):
    resp = client.post("/plivo/voice", data={})
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"].lower()
    body = resp.text
    assert "<Response>" in body
    assert "<Stream" in body
    assert 'bidirectional="true"' in body
    assert 'contentType="audio/x-mulaw"' in body
    assert 'sampleRate="8000"' in body
    assert "wss://voice.example.com/plivo/stream" in body


def test_plivo_voice_unconfigured_says_error(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "plivo_public_url", None)
    monkeypatch.setenv("PLIVO_SIGNATURE_ENFORCE", "false")
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    from app.main import create_app
    c = TestClient(create_app())
    resp = c.post("/plivo/voice", data={})
    assert resp.status_code == 200
    assert "PLIVO_PUBLIC_URL" in resp.text
