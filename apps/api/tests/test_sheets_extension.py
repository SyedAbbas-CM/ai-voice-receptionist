"""Tests for the outbound-facing Sheets methods (list_rows, update_by_row,
update_by_match). Uses a fake Google Sheets service — no network calls."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from packages.integrations.google_sheets import GoogleSheets


def _fake_sheets(data: dict[str, list[list[str]]]):
    """Build a mock resembling googleapiclient's Sheets service.

    `data` maps A1-ish range strings ('tab!A:Z', 'tab!1:1', 'tab!C5', ...)
    to 2D arrays. Reads look up the range; writes stored in `writes`."""
    writes: list[dict] = []

    def _get(spreadsheetId=None, range=None):  # noqa: N803
        # Try exact then loose match
        payload = data.get(range) or data.get(range.split("!")[-1])
        if payload is None and "!" in range:
            # Match by tab regardless of range spec
            tab = range.split("!")[0]
            for k, v in data.items():
                if k.startswith(tab + "!"):
                    payload = v
                    break
        return MagicMock(execute=lambda: {"values": payload or []})

    def _batch_update(spreadsheetId=None, body=None):  # noqa: N803
        writes.append(body)
        total = sum(
            len(entry.get("values", [[]])[0])
            for entry in (body or {}).get("data", [])
        )
        return MagicMock(execute=lambda: {"totalUpdatedCells": total})

    values_mock = MagicMock()
    values_mock.get = MagicMock(side_effect=_get)
    values_mock.batchUpdate = MagicMock(side_effect=_batch_update)
    values_mock.append = MagicMock(return_value=MagicMock(execute=lambda: {"updates": {"updatedCells": 1}}))
    values_mock.update = MagicMock(return_value=MagicMock(execute=lambda: {"updatedCells": 1}))

    spreadsheets_mock = MagicMock()
    spreadsheets_mock.values = MagicMock(return_value=values_mock)

    service = MagicMock()
    service.spreadsheets = MagicMock(return_value=spreadsheets_mock)
    return service, writes


@pytest.fixture
def sheets():
    gs = GoogleSheets(service_account_json_path="/tmp/fake.json", sheet_id="S1", tab="Sheet1")
    service, writes = _fake_sheets({
        "Sheet1!A:Z": [
            ["Name", "Phone", "Property address", "Rent Amount", "Total Calls", "Status", "Last Called", "Notes"],
            ["Bob Owner", "5551234567", "123 Elm St", "1500", "0", "", "", ""],
            ["Alice Owner", "5559999999", "456 Oak Ave", "1800", "2", "", "2026-07-08T14:00:00+00:00", ""],
            ["Carol Owner", "5557777777", "789 Pine Rd", "2100", "3", "HOT_LEAD", "2026-07-06T10:00:00+00:00", ""],
        ],
        "Sheet1!1:1": [["Name", "Phone", "Property address", "Rent Amount", "Total Calls", "Status", "Last Called", "Notes"]],
    })
    gs._service = service  # bypass auth
    return gs, writes


def test_list_rows_returns_row_numbers(sheets):
    gs, _ = sheets
    rows = gs.list_rows()
    assert len(rows) == 3
    assert rows[0]["Name"] == "Bob Owner"
    assert rows[0]["_row_number"] == 2  # first data row is row 2
    assert rows[1]["_row_number"] == 3
    assert rows[2]["_row_number"] == 4


def test_col_letter_conversion(sheets):
    gs, _ = sheets
    assert gs._col_letter(0) == "A"
    assert gs._col_letter(25) == "Z"
    assert gs._col_letter(26) == "AA"
    assert gs._col_letter(27) == "AB"


def test_update_by_row_updates_correct_columns(sheets):
    gs, writes = sheets
    result = gs.update_by_row(3, {"Status": "COLD_LEAD", "Notes": "not interested"})
    assert result["updated"] == 2
    assert result["unknown_headers"] == []

    assert len(writes) == 1
    entries = {d["range"]: d["values"][0][0] for d in writes[0]["data"]}
    assert entries["Sheet1!F3"] == "COLD_LEAD"  # column F = 6th column = index 5 = Status
    assert entries["Sheet1!H3"] == "not interested"  # column H = Notes


def test_update_by_row_reports_unknown_headers(sheets):
    gs, writes = sheets
    result = gs.update_by_row(2, {"Status": "COLD_LEAD", "Bogus": "x"})
    assert result["updated"] == 1
    assert result["unknown_headers"] == ["Bogus"]


def test_update_by_row_rejects_row_one(sheets):
    gs, _ = sheets
    with pytest.raises(ValueError, match="row_number must be >= 2"):
        gs.update_by_row(1, {"Status": "x"})


def test_update_by_match_finds_and_updates(sheets):
    gs, writes = sheets
    result = gs.update_by_match(
        match_column="Property address",
        match_value="456 Oak Ave",
        fields={"Status": "HOT_LEAD", "Rent Amount": "1900"},
    )
    assert result["matched"] is True
    assert result["row_number"] == 3
    entries = {d["range"]: d["values"][0][0] for d in writes[0]["data"]}
    assert entries["Sheet1!F3"] == "HOT_LEAD"
    assert entries["Sheet1!D3"] == "1900"  # column D = Rent Amount


def test_update_by_match_returns_no_match(sheets):
    gs, writes = sheets
    result = gs.update_by_match(
        match_column="Phone",
        match_value="9999999999",  # not in the sheet
        fields={"Status": "x"},
    )
    assert result["matched"] is False
    assert result["row_number"] is None
    assert writes == []  # no writes attempted
