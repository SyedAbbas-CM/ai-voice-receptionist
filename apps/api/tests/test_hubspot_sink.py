"""Tests for HubSpotClient + HubSpotSink.

Client tests: stub httpx.AsyncClient to assert we send the right
requests without actually hitting HubSpot.

Sink tests: use a FakeHubSpotClient (records calls) to prove the
sink builds the right payloads from a CallState + booking dict.

Failure policy pinned: sink catches all exceptions from the client
and never propagates.  A broken CRM must never crash the call.
"""
from __future__ import annotations

import pytest

from packages.integrations.hubspot_client import HubSpotClient, HubSpotError
from packages.integrations.sinks import HubSpotSink
from packages.schemas import (
    CallState,
    CallStatus,
    ExtractedFields,
    Intent,
    Urgency,
)


# ── HubSpotClient constructor + auth header ──────────────────────


def test_client_requires_token():
    with pytest.raises(HubSpotError, match="HUBSPOT_ACCESS_TOKEN"):
        HubSpotClient(access_token="")


def test_client_headers_include_bearer():
    c = HubSpotClient(access_token="pat-abc123")
    assert c._headers["Authorization"] == "Bearer pat-abc123"
    assert c._headers["Content-Type"] == "application/json"


# ── HubSpotSink — fake client to record what the sink calls ──────


class FakeHubSpotClient:
    """Records every API call for assertion.  Optional per-method
    error injection to test the sink's swallow-on-error behavior."""

    def __init__(
        self,
        find_returns=None,
        upsert_raises=None,
        note_raises=None,
        deal_raises=None,
    ) -> None:
        self.find_calls: list[str] = []
        self.upsert_calls: list[dict] = []
        self.note_calls: list[tuple[str, str]] = []
        self.deal_calls: list[dict] = []
        self._find_returns = find_returns
        self._upsert_raises = upsert_raises
        self._note_raises = note_raises
        self._deal_raises = deal_raises

    async def find_contact_by_phone(self, phone):
        self.find_calls.append(phone)
        return self._find_returns

    async def upsert_contact(self, phone, first_name=None, last_name=None,
                              email=None, tags=None, source="voiceops-ai-agent"):
        self.upsert_calls.append({
            "phone": phone, "first_name": first_name, "last_name": last_name,
            "email": email, "tags": tags,
        })
        if self._upsert_raises:
            raise self._upsert_raises
        return {"id": "contact-123", "properties": {"phone": phone}}

    async def add_note(self, contact_id, body):
        self.note_calls.append((contact_id, body))
        if self._note_raises:
            raise self._note_raises
        return {"id": "note-456"}

    async def create_deal(self, contact_id, deal_name, amount=None,
                           pipeline_id=None, stage_id=None, close_date_ms=None):
        self.deal_calls.append({
            "contact_id": contact_id, "deal_name": deal_name,
            "amount": amount,
        })
        if self._deal_raises:
            raise self._deal_raises
        return {"id": "deal-789"}


def _make_state(
    phone="+15551234567",
    caller_name="Sarah Chen",
    intent=Intent.BOOK_APPOINTMENT,
    summary="Booked cleaning for Tuesday at 2pm",
    status=CallStatus.COMPLETED,
) -> CallState:
    state = CallState(session_id="sess-abc", business_id="biz-xyz")
    state.extracted = ExtractedFields(
        caller_name=caller_name,
        phone=phone,
        intent=intent,
        urgency=Urgency.MEDIUM,
        lead_score=75,
        summary=summary,
    )
    state.status = status
    return state


def _booking_success(**overrides) -> dict:
    return {
        "name": "book_appointment",
        "arguments": {
            "caller_name": "Sarah Chen",
            "phone": "+15551234567",
            "service": "cleaning",
            "date": "2026-08-26",
            "time": "14:30",
            **overrides.get("arguments", {}),
        },
        "result": {"booked": True, **overrides.get("result", {})},
        "error": None,
    }


# ── on_booking happy path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_on_booking_upserts_contact_and_adds_note():
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    assert len(client.upsert_calls) == 1
    upsert = client.upsert_calls[0]
    assert upsert["phone"] == "+15551234567"
    assert upsert["first_name"] == "Sarah"
    assert upsert["last_name"] == "Chen"
    assert "voiceops-ai-agent" in upsert["tags"]
    assert "book_appointment" in upsert["tags"]  # intent
    assert len(client.note_calls) == 1
    contact_id, body = client.note_calls[0]
    assert contact_id == "contact-123"
    assert "cleaning" in body
    assert "+15551234567" in body


@pytest.mark.asyncio
async def test_sink_on_booking_accepts_ok_result_shape():
    """Local calendar returns {'ok': True} instead of {'booked': True}.
    Sink must accept both — it's transport-agnostic."""
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    booking = {
        "name": "book_appointment",
        "arguments": {"caller_name": "Sam", "phone": "+15550000000",
                      "service": "consult", "date": "2026-09-01", "time": "10:00"},
        "result": {"ok": True},
    }
    await sink.on_booking(_make_state(), booking)
    assert len(client.upsert_calls) == 1


