"""Sprint 12 Track A tests: spawn_supervised + emit_local + source_epoch."""
from __future__ import annotations

import asyncio
import pytest

from packages.runtime import CallActor, CallEvent, EventSource


def test_call_event_has_source_epoch_default_zero():
    ev = CallEvent.new(
        call_id="c1", tenant_id="t1", source=EventSource.STT,
        turn_generation=3, speech_generation=1, kind="partial",
    )
    assert ev.source_epoch == 0  # default


def test_call_event_new_accepts_source_epoch():
    ev = CallEvent.new(
        call_id="c1", tenant_id="t1", source=EventSource.STT,
        turn_generation=5, speech_generation=1, kind="partial",
        source_epoch=3,   # captured back when turn was 3
    )
    assert ev.source_epoch == 3
