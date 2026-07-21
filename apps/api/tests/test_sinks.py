from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.integrations.sinks import (
    CompositeSink,
    CRMSink,
    NoopSink,
    build_sink_from_env,
)
from packages.schemas import CallState, CallStatus, Intent, Urgency, ExtractedFields


class RecordingSink(CRMSink):
    name = "recording"

    def __init__(self) -> None:
        self.bookings: list[tuple[CallState, dict]] = []
        self.ends: list[CallState] = []

    async def on_booking(self, state: CallState, booking: dict) -> None:
        self.bookings.append((state, booking))

    async def on_call_end(self, state: CallState) -> None:
        self.ends.append(state)


class ExplodingSink(CRMSink):
    name = "exploding"

    async def on_booking(self, state: CallState, booking: dict) -> None:
        raise RuntimeError("kaboom on booking")

    async def on_call_end(self, state: CallState) -> None:
        raise RuntimeError("kaboom on end")


def _sample_state() -> CallState:
    return CallState(
        session_id="s1",
        business_id="b1",
        status=CallStatus.COMPLETED,
        extracted=ExtractedFields(
            caller_name="John Carter",
            phone="5551234567",
            intent=Intent.BOOK_APPOINTMENT,
            urgency=Urgency.LOW,
            lead_score=70,
            summary="Booked back-pain consult",
        ),
    )


@pytest.mark.asyncio
async def test_noop_sink_does_nothing():
    sink = NoopSink()
    state = _sample_state()
    await sink.on_booking(state, {"name": "book_appointment", "result": {"booked": True}})
    await sink.on_call_end(state)


@pytest.mark.asyncio
async def test_composite_sink_isolates_failures():
    recording = RecordingSink()
    composite = CompositeSink([ExplodingSink(), recording, ExplodingSink()])
    state = _sample_state()
    await composite.on_booking(state, {"name": "book_appointment", "result": {"booked": True}})
    await composite.on_call_end(state)
    assert len(recording.bookings) == 1
    assert len(recording.ends) == 1


@pytest.mark.asyncio
async def test_build_sink_from_env_none():
    class Settings:
        pass
    sink = build_sink_from_env("none", Settings())
    assert isinstance(sink, NoopSink)


@pytest.mark.asyncio
async def test_build_sink_from_env_ghl(monkeypatch):
    class Settings:
        ghl_api_token = "pit-fake"
        ghl_location_id = "loc_fake"
        ghl_api_version = "2021-07-28"
        ghl_calendar_id = None
    sink = build_sink_from_env("ghl", Settings())
    assert sink.name == "ghl"
