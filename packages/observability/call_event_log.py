"""Durable per-call event log — Sprint 10 observability layer.

Every notable event on a call is written to a SQLite table
(`call_events`) so we can replay a failed call after the fact.  This
is what "measure failures" needs — without it, `logs errors tracing`
are ephemeral and per-process.

Table shape:

    call_events(
        id INTEGER PK,
        call_id TEXT,
        tenant_id TEXT,
        turn_generation INTEGER,
        speech_generation INTEGER,
        monotonic_ns INTEGER,
        wall_ts REAL,
        source TEXT,           -- media/stt/llm/tool/tts/playback/state/commit/error
        kind TEXT,             -- source-specific event kind
        payload TEXT,          -- JSON-serialized event payload
        error_category TEXT,   -- one of ErrorCategory when source='error', else NULL
    )

Design principles:
  * Fire-and-forget: writer is best-effort, never blocks the actor.
  * Bounded rows-per-call cap so a runaway loop can't fill the disk.
  * Rotation: rows older than N days pruned on startup.
  * Same DB as the existing app tables (SQLite) — one file, easy dump.
  * Read path (`/debug/call/{id}`) queries by call_id, returns full timeline.

Not a substitute for OTel — OTel is realtime distributed tracing.
This is durable local storage for after-the-fact analysis.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class EventSourceKind(str, Enum):
    """What subsystem emitted this event.  Superset of runtime
    EventSource — includes non-actor sources (state kernel, commit
    coordinator, error)."""
    MEDIA = "media"
    STT = "stt"
    LLM = "llm"
    TOOL = "tool"
    TTS = "tts"
    PLAYBACK = "playback"
    STATE = "state"           # DialogueState transitions
    COMMIT = "commit"         # CommitCoordinator outcomes
    CONTROL = "control"       # call start/stop/escalate
    ERROR = "error"           # classified exceptions


class ErrorCategory(str, Enum):
    """Failure taxonomy from audit 2 (Track 13, Failure Intelligence).

    Errors caught anywhere in the call path get classified into one
    of these buckets so we can cluster + report failure patterns
    instead of drowning in generic 'Exception' traces."""
    STATE_REDUCTION = "state_reduction"
    """Reducer rejected a patch (invalid transition, missing task, etc)."""
    TOOL_SELECTION = "tool_selection"
    """LLM picked the wrong tool or an unknown tool."""
    ARG_NORMALIZATION = "arg_normalization"
    """Tool arguments couldn't be normalized (bad date, missing phone)."""
    RETRIEVAL = "retrieval"
    """RAG failure — no confident match, embedder crash, index missing."""
    TEMPORAL = "temporal"
    """Date/time couldn't be resolved or was ambiguous."""
    ASR = "asr"
    """Speech recognition failure — provider error, empty transcript, hallucination."""
    TURN_TAKING = "turn_taking"
    """Barge-in classifier confused, unexpected state transition."""
    DELIVERY = "delivery"
    """TTS synthesis failed, VPL validation rejected, audio pipeline error."""
    PROVIDER_OUTAGE = "provider_outage"
    """External service down (LLM 5xx, Cartesia timeout, Deepgram disconnect)."""
    POLICY = "policy"
    """Safety guard fired — write blocked, prompt injection, compliance refusal."""
    UNKNOWN = "unknown"
    """Uncaught exception — surface + reclassify manually."""


# Hint patterns → category.  Coarse; refined over time as we see real errors.
# ORDER MATTERS: provider-outage signals (5xx, timeout) checked FIRST because
# they're strictly stronger — if the provider is down, the surrounding
# provider-name context (deepgram/cartesia) is downstream noise, not the
# root cause.
_CATEGORY_HINTS: list[tuple[str, ErrorCategory]] = [
    # Provider outage first — trumps provider-name matches below
    ("503", ErrorCategory.PROVIDER_OUTAGE),
    ("502", ErrorCategory.PROVIDER_OUTAGE),
    ("504", ErrorCategory.PROVIDER_OUTAGE),
    ("gateway", ErrorCategory.PROVIDER_OUTAGE),
    ("timeout", ErrorCategory.PROVIDER_OUTAGE),
    ("connection", ErrorCategory.PROVIDER_OUTAGE),
    # State reducer failures
    ("invalid_transition", ErrorCategory.STATE_REDUCTION),
    ("unknown_task", ErrorCategory.STATE_REDUCTION),
    ("evidence_", ErrorCategory.STATE_REDUCTION),
    ("PatchRejected", ErrorCategory.STATE_REDUCTION),
    # Tool routing
    ("unknown tool", ErrorCategory.TOOL_SELECTION),
    ("no handler matched", ErrorCategory.TOOL_SELECTION),
    # Argument normalization
    ("bad_start_iso", ErrorCategory.ARG_NORMALIZATION),
    ("missing_evidence", ErrorCategory.ARG_NORMALIZATION),
    ("argument_status_insufficient", ErrorCategory.ARG_NORMALIZATION),
    # RAG
    ("no_confident_match", ErrorCategory.RETRIEVAL),
    ("no_match", ErrorCategory.RETRIEVAL),
    ("search_error", ErrorCategory.RETRIEVAL),
    ("embedder", ErrorCategory.RETRIEVAL),
    # Temporal
    ("resolver", ErrorCategory.TEMPORAL),
    ("unparseable", ErrorCategory.TEMPORAL),
    ("ambiguous", ErrorCategory.TEMPORAL),
    # ASR (specific error patterns before generic provider-name)
    ("transcribe", ErrorCategory.ASR),
    ("whisper", ErrorCategory.ASR),
    ("deepgram", ErrorCategory.ASR),
    # Turn-taking
    ("barge", ErrorCategory.TURN_TAKING),
    # Delivery
    ("synth", ErrorCategory.DELIVERY),
    ("cartesia", ErrorCategory.DELIVERY),
    ("elevenlabs", ErrorCategory.DELIVERY),
    ("VPL", ErrorCategory.DELIVERY),
    # Policy
    ("BLOCKED_BY_GUARD", ErrorCategory.POLICY),
    ("policy_violation", ErrorCategory.POLICY),
    ("emergency", ErrorCategory.POLICY),
]


