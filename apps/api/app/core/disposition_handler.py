"""End-of-call disposition handler for outbound Vapi calls.

Called from apps/api/app/routes/vapi.py when an `end-of-call-report`
webhook arrives. If the call ID matches a known outbound context, run
the transcript extractor + lead classifier and write results back to
the source Google Sheet.

Kept out of routes/vapi.py so the webhook handler stays thin and this
module can be unit-tested in isolation."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.core import outbound_registry
from app.core.config import settings
from app.providers import get_llm
from packages.core_agent.classifiers import classify_lead, extract_transcript_signals
from packages.integrations.google_sheets import GoogleSheets


log = logging.getLogger(__name__)


async def process_end_of_call(vapi_message: dict) -> dict:
    """Given a Vapi end-of-call-report `message` body, run disposition.

    Returns a summary dict for logging / debugging. Never raises — errors
    are logged so the webhook still returns 200 and Vapi doesn't retry."""
    call = vapi_message.get("call") or {}
    call_id = call.get("id")
    if not call_id:
        return {"ok": False, "reason": "no call.id"}

    session_id = f"vapi_{call_id}"
    ctx = outbound_registry.pop(session_id)
    if ctx is None:
        # Not an outbound call — nothing to disposition
        return {"ok": True, "reason": "not_outbound"}

    transcript = (
        vapi_message.get("transcript")
        or (vapi_message.get("artifact") or {}).get("transcript")
        or ""
    )
    ended_reason = vapi_message.get("endedReason") or call.get("endedReason")

    old_rent = _parse_rent(ctx.lead.get("Rent Amount") or ctx.lead.get("rent_amount"))

    llm = get_llm()
    signals = await extract_transcript_signals(llm, transcript, old_rent_amount=old_rent)
    status = await classify_lead(llm, transcript, ended_reason=ended_reason)

    # Build the sheet update
    now_iso = datetime.utcnow().isoformat() + "Z"
    old_total = _parse_int(ctx.lead.get("Total Calls") or ctx.lead.get("Total calls") or 0)
    fields: dict[str, str | int] = {
        "Status": status.value,
        "lead_status": status.value,
        "Last Called": now_iso,
        "Total Calls": old_total + 1,
        "Notes": signals.summary_note or "",
        "Timestamp": now_iso,
    }
    if signals.rent_updated and signals.new_rent_amount is not None:
        fields["Rent Amount"] = signals.new_rent_amount

    outcome: dict = {
        "ok": True,
        "session_id": session_id,
        "status": status.value,
        "rent_updated": signals.rent_updated,
        "new_rent_amount": signals.new_rent_amount,
    }

    if ctx.sheet_id and settings.google_service_account_json:
        try:
            sheets = GoogleSheets(
                service_account_json_path=settings.google_service_account_json,
                sheet_id=ctx.sheet_id,
                tab=ctx.sheet_tab or "Sheet1",
            )
            row_number = int(ctx.lead.get("_row_number", 0))
            if row_number >= 2:
                write = sheets.update_by_row(row_number, fields)
                outcome["written"] = write.get("updated", 0)
            else:
                # Fall back to matching by phone
                phone = ctx.lead.get("Phone") or ctx.lead.get("phone")
                if phone:
                    write = sheets.update_by_match("Phone", str(phone), fields)
                    outcome["written"] = write.get("updated", 0)
        except Exception as e:  # never break Vapi ack path
            log.exception("disposition sheet write failed: %s", e)
            outcome["write_error"] = str(e)
    else:
        outcome["written"] = 0
        outcome["write_error"] = "sheet write skipped — no sheet_id or GOOGLE_SERVICE_ACCOUNT_JSON"

    return outcome


def _parse_rent(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        return int(digits) if digits else None
    except (TypeError, ValueError):
        return None


def _parse_int(v) -> int:
    try:
        return int(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0
