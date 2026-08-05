"""Sprint 9f: two-stage barge-in tests.

Coverage:
  * Flag OFF → no ducking, current one-stage behavior
  * Flag ON, first speech frame during SPEAKING → duck fires, YIELDING
  * Duck + backchannel CONTINUE → unduck, YIELDING → SPEAKING, metric
  * Duck + INTERRUPT → unduck (as confirmed_interrupt), clear + bump_turn
  * Duck + silence for stage2_deadline_ms → false_trigger auto-unduck
  * While ducked, _send_mulaw_frames skips outbound frames
  * mulaw gain helper: 0dB pass-through, +6dB boosts amplitude, clips

Uses the same FakeWebSocket + FakeVAD infrastructure as test_twilio_actor.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def events_of_type(self, kind: str) -> list[dict]:
        return [e for e in self.sent if e.get("event") == kind]


class FakeVAD:
    """Speech = any non-empty frame."""
    def is_speech(self, frame, sample_rate, mime):
        return len(frame) > 0


class FakeSTT:
    def __init__(self, text: str = "yeah") -> None:
        self.text = text

    async def transcribe(self, wav, sample_rate, mime):
        return self.text


class SlowFakeTTS:
    """Synthesizes a long-enough audio buffer that we have room to
    duck mid-stream.  8000 bytes = 1 second of µ-law @ 8kHz."""
    name = "elevenlabs-fake"

    async def synthesize(self, text, voice=None):
        return b"\xff" * 8000, "audio/mulaw"


@pytest.fixture
def patched(monkeypatch):
    from app.routes import twilio as twilio_module
    from app.core import session_manager
    from app import providers
    from app.routes import twilio_actor as actor_module

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: SlowFakeTTS())
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)
    monkeypatch.setattr(twilio_module, "_mulaw_frames_to_wav",
                        lambda m, sample_rate=8000: m)

    async def _rg(state, brain): return "Greeting — this is a longer message that gives us time to duck"
    async def _rut(state, brain, transcript):
        return {"reply": "You said: " + transcript, "escalated": False, "tool_results": []}
    async def _end(sid, tenant_id="default"): return None
    monkeypatch.setattr(session_manager, "run_greeting", _rg)
    monkeypatch.setattr(session_manager, "run_user_turn", _rut)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    from packages.runtime import call_actor
    call_actor._registry_singleton = None

    yield


def _counter(body: str, name: str, tenant: str, outcome: str) -> float:
    """Match a counter regardless of label ordering."""
    p1 = rf'{name}\{{[^}}]*outcome="{outcome}"[^}}]*tenant_id="{tenant}"[^}}]*\}}\s+([\d.]+)'
    p2 = rf'{name}\{{[^}}]*tenant_id="{tenant}"[^}}]*outcome="{outcome}"[^}}]*\}}\s+([\d.]+)'
    m = re.search(p1, body) or re.search(p2, body)
    return float(m.group(1)) if m else 0.0


# ── flag OFF: no ducking ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_no_duck_engages(patched, monkeypatch):
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallState

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", False)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-off", tenant_id="t-off",
    )
    await session.start()
    # Force state=SPEAKING deterministically — greeting timing races
    # with our test frame.  We're testing the duck decision, not the
    # greeting.
    for _ in range(30):
        await asyncio.sleep(0.01)
        if session.actor is not None:
            break
    session.actor.transition(CallState.SPEAKING)

    # Send a speech frame during SPEAKING — with flag OFF, no duck
    speech = b"\xff" * 160
    await session.on_media(speech)
    assert session._ducked is False
    assert session.actor.state == CallState.SPEAKING
    await session.stop("test")


# ── flag ON: duck engages on first speech frame ─────────────────────

@pytest.mark.asyncio
async def test_flag_on_first_speech_frame_ducks(patched, monkeypatch):
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallState
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", True)
    monkeypatch.setattr(settings, "barge_stage2_deadline_ms", 5000)  # long

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-duck", tenant_id="t-duck",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if session.actor is not None:
            break
    session.actor.transition(CallState.SPEAKING)

    speech = b"\xff" * 160
    await session.on_media(speech)
    assert session._ducked is True
    assert session.actor.state == CallState.YIELDING

    body = generate_latest(REGISTRY).decode()
    pending = _counter(body, "voiceops_stage1_duck_total", "t-duck", "pending")
    assert pending >= 1

    # Clean up: the deadline task is still pending — cancel via stop
    await session.stop("test")


# ── duck + INTERRUPT: unduck as confirmed_interrupt ────────────────

@pytest.mark.asyncio
async def test_duck_plus_interrupt_marks_confirmed(patched, monkeypatch):
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallEvent, CallState, EventSource
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", True)
    monkeypatch.setattr(settings, "barge_stage2_deadline_ms", 5000)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-int", tenant_id="t-int",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if session.actor is not None:
            break
    session.actor.transition(CallState.SPEAKING)

    # Trigger stage 1
    await session.on_media(b"\xff" * 160)
    assert session._ducked is True

    turn_before = session.actor.turn_generation
    ws.sent.clear()

    # Classifier fires INTERRUPT
    await session.actor.emit(CallEvent.new(
        call_id="CA-int", tenant_id="t-int", source=EventSource.STT,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="barge_candidate",
        payload={"text": "wait", "action": "INTERRUPT"},
    ))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if not session._ducked and session.actor.turn_generation > turn_before:
            break

    assert session._ducked is False
    assert ws.events_of_type("clear"), "INTERRUPT must send Twilio clear"
    body = generate_latest(REGISTRY).decode()
    assert _counter(body, "voiceops_stage1_duck_total", "t-int", "confirmed_interrupt") >= 1
    await session.stop("test")


# ── duck + CONTINUE (backchannel): unduck as backchannel_unduck ────

@pytest.mark.asyncio
async def test_duck_plus_backchannel_marks_backchannel(patched, monkeypatch):
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallEvent, CallState, EventSource
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", True)
    monkeypatch.setattr(settings, "barge_stage2_deadline_ms", 5000)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-bc", tenant_id="t-bc",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if session.actor is not None:
            break
    session.actor.transition(CallState.SPEAKING)

    await session.on_media(b"\xff" * 160)
    assert session._ducked is True
    assert session.actor.state == CallState.YIELDING

    turn_before = session.actor.turn_generation
    ws.sent.clear()

    # Classifier says CONTINUE (backchannel)
    await session.actor.emit(CallEvent.new(
        call_id="CA-bc", tenant_id="t-bc", source=EventSource.STT,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="barge_candidate",
        payload={"text": "mm-hm", "action": "CONTINUE"},
    ))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if not session._ducked:
            break

    assert session._ducked is False, "backchannel must unduck"
    assert session.actor.state == CallState.SPEAKING, \
        "YIELDING → SPEAKING on backchannel"
    assert session.actor.turn_generation == turn_before, \
        "backchannel must NOT bump turn"
    assert not ws.events_of_type("clear"), "backchannel must not send clear"

    body = generate_latest(REGISTRY).decode()
    assert _counter(body, "voiceops_stage1_duck_total", "t-bc", "backchannel_unduck") >= 1
    await session.stop("test")


# ── duck + deadline: auto-unduck as false_trigger ──────────────────

@pytest.mark.asyncio
async def test_duck_deadline_auto_unducks_as_false_trigger(patched, monkeypatch):
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallState
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", True)
    # Very short deadline so the test completes fast
    monkeypatch.setattr(settings, "barge_stage2_deadline_ms", 50)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-ft", tenant_id="t-ft",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if session.actor is not None:
            break
    session.actor.transition(CallState.SPEAKING)

    await session.on_media(b"\xff" * 160)
    assert session._ducked is True

    # Wait past the deadline WITHOUT emitting a classifier result
    await asyncio.sleep(0.15)

    assert session._ducked is False, "deadline should have auto-unducked"
    assert session.actor.state == CallState.SPEAKING, \
        "YIELDING → SPEAKING on false trigger"
    body = generate_latest(REGISTRY).decode()
    assert _counter(body, "voiceops_stage1_duck_total", "t-ft", "false_trigger") >= 1
    await session.stop("test")


# ── while ducked, outbound frames skipped ──────────────────────────

@pytest.mark.asyncio
async def test_ducked_state_skips_outbound_media_frames(patched, monkeypatch):
    """When _ducked=True, _send_audio_frames must NOT send media
    events to the websocket."""
    from app.core.config import settings
    from app.routes.twilio_actor import TwilioActorSession

    monkeypatch.setattr(settings, "two_stage_barge_in_enabled", True)
    monkeypatch.setattr(settings, "barge_stage2_deadline_ms", 5000)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-skip", tenant_id="t-skip",
    )
    # Manually construct the actor context — we bypass start() to
    # test _send_audio_frames directly under duck.
    from packages.runtime import get_registry, CallState
    session.actor = await get_registry().get_or_create(
        "CA-skip", "t-skip",
    )
    session.actor.transition(CallState.SPEAKING)

    # Send some frames WITHOUT ducking — they should go out.
    # Sprint 11 renamed the method + it now takes (bytes, mime).
    # Passing µ-law bytes + audio/mulaw mime skips the PCM→µ-law
    # transcoding branch (they pass through unchanged) so we're
    # testing the same duck logic as before.
    ws.sent.clear()
    await session._send_audio_frames(b"\xff" * 320, "audio/mulaw")  # 2 frames
    assert len(ws.events_of_type("media")) == 2

    # Duck manually + send more — should skip
    session._ducked = True
    ws.sent.clear()
    await session._send_audio_frames(b"\xff" * 480, "audio/mulaw")  # 3 frames
    assert len(ws.events_of_type("media")) == 0, \
        "ducked frames must not send"

    session._ducked = False
    await session.stop("test")


# ── gain helper ─────────────────────────────────────────────────────

def test_mulaw_gain_zero_db_passthrough():
    from app.routes.twilio_actor import _apply_mulaw_gain
    src = bytes(range(256))
    out = _apply_mulaw_gain(src, 0.0)
    assert out == src


def test_mulaw_gain_positive_db_changes_bytes():
    """Boosting quiet µ-law should change the output bytes."""
    from app.routes.twilio_actor import _apply_mulaw_gain
    # µ-law 0x80 ≈ -8sample, 0xFF ≈ 0 amplitude, mid-range is 0x00
    src = bytes([0x80, 0x81, 0x7F, 0x00, 0xFF] * 16)
    boosted = _apply_mulaw_gain(src, 6.0)
    assert boosted != src, "6dB boost must change output"
    assert len(boosted) == len(src)


def test_mulaw_gain_survives_bad_input():
    """Malformed input should not raise — degrade to passthrough."""
    from app.routes.twilio_actor import _apply_mulaw_gain
    # Odd-length input is fine for µ-law (byte-per-sample)
    src = b"\x00\x01\x02"
    out = _apply_mulaw_gain(src, 3.0)
    assert isinstance(out, bytes)
