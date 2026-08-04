"""Sprint 10 streaming-parity extension: pub/sub fan-out on CallEventLog.

Writers keep working exactly as before. New subscribers get a live copy
of every write matching their call_id. Unsubscribed callbacks stop firing.
Errors in a subscriber never break the writer."""
from __future__ import annotations

from pathlib import Path

import pytest

from packages.observability.call_event_log import (
    CallEvent,
    CallEventLog,
    EventSourceKind,
)


@pytest.fixture
def log(tmp_path: Path) -> CallEventLog:
    return CallEventLog(db_path=str(tmp_path / "test.db"))


def _make_event(call_id: str, kind: str = "test") -> CallEvent:
    return CallEvent(
        call_id=call_id, tenant_id="t1",
        source=EventSourceKind.CONTROL, kind=kind,
        payload={"text": kind},
    )


def test_subscribe_receives_writes(log: CallEventLog) -> None:
    received: list[dict] = []
    log.subscribe("call-a", received.append)
    log.write(_make_event("call-a", "first"))
    log.write(_make_event("call-a", "second"))
    assert len(received) == 2
    assert received[0]["kind"] == "first"
    assert received[1]["kind"] == "second"
    assert received[0]["source"] == "control"
    assert received[0]["payload"] == {"text": "first"}


def test_subscribe_filters_by_call_id(log: CallEventLog) -> None:
    received_a: list[dict] = []
    received_b: list[dict] = []
    log.subscribe("call-a", received_a.append)
    log.subscribe("call-b", received_b.append)
    log.write(_make_event("call-a", "for-a"))
    log.write(_make_event("call-b", "for-b"))
    assert len(received_a) == 1 and received_a[0]["kind"] == "for-a"
    assert len(received_b) == 1 and received_b[0]["kind"] == "for-b"


def test_unsubscribe_stops_delivery(log: CallEventLog) -> None:
    received: list[dict] = []
    log.subscribe("call-a", received.append)
    log.write(_make_event("call-a", "one"))
    log.unsubscribe("call-a", received.append)
    log.write(_make_event("call-a", "two"))
    assert len(received) == 1
    assert received[0]["kind"] == "one"


def test_subscriber_exception_does_not_break_writer(log: CallEventLog) -> None:
    def bad_cb(_ev: dict) -> None:
        raise RuntimeError("subscriber blew up")
    log.subscribe("call-a", bad_cb)
    # write must not raise
    log.write(_make_event("call-a", "still-writes"))
    # and the row IS persisted despite the subscriber failure
    tl = log.timeline("call-a")
    assert len(tl) == 1
    assert tl[0]["kind"] == "still-writes"


def test_multiple_subscribers_same_call_id(log: CallEventLog) -> None:
    a: list[dict] = []
    b: list[dict] = []
    log.subscribe("call-a", a.append)
    log.subscribe("call-a", b.append)
    log.write(_make_event("call-a", "broadcast"))
    assert len(a) == 1 and len(b) == 1
