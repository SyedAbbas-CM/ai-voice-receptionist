"""Sprint 11c: failure intelligence pipeline tests."""
from __future__ import annotations

import pytest

from packages.observability.call_event_log import (
    CallEvent,
    CallEventLog,
    ErrorCategory,
    EventSourceKind,
)
from packages.observability.failure_intelligence import (
    _signature_stem,
    cluster_recent_failures,
    failure_patterns_report,
    per_call_failure_summary,
)


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """Every test gets its own call_events.db so clusters are clean."""
    import packages.observability.call_event_log as cel
    cel._SINGLETON = None
    monkeypatch.setenv("CALL_EVENT_LOG_PATH", str(tmp_path / "events.db"))
    yield
    cel._SINGLETON = None


def _seed(call_id: str, tenant_id: str, category: ErrorCategory,
          message: str, turn: int = 0) -> None:
    from packages.observability.call_event_log import get_call_event_log
    log = get_call_event_log()
    log.write(CallEvent(
        call_id=call_id, tenant_id=tenant_id,
        source=EventSourceKind.ERROR, kind="test",
        payload={"message": message},
        error_category=category, turn_generation=turn,
    ))


# ── signature normalization ────────────────────────────────────────

def test_signature_stem_strips_call_ids():
    s1 = _signature_stem("evidence_invalidated for CA-abc123 turn_5")
    s2 = _signature_stem("evidence_invalidated for CA-xyz999 turn_9")
    assert s1 == s2


def test_signature_stem_strips_action_ids():
    s1 = _signature_stem("commit dedup hit action_id=act_abc123def456")
    s2 = _signature_stem("commit dedup hit action_id=act_999888777666")
    assert s1 == s2


def test_signature_stem_strips_cooldown_seconds():
    s1 = _signature_stem("groq cool_for_28s")
    s2 = _signature_stem("groq cool_for_15s")
    assert s1 == s2


def test_signature_stem_empty_message():
    assert _signature_stem("") == "(empty)"


# ── clustering ─────────────────────────────────────────────────────

def test_cluster_groups_same_category_same_signature():
    for i in range(5):
        _seed(f"CA-{i}", "acme", ErrorCategory.PROVIDER_OUTAGE,
              "cerebras timeout after 30s")
    clusters = cluster_recent_failures()
    assert len(clusters) == 1
    assert clusters[0].count == 5
    assert clusters[0].category == "provider_outage"


def test_cluster_separates_different_signatures():
    _seed("CA-a", "acme", ErrorCategory.PROVIDER_OUTAGE, "cerebras timeout")
    _seed("CA-b", "acme", ErrorCategory.PROVIDER_OUTAGE, "groq 429 rate limit")
    clusters = cluster_recent_failures()
    assert len(clusters) == 2


def test_cluster_ranked_by_count_desc():
    for _ in range(3):
        _seed("CA-x", "acme", ErrorCategory.PROVIDER_OUTAGE, "cerebras down")
    _seed("CA-y", "acme", ErrorCategory.RETRIEVAL, "no_confident_match")
    clusters = cluster_recent_failures()
    assert clusters[0].count == 3
    assert clusters[1].count == 1


def test_cluster_tracks_first_and_last_seen():
    _seed("CA-1", "acme", ErrorCategory.ASR, "deepgram error")
    _seed("CA-2", "acme", ErrorCategory.ASR, "deepgram error")
    clusters = cluster_recent_failures()
    c = clusters[0]
    assert c.first_seen_ts > 0
    assert c.last_seen_ts >= c.first_seen_ts


def test_cluster_dedupes_calls_in_sample():
    for _ in range(10):
        _seed("CA-repeat", "acme", ErrorCategory.DELIVERY, "cartesia synth")
    clusters = cluster_recent_failures()
    assert clusters[0].affected_call_ids == ["CA-repeat"]


def test_cluster_tenant_filter():
    _seed("CA-a", "tenant-a", ErrorCategory.ASR, "deepgram")
    _seed("CA-b", "tenant-b", ErrorCategory.ASR, "deepgram")
    a_only = cluster_recent_failures(tenant_id="tenant-a")
    assert len(a_only) == 1


def test_cluster_suggested_action_populated():
    _seed("CA-1", "acme", ErrorCategory.STATE_REDUCTION, "PatchRejected: bad")
    clusters = cluster_recent_failures()
    assert "reducer" in clusters[0].suggested_action.lower() or \
           "state" in clusters[0].suggested_action.lower()


def test_no_errors_returns_empty_list():
    assert cluster_recent_failures() == []


# ── report shape ───────────────────────────────────────────────────

def test_report_shape():
    _seed("CA-1", "acme", ErrorCategory.ASR, "deepgram outage")
    r = failure_patterns_report()
    assert r["window_hours"] == 24
    assert r["total_classified_errors"] == 1
    assert r["cluster_count"] == 1
    assert r["top_action"] is not None
    assert "clusters" in r


def test_report_empty_when_no_errors():
    r = failure_patterns_report()
    assert r["total_classified_errors"] == 0
    assert r["top_action"] is None


# ── per-call summary ───────────────────────────────────────────────

def test_per_call_summary_counts_categories():
    _seed("CA-1", "acme", ErrorCategory.ASR, "deepgram", turn=2)
    _seed("CA-1", "acme", ErrorCategory.ASR, "deepgram again", turn=3)
    _seed("CA-1", "acme", ErrorCategory.PROVIDER_OUTAGE, "cerebras", turn=5)
    s = per_call_failure_summary("CA-1")
    assert s["error_count"] == 3
    assert s["categories"]["asr"] == 2
    assert s["categories"]["provider_outage"] == 1
    assert s["first_bad_turn"] == 2


def test_per_call_summary_no_errors_returns_zero():
    s = per_call_failure_summary("CA-clean")
    assert s["error_count"] == 0
    assert s["categories"] == {}
