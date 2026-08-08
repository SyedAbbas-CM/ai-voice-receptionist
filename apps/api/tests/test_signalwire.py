"""SignalWire adapter tests.

Verifies:
  * POST /signalwire/voice returns TwiML pointing at the correct wss URL.
  * Signature enforcement rejects requests with a bad X-SignalWire-Signature.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    yield


def _client() -> TestClient:
    from app.main import create_app
    return TestClient(create_app())


def test_signalwire_voice_returns_stream_twiml(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "twilio_public_url", "https://example.com")
    monkeypatch.setenv("SIGNALWIRE_SIGNATURE_ENFORCE", "false")

    resp = _client().post("/signalwire/voice")
    assert resp.status_code == 200
    body = resp.text
    assert "<Connect>" in body
    assert "<Stream" in body
    assert 'url="wss://example.com/signalwire/stream"' in body


def test_signalwire_voice_rejects_bad_signature(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "twilio_public_url", "https://example.com")
    monkeypatch.setattr(settings, "signalwire_token", "test-token")
    monkeypatch.setenv("SIGNALWIRE_SIGNATURE_ENFORCE", "true")

    resp = _client().post(
        "/signalwire/voice",
        headers={"X-SignalWire-Signature": "definitely-not-valid"},
    )
    assert resp.status_code == 401


def test_signalwire_voice_accepts_good_signature(monkeypatch):
    from app.core.config import settings
    token = "test-token"
    monkeypatch.setattr(settings, "twilio_public_url", "https://example.com")
    monkeypatch.setattr(settings, "signalwire_token", token)
    monkeypatch.setenv("SIGNALWIRE_SIGNATURE_ENFORCE", "true")

    url = "https://example.com/signalwire/voice"
    form = {"CallSid": "CA123", "From": "+15551234567"}
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form.keys()))
    sig = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()

    resp = _client().post(
        "/signalwire/voice",
        headers={"X-SignalWire-Signature": sig},
        data=form,
    )
    assert resp.status_code == 200
    assert "<Stream" in resp.text
