"""Email sender: SendGrid (preferred) or plain SMTP fallback.

Fires from booking flow when caller provides an email address.  Includes
an ICS attachment so the caller can add the appointment to their calendar
in one tap.

Env (SendGrid, preferred — 100/day free):
    SENDGRID_API_KEY
    SENDGRID_FROM_EMAIL       (verified sender)
    SENDGRID_FROM_NAME        (display name, optional)

Env (SMTP fallback — for tenants who want their own domain):
    SMTP_HOST
    SMTP_PORT                 (default 587)
    SMTP_USER
    SMTP_PASSWORD
    SMTP_FROM_EMAIL
    SMTP_FROM_NAME
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timedelta
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError

log = logging.getLogger(__name__)


@dataclass
class EmailResult:
    ok: bool
    provider: Optional[str] = None
    error: Optional[str] = None


def _build_ics(
    business_name: str,
    service: str,
    start: datetime,
    duration_minutes: int,
    location: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Minimal RFC 5545 iCalendar VEVENT the caller can double-click."""
    end = start + timedelta(minutes=duration_minutes)
    fmt = lambda dt: dt.strftime("%Y%m%dT%H%M%S")
    uid = f"{int(start.timestamp())}-{business_name.replace(' ', '')}@voiceops"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//voiceops//receptionist//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{fmt(datetime.utcnow())}Z",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SUMMARY:{service} at {business_name}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if description:
        # Escape newlines per iCal spec
        lines.append(f"DESCRIPTION:{description.replace(chr(10), chr(92) + 'n')}")
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def render_confirmation_html(
    business_name: str,
    service: str,
    when_human: str,
    provider: Optional[str] = None,
    address: Optional[str] = None,
    phone: Optional[str] = None,
    prep_notes: Optional[str] = None,
    confirmation_id: Optional[str] = None,
) -> tuple[str, str]:
    """Return (subject, html_body)."""
    subject = f"Booked: {service} at {business_name}"
    parts = [
        f"<p>Hi,</p>",
        f"<p>Your appointment is booked:</p>",
        "<ul>",
        f"<li><b>Service:</b> {service}</li>",
        f"<li><b>When:</b> {when_human}</li>",
    ]
    if provider:
        parts.append(f"<li><b>Provider:</b> {provider}</li>")
    if address:
        parts.append(f"<li><b>Address:</b> {address}</li>")
    if phone:
        parts.append(f"<li><b>Phone:</b> {phone}</li>")
    if confirmation_id:
        parts.append(f"<li><b>Confirmation:</b> {confirmation_id}</li>")
    parts.append("</ul>")
    if prep_notes:
        parts.append(f"<p><b>Please note:</b> {prep_notes}</p>")
    parts.append(
        "<p>The attached .ics file will add this to your calendar. "
        "If you need to reschedule or cancel, just call us back.</p>"
    )
    parts.append(f"<p>Thanks,<br>{business_name}</p>")
    return subject, "\n".join(parts)


async def send_confirmation_email(
    to: str,
    subject: str,
    html_body: str,
    ics_content: Optional[str] = None,
) -> EmailResult:
    """Try SendGrid first, fall back to SMTP.  Both async-wrapped so they
    don't block the actor's event loop."""
    if not to or "@" not in to:
        return EmailResult(ok=False, error=f"invalid email: {to!r}")

    sg_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if sg_key:
        return await _send_via_sendgrid(sg_key, to, subject, html_body, ics_content)
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if smtp_host:
        return await _send_via_smtp(to, subject, html_body, ics_content)
    return EmailResult(ok=False, error="no email provider configured (set SENDGRID_API_KEY or SMTP_HOST)")


async def _send_via_sendgrid(
    api_key: str, to: str, subject: str, html: str, ics: Optional[str],
) -> EmailResult:
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "").strip()
    from_name = os.environ.get("SENDGRID_FROM_NAME", "Reception").strip()
    if not from_email:
        return EmailResult(ok=False, error="SENDGRID_FROM_EMAIL unset")
    payload: dict = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    if ics:
        import base64
        payload["attachments"] = [{
            "content": base64.b64encode(ics.encode()).decode(),
            "type": "text/calendar",
            "filename": "appointment.ics",
        }]
    req = Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=15))
        log.info("SendGrid sent to=%s status=%s", to, resp.status)
        return EmailResult(ok=True, provider="sendgrid")
    except HTTPError as e:
        body_snip = ""
        try:
            body_snip = e.read().decode()[:200]
        except Exception:
            pass
        log.warning("SendGrid send failed to=%s http=%d body=%r", to, e.code, body_snip)
        return EmailResult(ok=False, provider="sendgrid", error=f"HTTP {e.code}: {body_snip}")
    except Exception as e:
        log.warning("SendGrid send failed to=%s: %s", to, e)
        return EmailResult(ok=False, provider="sendgrid", error=str(e))


async def _send_via_smtp(
    to: str, subject: str, html: str, ics: Optional[str],
) -> EmailResult:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or 587)
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASSWORD", "").strip()
    from_email = os.environ.get("SMTP_FROM_EMAIL", "").strip() or user
    from_name = os.environ.get("SMTP_FROM_NAME", "Reception").strip()

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to
    msg.attach(MIMEText(html, "html"))
    if ics:
        att = MIMEBase("text", "calendar")
        att.set_payload(ics.encode())
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", 'attachment; filename="appointment.ics"')
        msg.attach(att)

    def _send():
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)
        log.info("SMTP sent to=%s via %s:%d", to, host, port)
        return EmailResult(ok=True, provider="smtp")
    except Exception as e:
        log.warning("SMTP send failed to=%s: %s", to, e)
        return EmailResult(ok=False, provider="smtp", error=str(e))
