"""Sprint 10 obs tests: durable call event log + error classifier.

Coverage:
  * Write + read: events persist and come back in timeline
  * Per-call row cap enforced (safety against runaway loops)
  * classify_error maps common patterns
  * recent_errors filters by tenant + time window
  * error_category_counts rolls up correctly
  * write_error convenience helper both classifies AND persists
  * Best-effort: DB failures don't propagate
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from packages.observability.call_event_log import (
    CallEvent,
    CallEventLog,
    ErrorCategory,
    EventSourceKind,
    classify_error,
)


@pytest.fixture
def log(tmp_path: Path) -> CallEventLog:
    return CallEventLog(db_path=str(tmp_path / "events.db"))


# ── write + timeline ────────────────────────────────────────────────

def test_write_then_read_returns_event(log):
    log.write(CallEvent(
        call_id="CA-x", tenant_id="acme",
        source=EventSourceKind.STATE, kind="task_added",
        payload={"task_id": "book_1", "kind": "book"},
        turn_generation=1,
    ))
    timeline = log.timeline("CA-x")
    assert len(timeline) == 1
    assert timeline[0]["source"] == "state"
    assert timeline[0]["kind"] == "task_added"
    assert timeline[0]["payload"]["task_id"] == "book_1"
    assert timeline[0]["turn_generation"] == 1


def test_timeline_returns_newest_first(log):
    for i in range(3):
        log.write(CallEvent(
            call_id="CA-y", tenant_id="acme",
            source=EventSourceKind.STATE, kind=f"event_{i}",
            payload={"i": i},
        ))
    timeline = log.timeline("CA-y")
    kinds = [e["kind"] for e in timeline]
    assert kinds == ["event_2", "event_1", "event_0"]


def test_timeline_filters_by_call(log):
    log.write(CallEvent(call_id="CA-a", tenant_id="acme",
                        source=EventSourceKind.STATE, kind="k1", payload={}))
    log.write(CallEvent(call_id="CA-b", tenant_id="acme",
                        source=EventSourceKind.STATE, kind="k2", payload={}))
    assert len(log.timeline("CA-a")) == 1
    assert len(log.timeline("CA-b")) == 1
    assert len(log.timeline("CA-c")) == 0


def test_row_cap_prevents_runaway_writes(tmp_path):
    log = CallEventLog(db_path=str(tmp_path / "cap.db"), max_rows_per_call=5)
    for i in range(20):
        log.write(CallEvent(
            call_id="CA-cap", tenant_id="acme",
            source=EventSourceKind.STATE, kind=f"k{i}", payload={},
        ))
    timeline = log.timeline("CA-cap", limit=1000)
    assert len(timeline) == 5, "row cap must throttle writes per call"


# ── classify_error ─────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("PatchRejected: unknown_task book_1", ErrorCategory.STATE_REDUCTION),
    ("invalid_transition COMPLETED -> COMMITTING", ErrorCategory.STATE_REDUCTION),
    ("evidence_invalidated for slot start_iso", ErrorCategory.STATE_REDUCTION),
    ("no handler matched foo_tool", ErrorCategory.TOOL_SELECTION),
    ("unknown tool: nonsense", ErrorCategory.TOOL_SELECTION),
    ("bad_start_iso: '2026-xx-xx'", ErrorCategory.ARG_NORMALIZATION),
    ("missing_evidence: caller_name,phone", ErrorCategory.ARG_NORMALIZATION),
    ("argument_status_insufficient caller_name", ErrorCategory.ARG_NORMALIZATION),
    ("no_confident_match on insurance question", ErrorCategory.RETRIEVAL),
    ("search_error connecting to sqlite", ErrorCategory.RETRIEVAL),
    ("temporal unparseable 'blah'", ErrorCategory.TEMPORAL),
    ("deepgram 500 during transcribe", ErrorCategory.ASR),
    ("cartesia synth failed", ErrorCategory.DELIVERY),
    ("Cartesia timeout after 30s", ErrorCategory.PROVIDER_OUTAGE),
    ("BLOCKED_BY_GUARD unverified name", ErrorCategory.POLICY),
    ("some totally random exception", ErrorCategory.UNKNOWN),
])
def test_classify_error_patterns(message, expected):
    assert classify_error(message) == expected


def test_classify_error_uses_exc_type_hint():
    """Exception type name factored in for edge cases."""
    assert classify_error("misc", "PatchRejected") == ErrorCategory.STATE_REDUCTION


# ── write_error helper ─────────────────────────────────────────────

def test_write_error_classifies_and_persists(log):
    category = log.write_error(
        call_id="CA-e", tenant_id="acme",
        message="deepgram 502 gateway error",
        exc_type="ConnectionError",
    )
    assert category == ErrorCategory.PROVIDER_OUTAGE   # "502" wins over "deepgram"
    timeline = log.timeline("CA-e")
    assert len(timeline) == 1
    assert timeline[0]["source"] == "error"
    assert timeline[0]["error_category"] == "provider_outage"


# ── recent_errors filters ──────────────────────────────────────────

def test_recent_errors_filters_by_tenant(log):
    log.write_error("CA-1", "tenant-a", "deepgram outage")
    log.write_error("CA-2", "tenant-b", "cartesia synth failed")
    a_errors = log.recent_errors(tenant_id="tenant-a")
    assert len(a_errors) == 1
    assert a_errors[0]["tenant_id"] == "tenant-a"


def test_recent_errors_no_tenant_returns_all(log):
    log.write_error("CA-1", "tenant-a", "err1")
    log.write_error("CA-2", "tenant-b", "err2")
    all_errors = log.recent_errors()
    assert len(all_errors) == 2


def test_error_category_counts_rollup(log):
    log.write_error("CA-1", "acme", "deepgram outage")   # ASR
    log.write_error("CA-2", "acme", "cartesia synth failed")   # DELIVERY
    log.write_error("CA-3", "acme", "deepgram outage")   # ASR again
    counts = log.error_category_counts(tenant_id="acme")
    assert counts.get("asr") == 2
    assert counts.get("delivery") == 1


# ── best-effort writes don't raise ─────────────────────────────────

def test_write_on_bad_db_path_does_not_raise(tmp_path):
    """If the DB path is unwritable, write() must swallow the error —
    logging must never break a live call."""
    # Point the log at a path we can't write to (a directory)
    bad = tmp_path / "notafile"
    bad.mkdir()
    log = CallEventLog(db_path=str(bad / "cannot_open.db"))
    # This should be a straight-up no-op, no exception.
    log.write(CallEvent(
        call_id="CA-broken", tenant_id="acme",
        source=EventSourceKind.STATE, kind="test", payload={},
    ))
