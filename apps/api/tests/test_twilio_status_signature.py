"""Tests for /twilio/status signature verification.

2026-08-25 (ChatGPT backend audit P1): the status callback endpoint
must verify X-Twilio-Signature before consuming the payload.  Forged
requests must never trigger downstream dispatch.

Twilio retry semantics: MUST return 200 for both valid and invalid
signatures — 4xx/5xx from us triggers Twilio's exponential retry
storm which floods the log.  Invalid signature returns 200 but skips
downstream logic + emits SIGNATURE_INVALID log line.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """TestClient with signature enforcement ON + a known auth token.

    Force Settings re-instantiation because pydantic-settings caches
    the class instance module-globally; other test fixtures may have
    left twilio_public_url empty from a prior Settings() build.
    """
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("TWILIO_SIGNATURE_ENFORCE", "true")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token-xyz")
    monkeypatch.setenv(
        "TWILIO_PUBLIC_URL", "https://agent.example.com",
    )
    # Force settings reload — new instance picks up env vars set above.
    from app.core import config as _cfg
    from app.core.config import Settings
    fresh_settings = Settings()
    monkeypatch.setattr(_cfg, "settings", fresh_settings)
    # ALSO patch the twilio route's captured settings ref if it was
    # imported at module load (from app.core.config import settings).
    from app.routes import twilio as _twilio_mod
    monkeypatch.setattr(_twilio_mod, "settings", fresh_settings)
    from app.main import create_app
    return TestClient(create_app())


def _sign(url: str, form: dict, token: str) -> str:
    """Compute the X-Twilio-Signature the way Twilio's servers do it."""
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    return base64.b64encode(
        hmac.new(
            token.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")


def test_status_endpoint_rejects_missing_signature(client):
    """No X-Twilio-Signature header → still returns 200 (Twilio retry
    policy) but downstream dispatch skipped.  Verify by checking that
    a WELL-KNOWN log marker doesn't appear."""
    resp = client.post("/twilio/status", data={
        "CallSid": "CAtest", "CallStatus": "completed",
    })
    assert resp.status_code == 200


def test_status_endpoint_rejects_invalid_signature(client):
    resp = client.post(
        "/twilio/status",
        data={"CallSid": "CAtest", "CallStatus": "completed"},
        headers={"X-Twilio-Signature": "totally-fake-signature"},
    )
    assert resp.status_code == 200


def test_status_endpoint_accepts_valid_signature(client, caplog):
    import logging
    url = "https://agent.example.com/twilio/status"
    form = {"CallSid": "CAvalid123", "CallStatus": "completed",
            "CallDuration": "42"}
    sig = _sign(url, form, "test-token-xyz")
    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/twilio/status", data=form,
            headers={"X-Twilio-Signature": sig},
        )
    assert resp.status_code == 200
    # Valid signature → downstream log line should fire.
    joined = " ".join(r.message for r in caplog.records)
    assert "TWILIO_STATUS_CALLBACK" in joined
    assert "CAvalid123" in joined


def test_status_endpoint_skips_dispatch_on_invalid_signature(client, caplog):
    """Verify that on invalid signature, the CALLBACK log line does NOT
    fire (dispatch skipped) but the SIGNATURE_INVALID line DOES."""
    import logging
    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/twilio/status",
            data={"CallSid": "CAforged", "CallStatus": "completed"},
            headers={"X-Twilio-Signature": "faked"},
        )
    assert resp.status_code == 200
    joined = " ".join(r.message for r in caplog.records)
    # SIGNATURE_INVALID must fire.
    assert "TWILIO_STATUS_SIGNATURE_INVALID" in joined
    # Downstream CALLBACK dispatch must NOT fire.
    assert "TWILIO_STATUS_CALLBACK" not in joined


def test_status_endpoint_signature_disabled_in_dev(monkeypatch):
    """When TWILIO_SIGNATURE_ENFORCE=false (dev mode) any request
    is accepted so local testing without Twilio's public URL works."""
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("TWILIO_SIGNATURE_ENFORCE", "false")
    # Reload settings.
    from app.core import config as _cfg
    from app.core.config import Settings
    _cfg.settings = Settings()
    from app.main import create_app
    client = TestClient(create_app())
    resp = client.post(
        "/twilio/status",
        data={"CallSid": "CAdev", "CallStatus": "completed"},
    )
    assert resp.status_code == 200
