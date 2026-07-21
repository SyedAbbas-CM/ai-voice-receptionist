"""Outbound dialer endpoints.

Replaces the SubtoDealz n8n workflow's entire "Google Sheet -> filter ->
loop -> Vapi -> Wait 8min -> classify -> write back" chain with two
routes and one webhook hook:

  POST /outbound/dry_run     — preview which leads would be dialed (no calls)
  POST /outbound/start_batch — fire calls with variable overrides, register
                                context in outbound_registry, return summary.
                                Disposition write-back happens in /vapi/events
                                when the end-of-call-report arrives.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import outbound_registry
from app.core.config import settings
from packages.integrations.dialer_policy import (
    DialerPolicy,
    Lead,
    filter_leads,
    lead_from_sheet_row,
)
from packages.integrations.google_sheets import GoogleSheets
from packages.integrations.vapi_client import VapiClient, VapiError


log = logging.getLogger(__name__)
router = APIRouter(prefix="/outbound", tags=["outbound"])


class BatchRequest(BaseModel):
    """Everything the caller must / can specify to start a batch."""

    transport: str = Field(
        default="vapi",
        description="'vapi' (real phone calls) or 'local' (Qwen3-TTS emulator, no phone).",
    )
    sheet_id: Optional[str] = Field(
        default=None,
        description="Google Sheet id. Defaults to settings.google_sheet_id.",
    )
    sheet_tab: str = "Sheet1"
    business_id: str = Field(
        default="demo-subtodealz-001",
        description="Which business profile drives the assistant / vertical / hours.",
    )
    assistant_id: Optional[str] = Field(default=None, description="Vapi assistantId to dial with. Defaults to VAPI_ASSISTANT_ID env.")
    phone_number_id: Optional[str] = Field(default=None, description="Vapi phoneNumberId (caller ID). Defaults to VAPI_PHONE_NUMBER_ID env.")

    # Dialer policy overrides
    timezone: str = "America/New_York"
    cooldown_hours: int = 24
    max_attempts: int = 3
    business_hours_start: int = 9
    business_hours_end: int = 18
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    dnc_numbers: list[str] = Field(default_factory=list)

    # Column mapping (SubtoDealz used specific names — allow overrides)
    phone_column: str = "Phone"
    name_column: str = "Name"
    address_column: str = "Property address"
    rent_column: str = "Rent Amount"

    max_calls_per_batch: int = 25


class LeadPreview(BaseModel):
    name: str
    phone: str
    address: str
    rent: str
    row_number: int
    can_call: bool
    reason: str
    detail: str = ""


class BatchPreview(BaseModel):
    total_rows: int
    dialable: list[LeadPreview]
    skipped: list[LeadPreview]


class DispatchOutcome(BaseModel):
    row_number: int
    phone: str
    ok: bool
    vapi_call_id: Optional[str] = None
    error: Optional[str] = None


class BatchResult(BaseModel):
    started_at: str
    dispatched: list[DispatchOutcome]
    skipped: list[LeadPreview]
    total: int


def _build_policy(req: BatchRequest) -> DialerPolicy:
    from datetime import time
    return DialerPolicy(
        timezone=req.timezone,
        weekdays=tuple(req.weekdays),
        business_hours=((time(req.business_hours_start, 0), time(req.business_hours_end, 0)),),
        cooldown_hours=req.cooldown_hours,
        max_attempts=req.max_attempts,
        dnc_numbers=frozenset(req.dnc_numbers),
    )


def _sheet_client(req: BatchRequest) -> GoogleSheets:
    sa_json = settings.google_service_account_json
    sheet_id = req.sheet_id or settings.google_sheet_id
    if not sa_json:
        raise HTTPException(500, "GOOGLE_SERVICE_ACCOUNT_JSON not configured")
    if not sheet_id:
        raise HTTPException(400, "sheet_id not provided and GOOGLE_SHEET_ID not configured")
    return GoogleSheets(service_account_json_path=sa_json, sheet_id=sheet_id, tab=req.sheet_tab)


def _preview_row(row: dict, req: BatchRequest, can_call: bool, reason: str, detail: str) -> LeadPreview:
    return LeadPreview(
        name=str(row.get(req.name_column, "") or ""),
        phone=str(row.get(req.phone_column, "") or ""),
        address=str(row.get(req.address_column, "") or ""),
        rent=str(row.get(req.rent_column, "") or ""),
        row_number=int(row.get("_row_number", 0)),
        can_call=can_call,
        reason=reason,
        detail=detail,
    )


def _load_leads(sheets: GoogleSheets, req: BatchRequest) -> tuple[list[dict], list[Lead]]:
    """Return (raw_rows, normalized_leads)."""
    rows = sheets.list_rows(tab=req.sheet_tab)
    leads = []
    for row in rows:
        aliased = dict(row)
        # dialer_policy.lead_from_sheet_row expects known column names; if the
        # sheet uses different ones, alias them into the expected keys.
        if req.phone_column != "Phone":
            aliased["Phone"] = row.get(req.phone_column, "")
        if req.name_column != "Name":
            aliased["Name"] = row.get(req.name_column, "")
        leads.append(lead_from_sheet_row(aliased))
    return rows, leads


@router.post("/dry_run", response_model=BatchPreview)
async def dry_run(req: BatchRequest) -> BatchPreview:
    """Preview which leads WOULD be dialed. Zero API calls to Vapi."""
    sheets = _sheet_client(req)
    rows, leads = _load_leads(sheets, req)
    policy = _build_policy(req)

    from packages.integrations.dialer_policy import decide_can_call

    dialable, skipped = [], []
    for row, lead in zip(rows, leads):
        decision = decide_can_call(lead, policy)
        preview = _preview_row(row, req, decision.can_call, decision.reason, decision.detail)
        (dialable if decision.can_call else skipped).append(preview)

    dialable = dialable[: req.max_calls_per_batch]
    return BatchPreview(total_rows=len(rows), dialable=dialable, skipped=skipped)


def _load_business_profile(business_id: str) -> "object":
    """Load the BusinessProfile JSON keyed by id, or fall back to the env default."""
    import json
    from pathlib import Path

    from packages.schemas import BusinessProfile

    # Convention: sample-data/<slug>/business.json where slug is derived from the id
    # (e.g. demo-subtodealz-001 -> subtodealz)
    repo_root = Path(__file__).resolve().parents[4]
    candidates = [
        Path(settings.business_profile_path),
        repo_root / "sample-data" / "subtodealz" / "business.json",
        repo_root / "sample-data" / "clinic" / "business.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            data = json.loads(candidate.read_text())
            if data.get("id") == business_id or candidate == candidates[0]:
                return BusinessProfile(**data)
    # Last-resort fallback
    return BusinessProfile(**json.loads(candidates[-1].read_text()))


@router.post("/start_batch", response_model=BatchResult)
async def start_batch(req: BatchRequest) -> BatchResult:
    """Actually dial. Non-blocking.

    - transport=vapi: POST /call/phone to Vapi; end-of-call arrives at /vapi/events
    - transport=local: LocalVoiceOrchestrator runs the conversation locally with
      Qwen3-TTS; posts the same event shape to /vapi/events when done.

    Either way, the disposition + Google Sheet writeback path is identical."""
    transport = (req.transport or "vapi").lower()
    if transport not in {"vapi", "local"}:
        raise HTTPException(400, f"unknown transport {transport!r}; use 'vapi' or 'local'")

    sheets = _sheet_client(req)
    rows, leads = _load_leads(sheets, req)
    policy = _build_policy(req)

    from packages.integrations.dialer_policy import decide_can_call

    dispatched: list[DispatchOutcome] = []
    skipped: list[LeadPreview] = []

    dial_candidates: list[tuple[dict, Lead]] = []
    for row, lead in zip(rows, leads):
        decision = decide_can_call(lead, policy)
        if not decision.can_call:
            skipped.append(_preview_row(row, req, False, decision.reason, decision.detail))
        elif len(dial_candidates) < req.max_calls_per_batch:
            dial_candidates.append((row, lead))

    # Prepare the transport-specific client
    if transport == "vapi":
        api_key = settings.vapi_private_key
        assistant_id = req.assistant_id or settings.vapi_assistant_id
        phone_number_id = req.phone_number_id or settings.vapi_phone_number_id
        if not api_key:
            raise HTTPException(500, "VAPI_PRIVATE_KEY not configured (or set transport=local)")
        if not assistant_id or not phone_number_id:
            raise HTTPException(
                400,
                "assistant_id and phone_number_id are required for vapi transport (env or body)",
            )
        client = VapiClient(api_key=api_key)
        # local placeholder ids for logging / observability
        assistant_id_for_log = assistant_id
        phone_number_id_for_log = phone_number_id
    else:
        from packages.integrations.local_voice_orchestrator import LocalVoiceOrchestrator
        business = _load_business_profile(req.business_id)
        client = LocalVoiceOrchestrator(business=business)
        assistant_id_for_log = "local-emulator"
        phone_number_id_for_log = "local-caller-id"

    started_at = datetime.utcnow().isoformat() + "Z"

    for row, lead in dial_candidates:
        variable_values = {
            "lead_name": str(row.get(req.name_column, "") or "there").split()[0],
            "property_address": str(row.get(req.address_column, "") or ""),
            "rent_amount": str(row.get(req.rent_column, "") or ""),
        }
        try:
            result = await client.dispatch_call(
                assistant_id=assistant_id_for_log,
                phone_number_id=phone_number_id_for_log,
                customer_number=lead.phone,
                variable_values=variable_values,
            )
        except VapiError as e:
            log.warning("vapi dispatch failed for row %s: %s", row.get("_row_number"), e)
            dispatched.append(DispatchOutcome(
                row_number=int(row.get("_row_number", 0)),
                phone=lead.phone, ok=False, error=str(e),
            ))
            continue
        except Exception as e:
            log.exception("local dispatch failed for row %s: %s", row.get("_row_number"), e)
            dispatched.append(DispatchOutcome(
                row_number=int(row.get("_row_number", 0)),
                phone=lead.phone, ok=False, error=str(e),
            ))
            continue

        # Register outbound context so /vapi/events (or local end-of-call) can find it
        outbound_registry.remember(outbound_registry.OutboundContext(
            session_id=f"vapi_{result.id}",
            business_id=req.business_id,
            lead=row,
            sheet_id=req.sheet_id or settings.google_sheet_id,
            sheet_tab=req.sheet_tab,
        ))

        dispatched.append(DispatchOutcome(
            row_number=int(row.get("_row_number", 0)),
            phone=lead.phone,
            ok=True,
            vapi_call_id=result.id,
        ))

    return BatchResult(
        started_at=started_at,
        dispatched=dispatched,
        skipped=skipped,
        total=len(dispatched) + len(skipped),
    )
