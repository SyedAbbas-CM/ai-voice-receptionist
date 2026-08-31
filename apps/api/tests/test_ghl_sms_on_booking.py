"""GHL SMS-on-booking (Wave 1) — regression tests.

Verify:
1. When send_sms_on_booking=True and booking succeeds, send_sms is called
2. When send_sms_on_booking=False (default), no SMS goes out
3. Custom template substitutes {first_name} / {business_name} / {service} / {when}
4. Fallback default template is used when no custom template set
5. send_sms failure never crashes the sink (must not raise)
6. Body is capped at 320 chars (2 SMS segments)
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.integrations.sinks import GHLSink
from packages.schemas.business import BusinessProfile
from packages.schemas.call import CallState, TranscriptTurn, TurnRole


def _make_state(business: BusinessProfile) -> CallState:
    s = CallState(session_id="test-x", tenant_id="clinic", business_id=business.id, business=business)
    # Minimal extracted so on_booking doesn't NPE on state.extracted.intent
    from packages.schemas.call import ExtractedFields, Intent
    s.extracted = ExtractedFields(
        intent=Intent.BOOK_APPOINTMENT,
        phone="+15551234567",
        caller_name="Alice Smith",
        summary="booked a cleaning",
    )
    return s


def _make_booking(start_iso: str = "2026-09-01T09:30:00", service: str = "Adult cleaning") -> dict:
    return {
        "arguments": {
            "phone": "+15551234567",
            "caller_name": "Alice Smith",
            "service": service,
        },
        "result": {
            "booked": True,
            "event": {"start": start_iso, "id": "evt_abc"},
        },
    }


def _mock_client(sms_should_raise: bool = False) -> MagicMock:
    """Client that returns a canned contact upsert and records send_sms."""
    c = MagicMock()
    c.default_calendar_id = None  # skip book_appointment path
    c.upsert_contact = AsyncMock(return_value={"id": "contact_xyz"})
    c.add_note = AsyncMock(return_value={})

    if sms_should_raise:
        c.send_sms = AsyncMock(side_effect=RuntimeError("GHL down"))
    else:
        c.send_sms = AsyncMock(return_value={"messageId": "m_1"})
    return c


# ─── 1. Default: no SMS fires ─────────────────────────────────────────────


def test_default_no_sms_fired():
    biz = BusinessProfile(id="x", name="Smile Dental")  # send_sms_on_booking defaults False
    sink = GHLSink(client=_mock_client(), business=biz)
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))
    sink.client.send_sms.assert_not_called()


# ─── 2. Opt-in: SMS fires with sane default template ─────────────────────


def test_opt_in_sms_fires_default_template():
    biz = BusinessProfile(id="x", name="Smile Dental", send_sms_on_booking=True)
    sink = GHLSink(client=_mock_client(), business=biz)
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))
    sink.client.send_sms.assert_called_once()
    call_args = sink.client.send_sms.call_args
    contact_id = call_args.args[0]
    body = call_args.args[1]
    assert contact_id == "contact_xyz"
    # Default template mentions the caller's first name + business + service
    assert "Alice" in body
    assert "Smile Dental" in body
    assert "Adult cleaning" in body
    # And formats the time as a readable weekday
    assert any(day in body for day in
               ("Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"))


# ─── 3. Custom template with all vars ────────────────────────────────────


def test_custom_template_substitutes_all_vars():
    biz = BusinessProfile(
        id="x", name="Smile Dental",
        send_sms_on_booking=True,
        sms_confirmation_template="Hey {first_name}! {business_name} confirming {service} on {when}. -A",
    )
    sink = GHLSink(client=_mock_client(), business=biz)
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))
    body = sink.client.send_sms.call_args.args[1]
    assert body.startswith("Hey Alice!")
    assert "Smile Dental" in body
    assert "Adult cleaning" in body
    assert body.endswith(". -A") or "-A" in body


# ─── 4. send_sms failure does not crash the sink ─────────────────────────


def test_sms_failure_does_not_raise():
    biz = BusinessProfile(id="x", name="Smile Dental", send_sms_on_booking=True)
    sink = GHLSink(client=_mock_client(sms_should_raise=True))
    # Must complete without raising
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))


# ─── 5. Body capped at 320 chars ─────────────────────────────────────────


def test_body_capped_at_320():
    long_biz_name = "The Very Long Dental Practice Name " * 20  # 700 chars
    biz = BusinessProfile(
        id="x", name=long_biz_name,
        send_sms_on_booking=True,
    )
    sink = GHLSink(client=_mock_client(), business=biz)
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))
    body = sink.client.send_sms.call_args.args[1]
    assert len(body) <= 320


# ─── 6. Booking that didn't succeed → no SMS ─────────────────────────────


def test_failed_booking_no_sms():
    biz = BusinessProfile(id="x", name="Smile Dental", send_sms_on_booking=True)
    sink = GHLSink(client=_mock_client(), business=biz)
    booking = _make_booking()
    booking["result"]["booked"] = False
    asyncio.run(sink.on_booking(_make_state(biz), booking))
    sink.client.send_sms.assert_not_called()


# ─── 7. Missing phone → no SMS ───────────────────────────────────────────


def test_missing_phone_no_sms():
    biz = BusinessProfile(id="x", name="Smile Dental", send_sms_on_booking=True)
    sink = GHLSink(client=_mock_client(), business=biz)
    state = _make_state(biz)
    state.extracted.phone = None
    booking = _make_booking()
    booking["arguments"]["phone"] = None
    asyncio.run(sink.on_booking(state, booking))
    sink.client.send_sms.assert_not_called()


# ─── 8. Env-var fallback: GHL_SMS_ON_BOOKING=true, biz field False ──────


def test_env_var_fallback(monkeypatch):
    monkeypatch.setenv("GHL_SMS_ON_BOOKING", "true")
    biz = BusinessProfile(id="x", name="Smile Dental")  # field default False
    # BusinessProfile field defaults False AND is set explicitly False by
    # the model — so this test verifies the env falls through only when
    # biz.send_sms_on_booking is False. Per our impl, biz overrides env.
    sink = GHLSink(client=_mock_client(), business=biz)
    asyncio.run(sink.on_booking(_make_state(biz), _make_booking()))
    # BusinessProfile default False → biz wins → NO sms
    sink.client.send_sms.assert_not_called()
    # Now set biz true — SMS fires
    biz2 = BusinessProfile(id="x", name="Smile Dental", send_sms_on_booking=True)
    sink2 = GHLSink(client=_mock_client(), business=biz2)
    asyncio.run(sink2.on_booking(_make_state(biz2), _make_booking()))
    sink2.client.send_sms.assert_called_once()

    # Env only (no business) also fires SMS — enables global toggle
    sink3 = GHLSink(client=_mock_client(), business=None)
    asyncio.run(sink3.on_booking(_make_state(biz), _make_booking()))
    sink3.client.send_sms.assert_called_once()