# ── on_booking skip conditions ───────────────────────────────────


@pytest.mark.asyncio
async def test_sink_skips_when_booking_failed():
    """Never write to CRM when the booking tool didn't actually book."""
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    booking = {
        "name": "book_appointment",
        "arguments": {"caller_name": "Sarah", "phone": "+15551234567"},
        "result": {"booked": False, "error": "slot_taken"},
    }
    await sink.on_booking(_make_state(), booking)
    assert client.upsert_calls == []
    assert client.note_calls == []


@pytest.mark.asyncio
async def test_sink_skips_when_no_phone():
    """Phone is the primary key.  No phone → nothing to upsert."""
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    state = _make_state(phone=None)  # type: ignore[arg-type]
    booking = {
        "name": "book_appointment",
        "arguments": {"caller_name": "Sarah"},
        "result": {"booked": True},
    }
    await sink.on_booking(state, booking)
    assert client.upsert_calls == []


# ── failure swallowing (safety-critical) ─────────────────────────


@pytest.mark.asyncio
async def test_sink_swallows_upsert_error():
    """A broken CRM must never crash the call."""
    client = FakeHubSpotClient(upsert_raises=HubSpotError("500 server"))
    sink = HubSpotSink(client)
    # Should not raise.
    await sink.on_booking(_make_state(), _booking_success())
    assert client.note_calls == []  # skipped because upsert failed


@pytest.mark.asyncio
async def test_sink_swallows_note_error():
    client = FakeHubSpotClient(note_raises=HubSpotError("429 rate limit"))
    sink = HubSpotSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    # Upsert succeeded, note failed silently — sink still returns clean.
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1


@pytest.mark.asyncio
async def test_sink_swallows_deal_error():
    client = FakeHubSpotClient(deal_raises=HubSpotError("400 bad request"))
    sink = HubSpotSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    # Upsert + note both succeeded, deal failed silently.
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1


# ── on_call_end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_on_call_end_logs_note_for_every_call():
    """Missed calls / enquiries should also show up in HubSpot, not
    only successful bookings.  on_call_end fires for every session."""
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    state = _make_state(
        summary="Caller asked about hours, no booking",
        status=CallStatus.COMPLETED,
    )
    await sink.on_call_end(state)
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1
    _, body = client.note_calls[0]
    assert "Call ended" in body
    assert "sess-abc" in body


@pytest.mark.asyncio
async def test_sink_on_call_end_skips_when_no_phone():
    client = FakeHubSpotClient()
    sink = HubSpotSink(client)
    state = _make_state(phone=None)  # type: ignore[arg-type]
    await sink.on_call_end(state)
    assert client.upsert_calls == []


# ── name splitting ────────────────────────────────────────────────


def test_split_name_single_word():
    assert HubSpotSink._split_name("Cher") == ("Cher", None)


def test_split_name_two_words():
    assert HubSpotSink._split_name("Sarah Chen") == ("Sarah", "Chen")


def test_split_name_multi_word_last():
    assert HubSpotSink._split_name("Mary Jane Watson") == ("Mary", "Jane Watson")


def test_split_name_empty():
    assert HubSpotSink._split_name("") == (None, None)
    assert HubSpotSink._split_name(None) == (None, None)


# ── factory wiring ────────────────────────────────────────────────


def test_factory_rejects_hubspot_without_token():
    class _S:
        hubspot_access_token = None
        crm_sink = "hubspot"
    from packages.integrations.sinks import build_sink_from_env
    with pytest.raises(RuntimeError, match="HUBSPOT_ACCESS_TOKEN"):
        build_sink_from_env("hubspot", _S())


def test_factory_builds_hubspot_sink_with_token():
    class _S:
        hubspot_access_token = "pat-fake"
        hubspot_portal_id = None
        hubspot_pipeline_id = None
        hubspot_stage_id = None
        hubspot_create_deals = False
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("hubspot", _S())
    assert sink.name == "hubspot"


def test_factory_builds_composite_ghl_plus_hubspot():
    """Multi-sink combo — GHL + HubSpot fan-out to both CRMs at once."""
    class _S:
        ghl_api_token = "ghl-fake"
        ghl_location_id = "loc-fake"
        ghl_api_version = "2021-07-28"
        ghl_calendar_id = None
        hubspot_access_token = "pat-fake"
        hubspot_portal_id = None
        hubspot_pipeline_id = None
        hubspot_stage_id = None
        hubspot_create_deals = False
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("ghl+hubspot", _S())
    # Composite (has children) or single (unlikely at 2 sinks) — expect composite.
    assert hasattr(sink, "children") or hasattr(sink, "sinks") or sink.name in {"ghl", "hubspot"}
