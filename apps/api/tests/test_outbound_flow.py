"""End-to-end tests for the SubtoDealz-replacement outbound flow.

Covers:
- disposition_handler.process_end_of_call reads outbound_registry, runs
  extractor + classifier, writes back to Sheets
- outbound_registry stores and pops correctly
- vapi_client.dispatch_call sends the right payload shape

Uses a scripted LLM + mock Sheets service — no external calls."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.core import outbound_registry
from app.core.disposition_handler import process_end_of_call
from app.providers.base import LLMResponse


class ScriptedLLM:
    """Duck-typed LLMProvider for disposition_handler."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.name = "scripted"

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls.append(messages)
        return LLMResponse(text=self.responses.pop(0) if self.responses else "")


def _fake_sheets_service():
    """Reads a stub 8-column sheet, records writes."""
    writes: list[dict] = []
    header_row = [["Name", "Phone", "Property address", "Rent Amount", "Total Calls", "Status", "Last Called", "Notes"]]

    def _get(spreadsheetId=None, range=None):  # noqa: N803
        # We only need the 1:1 header lookup for update_by_row
        if "1:1" in range or range.endswith("!1:1"):
            return MagicMock(execute=lambda: {"values": header_row})
        return MagicMock(execute=lambda: {"values": header_row + [
            ["Bob Owner", "5551234567", "123 Elm St", "1500", "1", "", "", ""],
        ]})

    def _batch_update(spreadsheetId=None, body=None):  # noqa: N803
        writes.append(body)
        total = sum(len(e.get("values", [[]])[0]) for e in (body or {}).get("data", []))
        return MagicMock(execute=lambda: {"totalUpdatedCells": total})

    values_mock = MagicMock()
    values_mock.get = MagicMock(side_effect=_get)
    values_mock.batchUpdate = MagicMock(side_effect=_batch_update)

    spreadsheets_mock = MagicMock()
    spreadsheets_mock.values = MagicMock(return_value=values_mock)

    service = MagicMock()
    service.spreadsheets = MagicMock(return_value=spreadsheets_mock)
    return service, writes


# ---------------- outbound_registry ----------------

def test_registry_remember_pop_roundtrip():
    ctx = outbound_registry.OutboundContext(
        session_id="vapi_test_1",
        business_id="demo-subtodealz-001",
        lead={"Name": "Test", "_row_number": 5},
    )
    outbound_registry.remember(ctx)
    assert outbound_registry.get("vapi_test_1") is ctx
    popped = outbound_registry.pop("vapi_test_1")
    assert popped is ctx
    assert outbound_registry.get("vapi_test_1") is None


def test_registry_returns_none_for_unknown():
    assert outbound_registry.pop("vapi_unknown_abc") is None


# ---------------- disposition_handler ----------------

@pytest.mark.asyncio
async def test_disposition_no_op_when_not_outbound(monkeypatch):
    """If the call ID isn't in the outbound registry, the handler is a no-op."""
    # Ensure registry is clean
    outbound_registry.pop("vapi_untracked")
    result = await process_end_of_call({"call": {"id": "untracked"}})
    assert result == {"ok": True, "reason": "not_outbound"}


