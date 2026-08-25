"""FollowupSink tests — SMS to caller + email to owner on booking.

Monkeypatches sms_sender.send_sms + email_sender.send_confirmation_email
so we don't hit Twilio or SendGrid in tests.  Verifies:
- Fires on successful booking with all args (caller phone + owner email).
- Skips when booking failed / no phone / no when_human.
- Never raises when downstream helpers fail.
- Toggles honor `send_sms_to_caller` + `send_email_to_owner`.
"""
from __future__ import annotations

import pytest

from packages.integrations.sinks import FollowupSink
from packages.schemas import (
    CallState, CallStatus, ExtractedFields, Intent, Urgency,
)


# ── recording helpers ────────────────────────────────────────────


class _SmsRecorder:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._ok = ok

    async def __call__(self, to: str, body: str):
        self.calls.append((to, body))
        # SmsResult shape from sms_sender.
        from packages.integrations.sms_sender import SmsResult
        return SmsResult(ok=self._ok, sid="SM123" if self._ok else None,
                          error=None if self._ok else "twilio 402 no credit")


class _EmailRecorder:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[dict] = []
        self._ok = ok

    async def __call__(self, to, subject, html_body, ics_content=None):
        self.calls.append({
            "to": to, "subject": subject, "html_body": html_body,
            "ics_content": ics_content,
        })
        from packages.integrations.email_sender import EmailResult
        return EmailResult(ok=self._ok, provider="sendgrid",
                            error=None if self._ok else "smtp 500")


def _state() -> CallState:
    s = CallState(session_id="sess-1", business_id="biz-1")
    s.extracted = ExtractedFields(
        caller_name="Sarah Chen",
        phone="+15551234567",
        intent=Intent.BOOK_APPOINTMENT,
        urgency=Urgency.MEDIUM,
        lead_score=80,
        summary="Booked cleaning",
    )
    s.status = CallStatus.COMPLETED
    return s


def _booking(**overrides) -> dict:
    args = {
        "caller_name": "Sarah Chen",
        "phone": "+15551234567",
        "service": "cleaning",
        "start_iso": "2026-08-26T14:30:00",
        **overrides.get("arguments", {}),
    }
    result = {"booked": True, **overrides.get("result", {})}
    return {"name": "book_appointment", "arguments": args,
            "result": result, "error": None}


# ── happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_followup_fires_sms_and_email_on_booking(monkeypatch):
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink(
        business_name="Smile Dental",
        owner_email="owner@smiledental.com",
        location="123 Main St",
    )
    await sink.on_booking(_state(), _booking())

    assert len(sms.calls) == 1
    to, body = sms.calls[0]
    assert to == "+15551234567"
    assert "Smile Dental" in body
    assert "cleaning" in body

    assert len(email.calls) == 1
    e = email.calls[0]
    assert e["to"] == "owner@smiledental.com"
    assert "New booking" in e["subject"]
    assert "Sarah Chen" in e["subject"]
    assert "cleaning" in e["subject"]
    assert e["ics_content"] is not None
    assert "BEGIN:VCALENDAR" in e["ics_content"]


# ── skip conditions ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_followup_skips_when_booking_failed(monkeypatch):
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("Smile Dental", "owner@x.com")
    booking = _booking(result={"booked": False, "reason": "slot taken"})
    await sink.on_booking(_state(), booking)
    assert sms.calls == []
    assert email.calls == []


@pytest.mark.asyncio
async def test_followup_skips_sms_when_no_phone(monkeypatch):
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("Smile Dental", "owner@x.com")
    booking = _booking(arguments={"phone": None})
    # Also strip extracted phone.
    state = _state()
    state.extracted.phone = None
    await sink.on_booking(state, booking)
    assert sms.calls == []
    # Email to owner still fires (owner email doesn't depend on caller phone).
    assert len(email.calls) == 1


@pytest.mark.asyncio
async def test_followup_toggles_disable_both_channels(monkeypatch):
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink(
        business_name="X", owner_email="o@x.com",
        send_sms_to_caller=False, send_email_to_owner=False,
    )
    await sink.on_booking(_state(), _booking())
    assert sms.calls == []
    assert email.calls == []


