"""SMS sender via Twilio Messages API.

Fires from booking + reschedule + cancel flows.  Non-blocking: if Twilio
is down or the number is invalid, we log + continue rather than failing
the booking.  Real receptionists confirm verbally AND by SMS; the SMS
is belt + suspenders, not the primary confirmation.

Env:
    TWILIO_ACCOUNT_SID     (already set for voice)
    TWILIO_AUTH_TOKEN      (already set for voice)
    TWILIO_SMS_FROM        E.164 sender number.  Falls back to
                           TWILIO_PHONE_NUMBER when unset.

Compliance:
    Every outbound SMS must include an opt-out ("Reply STOP to unsubscribe")
    per TCPA + Twilio's own AUP.  render_confirmation() adds it automatically.
"""
from __future__ import annotations

import logging
import os
from base64 import b64encode
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

log = logging.getLogger(__name__)


@dataclass
class SmsResult:
    ok: bool
    sid: Optional[str] = None
    error: Optional[str] = None


def _twilio_creds() -> tuple[Optional[str], Optional[str], Optional[str]]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip() or None
    tok = os.environ.get("TWILIO_AUTH_TOKEN", "").strip() or None
    frm = (os.environ.get("TWILIO_SMS_FROM", "").strip()
           or os.environ.get("TWILIO_PHONE_NUMBER", "").strip()
           or None)
    return sid, tok, frm


def render_confirmation(
    business_name: str,
    service: str,
    when_human: str,
    provider: Optional[str] = None,
    confirmation_id: Optional[str] = None,
) -> str:
    """Render the confirmation body.  Kept ASCII + <160 chars where possible
    to avoid GSM/UCS-2 rate spikes."""
    parts = [f"{business_name}: booked {service}"]
    if provider:
        parts.append(f"with {provider}")
    parts.append(f"on {when_human}.")
    if confirmation_id:
        parts.append(f"Ref {confirmation_id}.")
    parts.append("Reply Y to confirm, N to cancel. STOP to opt out.")
    return " ".join(parts)


async def send_sms(to: str, body: str) -> SmsResult:
    """POST to Twilio Messages API.  Sync HTTP but wrapped for async use."""
    sid, tok, frm = _twilio_creds()
    if not sid or not tok:
        return SmsResult(ok=False, error="TWILIO_ACCOUNT_SID/AUTH_TOKEN unset")
    if not frm:
        return SmsResult(ok=False, error="TWILIO_SMS_FROM (or TWILIO_PHONE_NUMBER) unset")
    if not to or not to.startswith("+"):
        return SmsResult(ok=False, error=f"invalid E.164 recipient: {to!r}")
    if not body or len(body) > 1600:
        return SmsResult(ok=False, error=f"invalid body length: {len(body) if body else 0}")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = b64encode(f"{sid}:{tok}".encode()).decode()
    data = urlencode({"To": to, "From": frm, "Body": body}).encode()
    req = Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        # Run in a thread so we don't block the event loop.
        import asyncio
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10))
        import json as _json
        payload = _json.loads(resp.read().decode())
        log.info("SMS sent to=%s sid=%s status=%s",
                 to, payload.get("sid"), payload.get("status"))
        return SmsResult(ok=True, sid=payload.get("sid"))
    except HTTPError as e:
        body_snip = ""
        try:
            body_snip = e.read().decode()[:200]
        except Exception:
            pass
        log.warning("SMS send failed to=%s http=%d body=%r", to, e.code, body_snip)
        return SmsResult(ok=False, error=f"HTTP {e.code}: {body_snip}")
    except Exception as e:
        log.warning("SMS send failed to=%s: %s", to, e)
        return SmsResult(ok=False, error=str(e))