def classify_error(message: str, exc_type_name: str = "") -> ErrorCategory:
    """Coarse pattern-based classifier.  Returns UNKNOWN when no
    pattern matches — those surface up so we can add rules."""
    text = f"{exc_type_name}: {message}".lower()
    for pattern, category in _CATEGORY_HINTS:
        if pattern.lower() in text:
            return category
    return ErrorCategory.UNKNOWN


@dataclass(frozen=True)
class CallEvent:
    """One row in the call_events table."""
    call_id: str
    tenant_id: str
    source: EventSourceKind
    kind: str
    payload: dict[str, Any]
    turn_generation: int = 0
    speech_generation: int = 0
    monotonic_ns: int = 0
    error_category: Optional[ErrorCategory] = None


class CallEventLog:
    """Best-effort SQLite writer for call events.

    Per-process singleton.  Writer runs synchronously (SQLite handles
    concurrent writers via internal lock) but under a try/except so a
    logging failure NEVER blocks a call."""

    def __init__(self, db_path: str, max_rows_per_call: int = 5000,
                 retention_days: int = 14) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_rows_per_call = max_rows_per_call
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._per_call_counts: dict[str, int] = {}
        # Streaming-parity extension: in-process fan-out for /debug/live.
        # Subscribers see the same event dict shape `timeline()` returns,
        # after the SQLite insert.  A failing subscriber never blocks the writer.
        self._subscribers: dict[str, set[Callable[[dict], None]]] = defaultdict(set)
        self._sub_lock = threading.Lock()
        self._init_schema()
        self._prune_old_rows()

    def subscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].add(cb)

    def unsubscribe(self, call_id: str, cb: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subscribers[call_id].discard(cb)
            if not self._subscribers[call_id]:
                del self._subscribers[call_id]

    def _fanout(self, event: CallEvent) -> None:
        # Snapshot subscribers under lock so we can call them lock-free.
        with self._sub_lock:
            subs = list(self._subscribers.get(event.call_id, ()))
        if not subs:
            return
        payload = {
            "call_id": event.call_id,
            "tenant_id": event.tenant_id,
            "turn_generation": event.turn_generation,
            "speech_generation": event.speech_generation,
            "monotonic_ns": event.monotonic_ns,
            "wall_ts": time.time(),
            "source": event.source.value,
            "kind": event.kind,
            "payload": event.payload,
            "error_category": event.error_category.value if event.error_category else None,
        }
        for cb in subs:
            try:
                cb(payload)
            except Exception as e:
                log.debug("subscriber cb failed: %s", e)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    turn_generation INTEGER DEFAULT 0,
                    speech_generation INTEGER DEFAULT 0,
                    monotonic_ns INTEGER DEFAULT 0,
                    wall_ts REAL NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    error_category TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_call_events_call ON call_events(call_id, wall_ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_call_events_tenant_ts ON call_events(tenant_id, wall_ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_call_events_error ON call_events(error_category, wall_ts) WHERE error_category IS NOT NULL"
            )

    def _prune_old_rows(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = time.time() - (self._retention_days * 86400)
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM call_events WHERE wall_ts < ?", (cutoff,),
                )
        except Exception as e:
            log.warning("call event log prune failed: %s", e)

    def write(self, event: CallEvent) -> None:
        """Persist one event.  Enforces per-call row cap so a stuck
        call can't blow up the DB.  Silent on failure — never raises."""
        with self._lock:
            count = self._per_call_counts.get(event.call_id, 0)
            if count >= self._max_rows_per_call:
                return
            self._per_call_counts[event.call_id] = count + 1
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO call_events (
                        call_id, tenant_id, turn_generation, speech_generation,
                        monotonic_ns, wall_ts, source, kind, payload, error_category
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.call_id,
                        event.tenant_id,
                        event.turn_generation,
                        event.speech_generation,
                        event.monotonic_ns,
                        time.time(),
                        event.source.value,
                        event.kind,
                        _serialize_payload(event.payload),
                        event.error_category.value if event.error_category else None,
                    ),
                )
        except Exception as e:
            # NEVER raise from the log path
            log.debug("call_event write failed: %s", e)
        # Fan out to live subscribers AFTER persistence.  Failure to
        # fan out never affects the caller.
        try:
            self._fanout(event)
        except Exception as e:
            log.debug("call_event fanout failed: %s", e)

    def write_error(
        self, call_id: str, tenant_id: str, message: str,
        exc_type: str = "", context: Optional[dict] = None,
        turn_generation: int = 0,
    ) -> ErrorCategory:
        """Convenience: classify + write in one call.  Returns the
        category so the caller can also bump a Prometheus counter."""
        category = classify_error(message, exc_type)
        self.write(CallEvent(
            call_id=call_id, tenant_id=tenant_id,
            source=EventSourceKind.ERROR, kind=exc_type or "error",
            payload={"message": message, "context": context or {}},
            error_category=category,
            turn_generation=turn_generation,
        ))
        return category

    # ── read path ───────────────────────────────────────────────────

    def timeline(self, call_id: str, limit: int = 500) -> list[dict]:
        """Return the last `limit` events for a call, newest first.
        Used by /debug/call/{call_id} to render the timeline."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, call_id, tenant_id, turn_generation,
                           speech_generation, monotonic_ns, wall_ts,
                           source, kind, payload, error_category
                    FROM call_events
                    WHERE call_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (call_id, limit),
                ).fetchall()
        except Exception as e:
            log.warning("call event timeline read failed: %s", e)
            return []
        return [_row_to_dict(r) for r in rows]

    def recent_errors(self, tenant_id: Optional[str] = None,
                      hours: int = 24, limit: int = 200) -> list[dict]:
        """Recent classified errors — feeds a failure dashboard."""
        cutoff = time.time() - (hours * 3600)
        try:
            with self._connect() as conn:
                if tenant_id is not None:
                    rows = conn.execute(
                        """
                        SELECT * FROM call_events
                        WHERE error_category IS NOT NULL
                          AND tenant_id = ?
                          AND wall_ts > ?
                        ORDER BY wall_ts DESC
                        LIMIT ?
                        """,
                        (tenant_id, cutoff, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM call_events
                        WHERE error_category IS NOT NULL AND wall_ts > ?
                        ORDER BY wall_ts DESC
                        LIMIT ?
                        """,
                        (cutoff, limit),
                    ).fetchall()
        except Exception as e:
            log.warning("recent_errors query failed: %s", e)
            return []
        return [_row_to_dict(r) for r in rows]

    def error_category_counts(
        self, tenant_id: Optional[str] = None, hours: int = 24,
    ) -> dict[str, int]:
        """Rollup by category — for the failure taxonomy dashboard."""
        cutoff = time.time() - (hours * 3600)
        try:
            with self._connect() as conn:
                if tenant_id is not None:
                    rows = conn.execute(
                        """
                        SELECT error_category, COUNT(*) as n
                        FROM call_events
                        WHERE error_category IS NOT NULL
                          AND tenant_id = ? AND wall_ts > ?
                        GROUP BY error_category
                        """,
                        (tenant_id, cutoff),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT error_category, COUNT(*) as n
                        FROM call_events
                        WHERE error_category IS NOT NULL AND wall_ts > ?
                        GROUP BY error_category
                        """,
                        (cutoff,),
                    ).fetchall()
        except Exception:
            return {}
        return {r["error_category"]: int(r["n"]) for r in rows}


# ── helpers ─────────────────────────────────────────────────────────

def _serialize_payload(payload: Any) -> str:
    try:
        return json.dumps(payload, default=str)
    except Exception:
        return str(payload)[:2000]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ── module-level singleton (lazy) ───────────────────────────────────

_SINGLETON: Optional[CallEventLog] = None
_SINGLETON_LOCK = threading.Lock()


def get_call_event_log() -> CallEventLog:
    """Lazy singleton.  Path from CALL_EVENT_LOG_PATH env or
    data/call_events.db default.  Retention from CALL_EVENT_LOG_RETENTION_DAYS
    env (0 or negative = never prune — default now).  The per-call row cap
    (5000) already keeps a runaway call from blowing up the DB."""
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            path = os.environ.get(
                "CALL_EVENT_LOG_PATH",
                str(Path(__file__).resolve().parents[2] / "data" / "call_events.db"),
            )
            try:
                retention_days = int(os.environ.get("CALL_EVENT_LOG_RETENTION_DAYS", "0"))
            except (TypeError, ValueError):
                retention_days = 0
            _SINGLETON = CallEventLog(db_path=path, retention_days=retention_days)
    return _SINGLETON


def reset_singleton_for_tests() -> None:
    """Test helper — force a fresh log with a per-test path."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