# ── failure isolation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_followup_never_raises_when_sms_helper_raises(monkeypatch):
    async def _raiser(to, body):
        raise RuntimeError("twilio unreachable")
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", _raiser)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("X", "o@x.com")
    # Must not raise.
    await sink.on_booking(_state(), _booking())
    # Email path still runs even though SMS raised.
    assert len(email.calls) == 1


@pytest.mark.asyncio
async def test_followup_never_raises_when_email_helper_raises(monkeypatch):
    sms = _SmsRecorder()
    async def _raiser(**kwargs):
        raise RuntimeError("sendgrid 500")
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", _raiser,
    )
    sink = FollowupSink("X", "o@x.com")
    await sink.on_booking(_state(), _booking())
    # SMS still fired.
    assert len(sms.calls) == 1


# ── on_call_end is a no-op (documented) ─────────────────────────


@pytest.mark.asyncio
async def test_followup_on_call_end_noop():
    sink = FollowupSink("X", "o@x.com")
    # Should not raise, no side effects expected.
    await sink.on_call_end(_state())


# ── when-human formatter ────────────────────────────────────────


def test_when_human_from_start_iso():
    args = {"start_iso": "2026-08-26T14:30:00"}
    out = FollowupSink._when_human(args)
    assert out is not None
    assert "August" in out
    assert "26" in out
    assert "2:30 PM" in out or "02:30 PM" in out


def test_when_human_falls_back_to_date_and_time():
    assert FollowupSink._when_human({"date": "Tuesday", "time": "2:30 PM"}) == "Tuesday at 2:30 PM"
    assert FollowupSink._when_human({"date": "Tuesday"}) == "Tuesday"
    assert FollowupSink._when_human({}) is None


# ── security: caller-controlled input escaped in email HTML + subject ─
#
# 2026-08-25 security review flagged that caller_name / caller_phone
# come from tool arguments populated by the LLM from CALLER SPEECH.
# Interpolating them raw into HTML → stored XSS in owner's inbox.
# Interpolating into a Subject line → email header injection via \r\n.


@pytest.mark.asyncio
async def test_followup_escapes_caller_name_in_email_html(monkeypatch):
    """Caller says a name containing HTML tags → email must render the
    tags as visible text, NOT as active HTML."""
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("Smile Dental", "owner@x.com")
    booking = _booking(arguments={
        "caller_name": "<script>alert('xss')</script>",
    })
    await sink.on_booking(_state(), booking)
    assert len(email.calls) == 1
    body = email.calls[0]["html_body"]
    # Escaped form should appear.
    assert "&lt;script&gt;" in body
    # Raw tag must NOT appear (stored XSS would fire on render).
    assert "<script>alert('xss')</script>" not in body


@pytest.mark.asyncio
async def test_followup_strips_control_chars_from_subject(monkeypatch):
    """Caller name containing \\r\\n must NOT reach the Subject header —
    email header injection risk (attacker adds Bcc: header)."""
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("Smile Dental", "owner@x.com")
    booking = _booking(arguments={
        "caller_name": "Alice\r\nBcc: attacker@evil.com\r\n",
    })
    await sink.on_booking(_state(), booking)
    assert len(email.calls) == 1
    subject = email.calls[0]["subject"]
    # No CR/LF in the subject line — those are what enable header injection.
    assert "\r" not in subject
    assert "\n" not in subject
    # Also must not leak the injected header text as literal content
    # (a Bcc header MTA-side would be a leak; here we ONLY assert the
    # control chars are gone — the "Bcc:" substring is fine as visible
    # text in the subject since it can't act as a header without \r\n).


@pytest.mark.asyncio
async def test_followup_escapes_caller_phone_in_email_html(monkeypatch):
    """Phone field goes through the same interpolation — also escape."""
    sms = _SmsRecorder()
    email = _EmailRecorder()
    monkeypatch.setattr("packages.integrations.sms_sender.send_sms", sms)
    monkeypatch.setattr(
        "packages.integrations.email_sender.send_confirmation_email", email,
    )
    sink = FollowupSink("Smile Dental", "owner@x.com")
    booking = _booking(arguments={
        "phone": "<img src=x onerror=alert(1)>",
    })
    await sink.on_booking(_state(), booking)
    assert len(email.calls) == 1
    body = email.calls[0]["html_body"]
    assert "<img src=x onerror" not in body
    assert "&lt;img" in body
