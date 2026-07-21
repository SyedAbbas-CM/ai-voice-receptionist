"""Google Sheets adapter — used as CRM sink (append_call) AND as lead source
(list_rows / update_by_match) for outbound dialing.

Deps:
    pip install google-api-python-client google-auth
"""
from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Iterable, Optional


HEADERS = [
    "timestamp", "session_id", "caller_name", "phone", "intent",
    "service", "preferred_date", "preferred_time", "urgency",
    "lead_score", "status", "escalated", "summary",
]


class GoogleSheets:
    def __init__(self, service_account_json_path: str, sheet_id: str, tab: str = "calls") -> None:
        self.service_account_json_path = service_account_json_path
        self.sheet_id = sheet_id
        self.tab = tab
        self._service = None
        self._lock = Lock()
        self._headers_ensured = False

    def _svc(self):
        if self._service is None:
            with self._lock:
                if self._service is None:
                    from google.oauth2 import service_account
                    from googleapiclient.discovery import build
                    creds = service_account.Credentials.from_service_account_file(
                        self.service_account_json_path,
                        scopes=["https://www.googleapis.com/auth/spreadsheets"],
                    )
                    self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _ensure_headers(self) -> None:
        if self._headers_ensured:
            return
        with self._lock:
            if self._headers_ensured:
                return
            res = self._svc().spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"{self.tab}!A1:M1",
            ).execute()
            row = (res.get("values") or [[]])[0]
            if row != HEADERS:
                self._svc().spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.tab}!A1",
                    valueInputOption="RAW",
                    body={"values": [HEADERS]},
                ).execute()
            self._headers_ensured = True

    def append_call(
        self,
        session_id: str,
        extracted: dict,
        status: str,
        escalated: bool,
        when: Optional[datetime] = None,
    ) -> dict:
        self._ensure_headers()
        row = [
            (when or datetime.utcnow()).isoformat(),
            session_id,
            extracted.get("caller_name") or "",
            extracted.get("phone") or "",
            extracted.get("intent") or "",
            extracted.get("service") or "",
            extracted.get("preferred_date") or "",
            extracted.get("preferred_time") or "",
            extracted.get("urgency") or "",
            extracted.get("lead_score") or 0,
            status,
            "yes" if escalated else "no",
            extracted.get("summary") or "",
        ]
        return self._svc().spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{self.tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    # ------------------------------------------------------------------
    # Generic sheet read/write — used by outbound dialer to read leads and
    # write back dispositions. Column names come from the sheet's header
    # row (row 1); tab defaults to the constructor's `tab` but callers can
    # override per method.
    # ------------------------------------------------------------------

    def list_rows(self, tab: Optional[str] = None, range_a1: str = "A:Z") -> list[dict]:
        """Return every row as a dict keyed by the sheet's header row.

        Also adds a `_row_number` field (2-indexed — row 2 is the first
        data row) so callers can round-trip updates via `update_by_row`."""
        target_tab = tab or self.tab
        res = self._svc().spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=f"{target_tab}!{range_a1}",
        ).execute()
        values = res.get("values") or []
        if not values:
            return []
        headers = values[0]
        rows: list[dict] = []
        for i, raw in enumerate(values[1:], start=2):
            row = dict(zip(headers, raw))
            row["_row_number"] = i
            rows.append(row)
        return rows

    def _col_letter(self, index_0based: int) -> str:
        """Convert 0-based column index to A1 column letter (0->A, 25->Z, 26->AA)."""
        letters = ""
        n = index_0based
        while True:
            letters = chr(ord("A") + (n % 26)) + letters
            n = n // 26 - 1
            if n < 0:
                break
        return letters

    def update_by_row(
        self,
        row_number: int,
        fields: dict,
        tab: Optional[str] = None,
    ) -> dict:
        """Update the given fields on a specific row.

        `fields` is a dict of {header_name: new_value}. Headers are read from
        row 1 of the sheet to find each column's letter. Returns a summary
        dict with the ranges that were written."""
        if row_number < 2:
            raise ValueError(f"row_number must be >= 2 (row 1 is headers); got {row_number}")
        target_tab = tab or self.tab

        header_res = self._svc().spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=f"{target_tab}!1:1",
        ).execute()
        headers = (header_res.get("values") or [[]])[0]
        if not headers:
            raise RuntimeError(f"tab {target_tab!r} has no header row")

        header_to_col = {h: i for i, h in enumerate(headers)}
        data_ranges: list[dict] = []
        unknown_headers: list[str] = []
        for key, value in fields.items():
            if key not in header_to_col:
                unknown_headers.append(key)
                continue
            col_letter = self._col_letter(header_to_col[key])
            data_ranges.append({
                "range": f"{target_tab}!{col_letter}{row_number}",
                "values": [[value]],
            })

        if not data_ranges:
            return {"updated": 0, "unknown_headers": unknown_headers}

        result = self._svc().spreadsheets().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data_ranges},
        ).execute()
        return {
            "updated": result.get("totalUpdatedCells", len(data_ranges)),
            "unknown_headers": unknown_headers,
            "raw": result,
        }

    def update_by_match(
        self,
        match_column: str,
        match_value: str,
        fields: dict,
        tab: Optional[str] = None,
    ) -> dict:
        """Find the first row where `match_column == match_value` and update
        `fields` on it. Returns {'matched': bool, 'row_number': int|None, ...}.

        This replaces the four duplicate n8n Sheet-update paths
        (test1/test2/test3/test4) in the SubtoDealz workflow. One method,
        one set of edge cases."""
        rows = self.list_rows(tab=tab)
        target_row: Optional[int] = None
        for row in rows:
            if str(row.get(match_column, "")) == str(match_value):
                target_row = row["_row_number"]
                break
        if target_row is None:
            return {"matched": False, "row_number": None}
        outcome = self.update_by_row(target_row, fields, tab=tab)
        return {"matched": True, "row_number": target_row, **outcome}

    def append_row(self, values: Iterable, tab: Optional[str] = None) -> dict:
        """Append a raw row (list of cell values) to the tab."""
        target_tab = tab or self.tab
        return self._svc().spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{target_tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [list(values)]},
        ).execute()
