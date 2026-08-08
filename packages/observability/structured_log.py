"""Structured JSON log formatter — Sprint 10 obs layer.

Standard Python logging writes free-text.  For a voice-agent
production system we want:
  * `call_id`, `tenant_id`, `turn_generation` on every record
  * Machine-parseable JSON so `jq` + Grafana Loki + BigQuery works
  * Env-toggled — dev keeps human logs, prod flips to JSON

Enable by setting `STRUCTURED_LOGS=true` in env.  When off, behavior
unchanged (existing basicConfig format).

Contextvars carry the call scope through async code without touching
every log call site — the formatter reads them lazily.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any, Optional

# Contextvars: set by request/actor entrypoints, read by the formatter.
current_call_id: ContextVar[Optional[str]] = ContextVar("current_call_id", default=None)
current_tenant_id: ContextVar[Optional[str]] = ContextVar("current_tenant_id", default=None)
current_turn_generation: ContextVar[Optional[int]] = ContextVar(
    "current_turn_generation", default=None,
)


def bind_call_context(
    call_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    turn_generation: Optional[int] = None,
) -> dict:
    """Set contextvars for the current call.  Returns tokens for
    reset() — callers should reset when the call scope ends."""
    tokens = {}
    if call_id is not None:
        tokens["call"] = current_call_id.set(call_id)
    if tenant_id is not None:
        tokens["tenant"] = current_tenant_id.set(tenant_id)
    if turn_generation is not None:
        tokens["turn"] = current_turn_generation.set(turn_generation)
    return tokens


def reset_call_context(tokens: dict) -> None:
    if "call" in tokens:
        current_call_id.reset(tokens["call"])
    if "tenant" in tokens:
        current_tenant_id.reset(tokens["tenant"])
    if "turn" in tokens:
        current_turn_generation.reset(tokens["turn"])


class JSONFormatter(logging.Formatter):
    """One log line = one JSON object.

    Fields:
      ts       — ISO timestamp (float epoch)
      level    — DEBUG/INFO/WARNING/ERROR/CRITICAL
      logger   — module logger name
      msg      — human-readable message
      call_id  — if bound
      tenant_id — if bound
      turn     — if bound
      err_type — exception class when logger.exception was used
      err_msg  — exception message when logger.exception was used
    """

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        call_id = current_call_id.get()
        if call_id:
            obj["call_id"] = call_id
        tenant_id = current_tenant_id.get()
        if tenant_id:
            obj["tenant_id"] = tenant_id
        turn = current_turn_generation.get()
        if turn is not None:
            obj["turn"] = turn

        # Include exception info when present
        if record.exc_info:
            obj["err_type"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
            obj["err_msg"] = str(record.exc_info[1]) if record.exc_info[1] else ""

        # Attached `extra=` fields flow through as record.__dict__ keys
        # not present on the stock LogRecord.  Include any that look
        # simple (not private, JSON-safe).
        for key, val in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in _STOCK_LOG_KEYS:
                continue
            try:
                json.dumps(val, default=str)
                obj[key] = val
            except (TypeError, ValueError):
                pass

        return json.dumps(obj, default=str)


# Stock LogRecord attrs we don't want to duplicate in the JSON output
_STOCK_LOG_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


def install_json_formatter() -> None:
    """Replace all root handlers with a JSON-formatted stderr handler.

    Called from main.py when STRUCTURED_LOGS=true.  Idempotent."""
    root = logging.getLogger()
    # Drop existing handlers so we don't double-emit
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


def maybe_install() -> None:
    """Install only when the env flag is on.  Called at import-time
    by app.main."""
    if os.environ.get("STRUCTURED_LOGS", "").lower() in ("true", "1", "yes"):
        install_json_formatter()
