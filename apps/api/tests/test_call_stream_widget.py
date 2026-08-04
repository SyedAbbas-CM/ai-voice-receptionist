"""Plumbing test for the /call-stream widget mount.

The full audio + intelligence integration is covered by:
  - test_twilio_actor.py (audio protocol + actor lifecycle)
  - test_streaming_wiring.py (StreamingSTTBridge/TurnManager/reconciler)
  - test_debug_live_ws.py (fan-out + backfill contract)
  - test_call_event_log_subscribe.py (subscribe/unsubscribe)

This test focuses on what those don't cover: that the widget's static
files are actually served, unauthenticated, at the /call-stream mount."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    from packages.observability import call_event_log
    call_event_log.reset_singleton_for_tests()
    yield
    call_event_log.reset_singleton_for_tests()


def _client() -> TestClient:
    from app.main import create_app
    return TestClient(create_app())


def test_call_stream_index_served():
    resp = _client().get("/call-stream/")
    assert resp.status_code == 200
    body = resp.text
    # Must reference the local static assets — proves the mount serves
    # the folder we expect and not some other index.html.
    assert "session.js" in body
    assert "style.css" in body


def test_call_stream_static_assets_served():
    c = _client()
    for asset in ("style.css", "session.js", "audio-pipe.js", "mulaw-worklet.js"):
        r = c.get(f"/call-stream/{asset}")
        assert r.status_code == 200, f"asset {asset} not served"
        assert len(r.text) > 100, f"asset {asset} suspiciously small"


def test_call_stream_mount_unauthenticated():
    """Widget must NOT require an API key — matches /call, /simulator."""
    import os
    os.environ["API_AUTH_ENFORCE"] = "true"
    try:
        c = _client()
        # No Authorization header, no api key — should still work
        r = c.get("/call-stream/")
        assert r.status_code == 200
    finally:
        os.environ["API_AUTH_ENFORCE"] = "false"