@pytest.mark.asyncio
async def test_disposition_full_happy_path(monkeypatch):
    """Outbound call ends with a transcript -> extractor runs -> classifier
    runs -> sheet write happens with the right fields."""
    # Register an outbound context
    ctx = outbound_registry.OutboundContext(
        session_id="vapi_happy_1",
        business_id="demo-subtodealz-001",
        lead={
            "Name": "Bob Owner",
            "Phone": "5551234567",
            "Property address": "123 Elm St",
            "Rent Amount": "1500",
            "Total Calls": "1",
            "_row_number": 2,
        },
        sheet_id="STUB_SHEET",
        sheet_tab="Sheet1",
    )
    outbound_registry.remember(ctx)

    # Wire in the fake LLM: extractor first (JSON), then classifier (single word)
    extractor_json = json.dumps({
        "rent_updated": True,
        "new_rent_amount": 1800,
        "rent_difference": 300,
        "summary_note": "Owner said rent went up to $1800; asked to be called back next week",
        "property_confirmed_available": True,
        "callback_requested_time": "next week",
    })
    scripted = ScriptedLLM([extractor_json, "CALLBACK_REQUESTED"])
    monkeypatch.setattr("app.core.disposition_handler.get_llm", lambda: scripted)

    # Point settings at a fake service-account path so the handler doesn't skip
    monkeypatch.setattr(
        "app.core.disposition_handler.settings",
        MagicMock(google_service_account_json="/tmp/fake.json"),
    )

    # Patch GoogleSheets constructor to use the mock service
    fake_service, writes = _fake_sheets_service()
    original_init = _patched_sheets_init(fake_service)
    monkeypatch.setattr(
        "app.core.disposition_handler.GoogleSheets.__init__", original_init,
    )

    msg = {
        "call": {"id": "happy_1"},
        "transcript": "AI: Hi, is $1500 still right?  User: Actually it's $1800 now — can you call me back next week?",
        "endedReason": "hangup",
    }
    result = await process_end_of_call(msg)

    assert result["ok"] is True
    assert result["status"] == "CALLBACK_REQUESTED"
    assert result["rent_updated"] is True
    assert result["new_rent_amount"] == 1800
    assert result.get("written", 0) > 0  # sheet write happened

    # Verify the actual fields written include the classification + new rent
    all_entries = {}
    for w in writes:
        for entry in w.get("data", []):
            all_entries[entry["range"]] = entry["values"][0][0]
    values_written = list(all_entries.values())
    assert "CALLBACK_REQUESTED" in values_written
    assert 1800 in values_written  # new rent
    assert 2 in values_written or "2" in values_written  # Total Calls incremented from 1 -> 2


@pytest.mark.asyncio
async def test_disposition_empty_transcript_short_circuits_to_no_answer(monkeypatch):
    ctx = outbound_registry.OutboundContext(
        session_id="vapi_empty_1",
        business_id="demo-subtodealz-001",
        lead={
            "Name": "Alice", "Phone": "5559999999",
            "Rent Amount": "1200", "Total Calls": "0",
            "_row_number": 3,
        },
        sheet_id="STUB_SHEET",
        sheet_tab="Sheet1",
    )
    outbound_registry.remember(ctx)

    # LLM will not be called (short-circuit on empty transcript)
    scripted = ScriptedLLM([])
    monkeypatch.setattr("app.core.disposition_handler.get_llm", lambda: scripted)
    monkeypatch.setattr(
        "app.core.disposition_handler.settings",
        MagicMock(google_service_account_json="/tmp/fake.json"),
    )
    fake_service, writes = _fake_sheets_service()
    monkeypatch.setattr(
        "app.core.disposition_handler.GoogleSheets.__init__", _patched_sheets_init(fake_service),
    )

    result = await process_end_of_call({
        "call": {"id": "empty_1"},
        "transcript": "",
        "endedReason": "no-answer",
    })

    assert result["ok"] is True
    assert result["status"] == "NO_ANSWER"
    assert scripted.calls == []  # zero LLM spend


def _patched_sheets_init(fake_service):
    def _init(self, service_account_json_path, sheet_id, tab="calls"):
        self.service_account_json_path = service_account_json_path
        self.sheet_id = sheet_id
        self.tab = tab
        from threading import Lock
        self._service = fake_service
        self._lock = Lock()
        self._headers_ensured = True
    return _init


# ---------------- vapi_client dispatch shape ----------------

@pytest.mark.asyncio
async def test_vapi_dispatch_call_sends_correct_shape(monkeypatch):
    """Verify the outbound POST body matches Vapi's expected schema."""
    from packages.integrations.vapi_client import VapiClient

    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "vapi_call_new_1", "status": "queued"}

        text = ""

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("packages.integrations.vapi_client.httpx.AsyncClient", FakeAsyncClient)

    client = VapiClient(api_key="pk_fake")
    result = await client.dispatch_call(
        assistant_id="asst_1",
        phone_number_id="pn_1",
        customer_number="+15551234567",
        variable_values={"lead_name": "Bob", "property_address": "123 Elm"},
    )

    assert result.id == "vapi_call_new_1"
    assert captured["url"] == "https://api.vapi.ai/call/phone"
    assert captured["headers"]["Authorization"] == "Bearer pk_fake"

    body = captured["json"]
    assert body["assistantId"] == "asst_1"
    assert body["phoneNumberId"] == "pn_1"
    assert body["customer"] == {"number": "+15551234567"}
    assert body["assistantOverrides"]["variableValues"] == {"lead_name": "Bob", "property_address": "123 Elm"}
