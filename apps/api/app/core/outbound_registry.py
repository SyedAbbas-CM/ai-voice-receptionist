"""Outbound call registry — maps Vapi call IDs to the source lead so the
end-of-call webhook can write disposition back to the right sheet row.

Keyed by `vapi_{call_id}` (same key session_manager uses). Values remember:
  - source (sheet id + tab + row number)
  - lead context (name, phone, address, old rent)
  - vertical business_id so we can pick the right disposition schema

In-memory for now — a client-scale deployment would swap this for a Redis
hash or a SQLite table. Kept as an interface so that swap is a 1-file change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Optional


@dataclass
class OutboundContext:
    session_id: str        # "vapi_{call_id}"
    business_id: str
    lead: dict = field(default_factory=dict)  # original sheet row (with _row_number)
    sheet_id: Optional[str] = None
    sheet_tab: Optional[str] = None


_registry: dict[str, OutboundContext] = {}
_lock = Lock()


def remember(ctx: OutboundContext) -> None:
    with _lock:
        _registry[ctx.session_id] = ctx


def pop(session_id: str) -> Optional[OutboundContext]:
    with _lock:
        return _registry.pop(session_id, None)


def get(session_id: str) -> Optional[OutboundContext]:
    with _lock:
        return _registry.get(session_id)


def size() -> int:
    with _lock:
        return len(_registry)
