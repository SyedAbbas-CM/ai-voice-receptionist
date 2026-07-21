from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    return TestClient(app)


def test_voices_endpoint_returns_curated_list(client):
    r = client.get("/v1/voices")
    assert r.status_code == 200
    body = r.json()
    assert "voices" in body
    assert len(body["voices"]) >= 1
    v = body["voices"][0]
    assert "voice_id" in v and "name" in v


def test_tts_endpoint_speaks_11L_shape(client):
    class FakeTTS:
        name = "fake"
        async def synthesize(self, text, voice=None):
            assert text == "hello there"
            assert voice == "Vivian"
            return b"FAKE_MP3_BYTES", "audio/mpeg"

    with patch("app.routes.elevenlabs_compat.get_tts", return_value=FakeTTS()):
        r = client.post(
            "/v1/text-to-speech/Vivian",
            json={"text": "hello there", "model_id": "eleven_turbo_v2_5"},
        )
    assert r.status_code == 200
    assert r.content == b"FAKE_MP3_BYTES"
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.headers.get("x-tts-provider") == "fake"


def test_tts_endpoint_requires_text(client):
    r = client.post("/v1/text-to-speech/Vivian", json={"text": "   "})
    assert r.status_code == 400


def test_tts_endpoint_rejects_browser_sentinel(client):
    class BrowserTTS:
        name = "browser"
        async def synthesize(self, text, voice=None):
            return b"", "text/x-browser-speak"

    with patch("app.routes.elevenlabs_compat.get_tts", return_value=BrowserTTS()):
        r = client.post("/v1/text-to-speech/Vivian", json={"text": "hi"})
    assert r.status_code == 400


def test_tts_endpoint_auth_gate(client, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "compat_api_key", "sk-secret")

    r_no_key = client.post("/v1/text-to-speech/Vivian", json={"text": "hi"})
    assert r_no_key.status_code == 401

    class FakeTTS:
        name = "fake"
        async def synthesize(self, text, voice=None):
            return b"BYTES", "audio/mpeg"

    with patch("app.routes.elevenlabs_compat.get_tts", return_value=FakeTTS()):
        r_ok = client.post(
            "/v1/text-to-speech/Vivian",
            json={"text": "hi"},
            headers={"xi-api-key": "sk-secret"},
        )
    assert r_ok.status_code == 200
