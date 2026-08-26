"""Tests for PipedriveClient + PipedriveSink.

Mirrors test_hubspot_sink.py structure since PipedriveClient's public
surface intentionally mirrors HubSpotClient's.

Client tests: no real HTTP — use a FakePipedriveClient recorder to
verify the SINK sends the right shape.  Client-internal retry logic
is separately tested in test_hubspot_retry.py (Pipedrive uses the
same backoff / retryable-status semantics, so the pattern is proven).

Sink tests: mirror test_hubspot_sink — happy path, skip failed booking,
skip no phone, failure isolation, on_call_end covers missed calls too.
"""
from __future__ import annotations

import pytest

from packages.integrations.pipedrive_client import (
    PipedriveClient,
    PipedriveError,
)
from packages.integrations.sinks import PipedriveSink
from packages.schemas import (
    CallState,
    CallStatus,
    ExtractedFields,
    Intent,
    Urgency,
)


# ── PipedriveClient constructor + domain normalization ────────────


def test_client_requires_token():
    with pytest.raises(PipedriveError, match="PIPEDRIVE_API_TOKEN"):
        PipedriveClient(api_token="", company_domain="acme")


def test_client_requires_domain():
    with pytest.raises(PipedriveError, match="PIPEDRIVE_COMPANY_DOMAIN"):
        PipedriveClient(api_token="pat-x", company_domain="")


def test_client_normalizes_domain_strips_scheme():
    """User might paste `https://acme.pipedrive.com/dashboard` — we
    normalize to `acme` before building the base URL."""
    c = PipedriveClient(api_token="pat-x",
                          company_domain="https://acme.pipedrive.com/dashboard")
    assert c.company_domain == "acme"
    assert c.base_url == "https://acme.pipedrive.com/api/v1"


def test_client_normalizes_domain_strips_suffix():
    c = PipedriveClient(api_token="pat-x",
                          company_domain="acme.pipedrive.com")
    assert c.company_domain == "acme"


def test_client_domain_bare_subdomain():
    c = PipedriveClient(api_token="pat-x", company_domain="acme")
    assert c.company_domain == "acme"
    assert c.base_url == "https://acme.pipedrive.com/api/v1"


# ── PipedriveSink — fake client to record calls ───────────────────


class FakePipedriveClient:
    """Records every API call for assertion.  Optional per-method
    error injection matches the HubSpot fake."""

    def __init__(
        self,
        find_returns=None,
        upsert_raises=None,
        note_raises=None,
        deal_raises=None,
        activity_raises=None,
    ) -> None:
        self.find_calls: list[str] = []
        self.upsert_calls: list[dict] = []
        self.note_calls: list[dict] = []
        self.deal_calls: list[dict] = []
        self.activity_calls: list[dict] = []
        self._find_returns = find_returns
        self._upsert_raises = upsert_raises
        self._note_raises = note_raises
        self._deal_raises = deal_raises
        self._activity_raises = activity_raises

    async def find_person_by_phone(self, phone):
        self.find_calls.append(phone)
        return self._find_returns

    async def upsert_person(self, phone, first_name=None, last_name=None,
                              email=None, label=None):
        self.upsert_calls.append({
            "phone": phone, "first_name": first_name,
            "last_name": last_name, "email": email, "label": label,
        })
        if self._upsert_raises:
            raise self._upsert_raises
        return {"id": 12345, "name": (first_name or "") + " "
                                       + (last_name or "")}

    async def add_note(self, content, person_id=None, deal_id=None):
        self.note_calls.append({
            "content": content, "person_id": person_id, "deal_id": deal_id,
        })
        if self._note_raises:
            raise self._note_raises
        return {"id": 98765}

    async def create_deal(self, person_id, title, value=None,
                            currency="EUR", pipeline_id=None, stage_id=None):
        self.deal_calls.append({
            "person_id": person_id, "title": title, "value": value,
            "currency": currency,
        })
        if self._deal_raises:
            raise self._deal_raises
        return {"id": 55555}

    async def create_activity(self, subject, due_date, due_time,
                                duration=None, activity_type="meeting",
                                person_id=None, deal_id=None, note=None):
        self.activity_calls.append({
            "subject": subject, "due_date": due_date, "due_time": due_time,
            "duration": duration, "activity_type": activity_type,
            "person_id": person_id, "note": note,
        })
        if self._activity_raises:
            raise self._activity_raises
        return {"id": 11111}


def _make_state(
    phone="+351911234567",
    caller_name="João Silva",
    intent=Intent.BOOK_APPOINTMENT,
    summary="Booked viewing for Chiado apartment",
    status=CallStatus.COMPLETED,
) -> CallState:
    state = CallState(session_id="sess-p1", business_id="ribeira-prime")
    state.extracted = ExtractedFields(
        caller_name=caller_name,
        phone=phone,
        intent=intent,
        urgency=Urgency.MEDIUM,
        lead_score=80,
        summary=summary,
    )
    state.status = status
    return state


def _booking_success(**overrides) -> dict:
    return {
        "name": "book_viewing",
        "arguments": {
            "caller_name": "João Silva",
            "phone": "+351911234567",
            "service": "Property viewing",
            "start_iso": "2026-08-28T15:00:00",
            **overrides.get("arguments", {}),
        },
        "result": {"booked": True, **overrides.get("result", {})},
        "error": None,
    }


