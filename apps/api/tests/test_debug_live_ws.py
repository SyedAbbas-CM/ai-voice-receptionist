"""Integration test for WS /debug/live.

Verifies: client connects with ?call_id=, receives backfill of any
existing events for that call, then live-tails new writes.  Non-matching
call_ids don't leak events across."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from packages.observability.call_event_log import (
    CallEvent,
    EventSourceKind,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("API_AUTH_ENFORCE", "false")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def test_debug_live_backfills_and_streams():
    from app.main import create_app
    from packages.observability.call_event_log import get_call_event_log

    log = get_call_event_log()
    # Pre-existing event before the socket opens — must be backfilled.
    log.write(CallEvent(
        call_id="test-call", tenant_id="t1",
        source=EventSourceKind.CONTROL, kind="pre-existing",
        payload={"text": "before"},
    ))

    app = create_app()
    client = TestClient(app)
    with client.websocket_connect("/debug/live?call_id=test-call") as ws:
        first = ws.receive_json()
        assert first["kind"] == "pre-existing"
        assert first["payload"] == {"text": "before"}

        # New event after the socket is open — must arrive via fan-out.
        log.write(CallEvent(
            call_id="test-call", tenant_id="t1",
            source=EventSourceKind.STT, kind="partial",
            payload={"text": "hi"},
        ))
        second = ws.receive_json()
        assert second["kind"] == "partial"
        assert second["source"] == "stt"


def test_debug_live_filters_by_call_id():
    from app.main import create_app
    from packages.observability.call_event_log import get_call_event_log

    log = get_call_event_log()
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/debug/live?call_id=call-a") as ws:
        # Write to a DIFFERENT call
        log.write(CallEvent(
            call_id="call-b", tenant_id="t1",
            source=EventSourceKind.CONTROL, kind="not-for-us",
            payload={},
        ))
        # Then one for our call
        log.write(CallEvent(
            call_id="call-a", tenant_id="t1",
            source=EventSourceKind.CONTROL, kind="ours",
            payload={},
        ))
        got = ws.receive_json()
        assert got["kind"] == "ours"
        assert got["call_id"] == "call-a"
