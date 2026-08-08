"""Unit tests for SMS + email senders.  All network calls patched."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from packages.integrations.sms_sender import (
    SmsResult, render_confirmation, send_sms,
)
from packages.integrations.email_sender import (
    EmailResult, _build_ics, render_confirmation_html, send_confirmation_email,
)


# ── SMS ────────────────────────────────────────────────────────────────

def test_render_confirmation_includes_opt_out():
    body = render_confirmation("Smile Dental", "Cleaning", "Sat Nov 22 at 2pm")
    assert "STOP" in body
    assert "Smile Dental" in body
    assert "Cleaning" in body


def test_render_confirmation_with_provider_and_ref():
    body = render_confirmation(
        "Smile Dental", "Cleaning", "Sat 2pm",
        provider="Rosa Delgado", confirmation_id="ABC123",
    )
    assert "Rosa Delgado" in body
    assert "ABC123" in body


@pytest.mark.asyncio
async def test_send_sms_missing_creds(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    result = await send_sms("+14155551234", "hi")
    assert not result.ok
    assert "unset" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_send_sms_invalid_recipient(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+14175743859")
    result = await send_sms("415-555-1234", "hi")
    assert not result.ok
    assert "e.164" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_send_sms_rejects_too_long_body(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+14175743859")
    result = await send_sms("+14155551234", "x" * 2000)
    assert not result.ok


# ── Email ──────────────────────────────────────────────────────────────

def test_build_ics_has_required_fields():
    from datetime import datetime
    ics = _build_ics(
        "Smile Dental", "Cleaning",
        datetime(2026, 11, 22, 14, 0), 45,
        location="2847 Maple Ave",
    )
    assert "BEGIN:VCALENDAR" in ics
    assert "BEGIN:VEVENT" in ics
    assert "SUMMARY:Cleaning at Smile Dental" in ics
    assert "LOCATION:2847 Maple Ave" in ics
    assert "END:VEVENT" in ics
    assert "END:VCALENDAR" in ics


def test_render_email_html():
    subject, html = render_confirmation_html(
        "Smile Dental", "Cleaning", "Sat Nov 22 at 2pm",
        provider="Rosa Delgado", address="2847 Maple Ave",
        phone="+1 972 555 0192", confirmation_id="ABC123",
        prep_notes="Please arrive 15 min early",
    )
    assert "Booked" in subject
    assert "Smile Dental" in subject
    assert "Rosa Delgado" in html
    assert "2847 Maple Ave" in html
    assert "ABC123" in html
    assert "15 min early" in html
    assert ".ics" in html


@pytest.mark.asyncio
async def test_send_email_invalid_address():
    r = await send_confirmation_email("not-an-email", "subject", "<p>body</p>")
    assert not r.ok
    assert "invalid email" in (r.error or "").lower()


@pytest.mark.asyncio
async def test_send_email_no_provider_configured(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    r = await send_confirmation_email("user@example.com", "s", "<p>b</p>")
    assert not r.ok
    assert "no email provider" in (r.error or "").lower()