# ── on_booking happy path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_on_booking_upserts_person_and_notes():
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    assert len(client.upsert_calls) == 1
    upsert = client.upsert_calls[0]
    assert upsert["phone"] == "+351911234567"
    assert upsert["first_name"] == "João"
    assert upsert["last_name"] == "Silva"
    assert upsert["label"] == "voiceops-ai-agent"
    assert len(client.note_calls) == 1
    note = client.note_calls[0]
    assert note["person_id"] == 12345
    assert "Property viewing" in note["content"]
    assert "+351911234567" in note["content"]


@pytest.mark.asyncio
async def test_sink_on_booking_creates_activity_from_start_iso():
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    assert len(client.activity_calls) == 1
    act = client.activity_calls[0]
    assert act["due_date"] == "2026-08-28"
    assert act["due_time"] == "15:00"
    assert act["person_id"] == 12345
    assert "Property viewing" in act["subject"]


@pytest.mark.asyncio
async def test_sink_on_booking_accepts_ok_result_shape():
    """Local calendar returns {'ok': True} not {'booked': True}."""
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    booking = {
        "name": "book_viewing",
        "arguments": {"caller_name": "Ana", "phone": "+351912000001",
                        "service": "Home valuation",
                        "start_iso": "2026-09-01T10:00:00"},
        "result": {"ok": True},
    }
    await sink.on_booking(_make_state(), booking)
    assert len(client.upsert_calls) == 1


# ── skip conditions ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_skips_when_booking_failed():
    """Never write when booking wasn't successful."""
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    booking = {
        "name": "book_viewing",
        "arguments": {"caller_name": "X", "phone": "+351912000002"},
        "result": {"booked": False, "reason": "slot_taken"},
    }
    await sink.on_booking(_make_state(), booking)
    assert client.upsert_calls == []
    assert client.note_calls == []


@pytest.mark.asyncio
async def test_sink_skips_when_no_phone():
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    state = _make_state(phone=None)  # type: ignore[arg-type]
    booking = {
        "name": "book_viewing",
        "arguments": {"caller_name": "X"},
        "result": {"booked": True},
    }
    await sink.on_booking(state, booking)
    assert client.upsert_calls == []


# ── failure isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_swallows_upsert_error():
    client = FakePipedriveClient(upsert_raises=PipedriveError("500"))
    sink = PipedriveSink(client)
    # Must not raise.
    await sink.on_booking(_make_state(), _booking_success())
    # Upsert attempted, note skipped because we couldn't identify the person.
    assert client.note_calls == []


@pytest.mark.asyncio
async def test_sink_swallows_note_error():
    client = FakePipedriveClient(note_raises=PipedriveError("429"))
    sink = PipedriveSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1  # attempted, failed
    # Activity + deal still attempted (independent).
    assert len(client.activity_calls) == 1


@pytest.mark.asyncio
async def test_sink_swallows_activity_error():
    client = FakePipedriveClient(activity_raises=PipedriveError("400"))
    sink = PipedriveSink(client)
    await sink.on_booking(_make_state(), _booking_success())
    # Other paths still fire despite the activity failure.
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1


# ── on_call_end (missed calls / enquiries) ─────────────────────


@pytest.mark.asyncio
async def test_sink_on_call_end_logs_note_for_missed_call():
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    state = _make_state(
        summary="Caller asked about hours, no booking",
        status=CallStatus.COMPLETED,
    )
    await sink.on_call_end(state)
    assert len(client.upsert_calls) == 1
    assert len(client.note_calls) == 1
    note = client.note_calls[0]
    assert "Call ended" in note["content"]
    assert "sess-p1" in note["content"]


@pytest.mark.asyncio
async def test_sink_on_call_end_skips_no_phone():
    client = FakePipedriveClient()
    sink = PipedriveSink(client)
    state = _make_state(phone=None)  # type: ignore[arg-type]
    await sink.on_call_end(state)
    assert client.upsert_calls == []


# ── factory wiring ────────────────────────────────────────────


def test_factory_rejects_pipedrive_without_token():
    class _S:
        pipedrive_api_token = None
        pipedrive_company_domain = "acme"
        crm_sink = "pipedrive"
    from packages.integrations.sinks import build_sink_from_env
    with pytest.raises(RuntimeError, match="PIPEDRIVE_API_TOKEN"):
        build_sink_from_env("pipedrive", _S())


def test_factory_rejects_pipedrive_without_domain():
    class _S:
        pipedrive_api_token = "pat-fake"
        pipedrive_company_domain = None
        crm_sink = "pipedrive"
    from packages.integrations.sinks import build_sink_from_env
    with pytest.raises(RuntimeError, match="PIPEDRIVE_COMPANY_DOMAIN"):
        build_sink_from_env("pipedrive", _S())


def test_factory_builds_pipedrive_sink_with_creds():
    class _S:
        pipedrive_api_token = "pat-fake"
        pipedrive_company_domain = "acme"
        pipedrive_pipeline_id = None
        pipedrive_stage_id = None
        pipedrive_create_deals = False
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("pipedrive", _S())
    assert sink.name == "pipedrive"


def test_factory_builds_composite_hubspot_plus_pipedrive():
    """Multi-sink combo — dual-write to both CRMs is common."""
    class _S:
        hubspot_access_token = "pat-hs"
        hubspot_portal_id = None
        hubspot_pipeline_id = None
        hubspot_stage_id = None
        hubspot_create_deals = False
        pipedrive_api_token = "pat-pd"
        pipedrive_company_domain = "acme"
        pipedrive_pipeline_id = None
        pipedrive_stage_id = None
        pipedrive_create_deals = False
    from packages.integrations.sinks import build_sink_from_env
    sink = build_sink_from_env("hubspot+pipedrive", _S())
    assert hasattr(sink, "sinks") or sink.name in {"hubspot", "pipedrive"}
