"""R7 (task #355): arrival-event log emission tests.

Focus: the log LINES.  If a call dies before reaching the actor, the
only way to diagnose it is by grepping uvicorn logs for the arrival
chain.  These tests pin the log line SHAPE so grep-based tooling
stays stable.
"""
from __future__ import annotations

import logging

import pytest

from packages.observability import arrival_events as arr


def _capture(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [rec.getMessage() for rec in caplog.records
            if rec.name == "telephony.arrival"]


def test_session_keys_are_unique():
    seen = {arr.new_session_key() for _ in range(200)}
    assert len(seen) == 200


def test_session_key_format():
    key = arr.new_session_key()
    parts = key.split("-")
    assert parts[0] == "arr"
    assert parts[1].isdigit()   # epoch_ms
    assert parts[2].isdigit()   # counter


# ── voice webhook ───────────────────────────────────────────────────

def test_voice_webhook_received_emits_expected_line(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.voice_webhook_received(
        "arr-1-1", remote_ip="54.10.20.30", signature_ok=True,
    )
    lines = _capture(caplog)
    assert len(lines) == 1
    assert "ARRIVAL " in lines[0]
    assert "kind=VOICE_WEBHOOK_RECEIVED" in lines[0]
    assert "session_key=arr-1-1" in lines[0]
    assert "remote_ip=54.10.20.30" in lines[0]
    assert "signature_ok=True" in lines[0]


def test_voice_webhook_signature_unknown_renders_as_unknown(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.voice_webhook_received("arr-2-2", signature_ok=None)
    assert "signature_ok=unknown" in _capture(caplog)[0]


# ── twiml render ────────────────────────────────────────────────────

def test_stream_twiml_returned_carries_ws_url_and_size(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.stream_twiml_returned(
        "arr-3-3", ws_url="wss://x.example/twilio/stream", twiml_bytes=142,
    )
    line = _capture(caplog)[0]
    assert "kind=STREAM_TWIML_RETURNED" in line
    # Spaces are escaped to `_` for grep sanity — the URL has none but
    # the code path must still be exercised.
    assert "ws_url=wss://x.example/twilio/stream" in line
    assert "twiml_bytes=142" in line


# ── websocket + start + first media ─────────────────────────────────

def test_websocket_connected_precedes_call_sid(caplog):
    """WEBSOCKET_CONNECTED fires BEFORE we know the CallSid — that's
    the whole point.  It carries session_key only."""
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.websocket_connected("arr-4-4", remote_ip="1.2.3.4")
    line = _capture(caplog)[0]
    assert "kind=WEBSOCKET_CONNECTED" in line
    assert "session_key=arr-4-4" in line
    assert "call_sid=" not in line   # not yet known


def test_twilio_start_emits_link_line(caplog):
    """TWILIO_START is where session_key first meets CallSid.  We
    emit an extra ARRIVAL_LINK line so a `grep CA<sid>` finds both
    the linking line and every subsequent per-call log."""
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.twilio_start(
        "arr-5-5",
        call_sid="CA123abc",
        stream_sid="MZstream",
        from_number="+16502530000",
        to_number="+15551110000",
    )
    lines = _capture(caplog)
    assert len(lines) == 2
    assert "kind=TWILIO_START" in lines[0]
    assert "call_sid=CA123abc" in lines[0]
    assert "from_number=+16502530000" in lines[0]
    # Linking line — grep-friendly plain format.
    assert lines[1] == (
        "ARRIVAL_LINK session_key=arr-5-5 call_sid=CA123abc"
    )


def test_first_media_binds_session_key_and_call_sid(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.first_media(
        "arr-6-6",
        call_sid="CAxyz",
        stream_sid="MZstream",
        frame_bytes=160,
    )
    line = _capture(caplog)[0]
    assert "kind=FIRST_MEDIA" in line
    assert "session_key=arr-6-6" in line
    assert "call_sid=CAxyz" in line
    assert "frame_bytes=160" in line


# ── error paths ─────────────────────────────────────────────────────

def test_arrival_error_emits_warning_level(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.arrival_error("arr-7-7", at_stage="ws_accept", error="handshake_failed")
    warnings = [r for r in caplog.records
                if r.name == "telephony.arrival"
                and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "kind=ARRIVAL_ERROR" in warnings[0].getMessage()
    assert "at_stage=ws_accept" in warnings[0].getMessage()


def test_arrival_error_truncates_long_error_field(caplog):
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    long_err = "x" * 5000
    arr.arrival_error("arr-8-8", at_stage="twiml_render", error=long_err)
    line = _capture(caplog)[0]
    # Should NOT dump 5000 chars into one log line.
    assert "error=xxx" in line
    assert line.count("x") <= 250  # loose upper bound


# ── stability of key order (grep tooling) ───────────────────────────

def test_field_order_is_stable_across_emissions(caplog):
    """Field order after `kind=` and `session_key=` is alphabetical.
    Grep-based dashboards depend on this — do NOT sort by some other
    key without updating the tooling first."""
    caplog.set_level(logging.INFO, logger="telephony.arrival")
    arr.voice_webhook_received("arr-9-9", remote_ip="1.2.3.4", signature_ok=True)
    line = _capture(caplog)[0]
    remote_pos = line.index("remote_ip=")
    sig_pos = line.index("signature_ok=")
    assert remote_pos < sig_pos, "remote_ip must precede signature_ok"
