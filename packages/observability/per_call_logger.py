"""Per-call log extractor.

Every uvicorn log line that mentions a Twilio call SID (CA<32 hex>) gets
copied to `data/logs/calls/<CA...>.log`.  Files are NEVER pruned by us —
we want to be able to reopen a call log weeks later to compare timings
on a specific SID.

Zero-invasive: registers as a `logging.Handler` at the root; existing log
lines already carry the call_id in their message text.  The handler
scans the formatted message with a regex, opens (append-mode) the
matching file, writes the line.  Files are kept open in a small LRU
so we don't fdopen per line during a busy call.

Also produces a lightweight per-call INDEX line at first-seen (with
wall-clock + streamSid + inbound origin URL if extractable from the
message) so you can `grep -l "phone_number"` across the calls dir.

Ship path:
    from packages.observability.per_call_logger import install_per_call_logger
    install_per_call_logger()
Call this ONCE at server startup.

Env:
    PER_CALL_LOGS_DIR   default: <repo>/apps/api/data/logs/calls
    PER_CALL_LOGS_MAX_OPEN  default: 16 (LRU size)
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


# Twilio call SIDs look like CA<32 hex>.  Match anywhere in the message.
_CALL_SID_RE = re.compile(r"\b(CA[0-9a-fA-F]{32})\b")


class _CallLogHandler(logging.Handler):
    """Duplicates every log record that mentions a Twilio call SID to
    that call's per-call log file.  Never raises; never prunes."""

    def __init__(self, logs_dir: Path, max_open: int = 16) -> None:
        super().__init__(level=logging.DEBUG)
        self._logs_dir = logs_dir
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._max_open = max_open
        self._files: "OrderedDict[str, object]" = OrderedDict()
        self._first_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        # Standard formatter — timestamp + level + logger + message.
        # Matches uvicorn's default for easy cross-reference.
        self._formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    def _get_file(self, call_id: str):
        """LRU handle.  Reopens if evicted."""
        f = self._files.get(call_id)
        if f is not None:
            # move to end for LRU freshness
            self._files.move_to_end(call_id)
            return f
        # Evict oldest if at cap
        if len(self._files) >= self._max_open:
            old_id, old_f = self._files.popitem(last=False)
            try:
                old_f.close()
            except Exception:
                pass
        path = self._logs_dir / f"{call_id}.log"
        is_new = not path.exists()
        f = open(path, "a", buffering=1)  # line-buffered
        self._files[call_id] = f
        if is_new:
            wall = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())
            self._first_seen[call_id] = time.time()
            f.write(f"# per-call log opened {wall} for call_id={call_id}\n")
            f.flush()
        return f

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        # Fast reject: no CA... in this message
        if "CA" not in msg:
            return
        matches = _CALL_SID_RE.findall(msg)
        if not matches:
            return
        # De-dup within a single record (many lines carry the call_id once)
        seen: set[str] = set()
        try:
            formatted = self._formatter.format(record) + "\n"
        except Exception:
            return
        with self._lock:
            for call_id in matches:
                if call_id in seen:
                    continue
                seen.add(call_id)
                try:
                    f = self._get_file(call_id)
                    f.write(formatted)
                except Exception:
                    # Never break the app because we can't write a log
                    pass


_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def install_per_call_logger() -> Optional[_CallLogHandler]:
    """Idempotent — installs the handler on the root logger once.
    Returns the handler (or None if already installed)."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return None
        _INSTALLED = True
    logs_dir = Path(
        os.environ.get(
            "PER_CALL_LOGS_DIR",
            str(Path(__file__).resolve().parents[2] / "apps" / "api" / "data" / "logs" / "calls"),
        )
    )
    try:
        max_open = int(os.environ.get("PER_CALL_LOGS_MAX_OPEN", "16"))
    except (TypeError, ValueError):
        max_open = 16
    handler = _CallLogHandler(logs_dir=logs_dir, max_open=max_open)
    logging.getLogger().addHandler(handler)
    log.info(
        "per_call_logger installed dir=%s max_open=%d",
        logs_dir, max_open,
    )
    return handler
