"""Sprint 9b: observability plane tests.

Two families:
  * telemetry module unit tests — spans, marks, counters, gauges
  * integration — /metrics endpoint returns the expected metric names
"""
from __future__ import annotations

import asyncio
import re

import pytest


def test_turn_span_records_marks_and_elapsed():
    """TurnSpan.mark stores monotonic times; elapsed_ms returns the
    delta between two named marks."""
    from packages.runtime.telemetry import turn_span

    with turn_span(call_id="CA-1", tenant_id="acme",
                   turn_generation=3) as span:
        span.mark("media_in")
        # Real (small) delay so the elapsed calc is non-zero
        import time
        time.sleep(0.01)
        span.mark("stt_final")
        elapsed = span.elapsed_ms("media_in", "stt_final")

    assert elapsed is not None
    assert 5.0 < elapsed < 500.0, f"elapsed_ms was {elapsed}"


def test_turn_span_missing_marks_returns_none():
    """elapsed_ms with an unrecorded mark returns None, not zero — so
    callers can distinguish 'did not measure' from 'measured 0ms'."""
    from packages.runtime.telemetry import turn_span

    with turn_span(call_id="CA-2", tenant_id="acme",
                   turn_generation=1) as span:
        span.mark("media_in")
        assert span.elapsed_ms("media_in", "tts_first_byte") is None


def test_turn_span_first_mark_wins():
    """A repeated mark call (e.g. many STT partials) must not overwrite
    the first timestamp — first-partial latency is what we care about."""
    from packages.runtime.telemetry import turn_span
    import time

    with turn_span(call_id="CA-3", tenant_id="acme",
                   turn_generation=1) as span:
        span.mark("media_in")
        time.sleep(0.005)
        span.mark("stt_first_partial")
        first = span.elapsed_ms("media_in", "stt_first_partial")
        time.sleep(0.020)
        span.mark("stt_first_partial")  # ignored
        second = span.elapsed_ms("media_in", "stt_first_partial")
        assert first == second, "second mark call should not overwrite first"


def test_metrics_endpoint_serves_prometheus_format():
    """/metrics returns text in Prometheus exposition format after the
    module has been imported (registers all our metrics)."""
    # Trigger metric registration
    from packages.runtime import telemetry  # noqa: F401
    from prometheus_client import generate_latest, REGISTRY

    body = generate_latest(REGISTRY).decode()
    # A few of our metric names must appear even before any observation
    assert "voiceops_turn_latency_seconds" in body
    assert "voiceops_barge_in_total" in body
    assert "voiceops_heard_vs_generated_ratio" in body


def test_record_barge_in_increments_counter():
    """record_barge_in bumps the per-tenant counter."""
    from packages.runtime import telemetry
    from prometheus_client import generate_latest, REGISTRY

    telemetry.record_barge_in("test-tenant-barge")
    telemetry.record_barge_in("test-tenant-barge")
    body = generate_latest(REGISTRY).decode()
    match = re.search(
        r'voiceops_barge_in_total\{tenant_id="test-tenant-barge"\}\s+([\d.]+)',
        body,
    )
    assert match, "counter not present in /metrics output"
    assert float(match.group(1)) >= 2.0


def test_record_heard_vs_generated_ratio():
    """The ratio gauge should clamp at 1.0 (no interruption case) and
    reflect partial completion after a barge-in."""
    from packages.runtime import telemetry
    from prometheus_client import generate_latest, REGISTRY

    # Full completion
    telemetry.record_heard_vs_generated(
        tenant_id="test-tenant-ratio", heard_chars=100, generated_chars=100,
    )
    body = generate_latest(REGISTRY).decode()
    m = re.search(
        r'voiceops_heard_vs_generated_ratio\{tenant_id="test-tenant-ratio"\}\s+([\d.]+)',
        body,
    )
    assert m is not None
    assert abs(float(m.group(1)) - 1.0) < 0.01

    # Interruption at 40%
    telemetry.record_heard_vs_generated(
        tenant_id="test-tenant-ratio", heard_chars=40, generated_chars=100,
    )
    body = generate_latest(REGISTRY).decode()
    m = re.search(
        r'voiceops_heard_vs_generated_ratio\{tenant_id="test-tenant-ratio"\}\s+([\d.]+)',
        body,
    )
    assert abs(float(m.group(1)) - 0.4) < 0.01


def test_zero_generated_chars_does_not_divide_by_zero():
    """Defensive: caller with an empty response should not crash the
    metric update."""
    from packages.runtime import telemetry
    # Just needs to not raise
    telemetry.record_heard_vs_generated(
        tenant_id="tenant-zero", heard_chars=0, generated_chars=0,
    )


def test_metrics_endpoint_mounted_on_app():
    """The FastAPI app has /metrics mounted after create_app()."""
    import os
    os.environ.setdefault("METRICS_ENABLED", "true")
    from app.main import create_app
    from starlette.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "voiceops_" in resp.text or "python_" in resp.text


@pytest.mark.asyncio
async def test_actor_barge_interrupt_increments_metrics(monkeypatch):
    """End-to-end: a barge INTERRUPT event through the actor adapter
    must bump voiceops_barge_in_total for the tenant."""
    # Reset the actor registry for isolation
    from packages.runtime import call_actor
    call_actor._registry_singleton = None

    from prometheus_client import generate_latest, REGISTRY
    from packages.runtime import CallEvent, EventSource
    from apps.api.tests.test_twilio_actor import FakeWebSocket, FakeVAD, FakeSTT, FakeTTS
    from app.routes import twilio as twilio_module
    from app.routes import twilio_actor as actor_module
    from app.core import session_manager
    from app import providers

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: FakeTTS())
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)
    monkeypatch.setattr(twilio_module, "_mulaw_frames_to_wav", lambda m, sample_rate=8000: m)

    async def _rg(state, brain):
        return "greeting"
    async def _rut(state, brain, transcript):
        return {"reply": f"echo: {transcript}"}
    async def _end(sid, tenant_id="default"):
        return None
    monkeypatch.setattr(session_manager, "run_greeting", _rg)
    monkeypatch.setattr(session_manager, "run_user_turn", _rut)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    from app.routes.twilio_actor import TwilioActorSession

    ws = FakeWebSocket()
    tenant = "metrics-tenant"
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-metrics",
        tenant_id=tenant,
    )
    await session.start()
    # Wait for greeting to send
    for _ in range(30):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    # Snapshot the barge counter BEFORE
    def _counter(body, name, tenant):
        m = re.search(
            rf'{name}\{{tenant_id="{tenant}"\}}\s+([\d.]+)', body,
        )
        return float(m.group(1)) if m else 0.0

    body_before = generate_latest(REGISTRY).decode()
    before = _counter(body_before, "voiceops_barge_in_total", tenant)

    # Fire INTERRUPT
    await session.actor.emit(CallEvent.new(
        call_id="CA-metrics", tenant_id=tenant, source=EventSource.STT,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="barge_candidate",
        payload={"text": "wait", "action": "INTERRUPT"},
    ))
    for _ in range(30):
        await asyncio.sleep(0.02)
        body_after = generate_latest(REGISTRY).decode()
        if _counter(body_after, "voiceops_barge_in_total", tenant) > before:
            break

    body_after = generate_latest(REGISTRY).decode()
    after = _counter(body_after, "voiceops_barge_in_total", tenant)
    assert after == before + 1, \
        f"barge counter should increment by 1 (before={before}, after={after})"

    await session.stop("test")
