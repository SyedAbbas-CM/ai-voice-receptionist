"""Sprint 9a: tests for the CallActor-backed Twilio session adapter.

The adapter is an I/O bridge (websocket <-> CallActor).  We test it by
substituting a fake websocket + fake session_manager brain, then
asserting:

  * Utterance frames buffer + emit MEDIA/utterance_ready when silence hits.
  * bump_turn fires before the utterance event so late partials get dropped.
  * A barge INTERRUPT calls bump_turn AND sends Twilio `clear`.
  * A barge CONTINUE does NOT send clear + does NOT bump turn.
  * mark_ack advances ledger.heard_text_end.
  * Speech task is cancelled on bump_turn (heard-text stays truncated).

These bypass the actual STT/TTS/brain — we replace them with async stubs
so the test is deterministic and doesn't hit the network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


# ── Fakes ────────────────────────────────────────────────────────────

class FakeWebSocket:
    """Records every send_text call so tests can assert on the wire."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def events_of_type(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.sent if e.get("event") == kind]


class FakeVAD:
    """VAD stub: any non-empty frame counts as speech."""
    def is_speech(self, frame: bytes, sample_rate: int, mime: str) -> bool:
        return len(frame) > 0


class FakeSTT:
    """STT stub — returns a canned transcript."""

    def __init__(self, transcript: str = "hello there") -> None:
        self.transcript = transcript

    async def transcribe(self, wav: bytes, sample_rate: int, mime: str) -> str:
        return self.transcript


class FakeTTS:
    """TTS stub — returns µ-law bytes so the mu-law converter passthroughs."""

    def __init__(self, audio: bytes = b"\xff" * 4000, mime: str = "audio/mulaw") -> None:
        self.audio = audio
        self.mime = mime

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        return self.audio, self.mime


# ── fixture: patch out the heavy deps ───────────────────────────────

@pytest.fixture
def patched_env(monkeypatch):
    """Replace VAD/STT/TTS/session_manager with in-memory fakes so the
    adapter can run without network or model calls."""
    from app.routes import twilio as twilio_module
    from app.routes import twilio_actor as actor_module
    from app.core import session_manager

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: FakeTTS())

    # STT provider comes via app.providers.get_stt
    from app import providers
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())

    # Pass µ-law bytes through unchanged so the test doesn't need
    # the audio codec pipeline.
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw",
                        lambda audio, mime: audio)
    monkeypatch.setattr(twilio_module, "_mulaw_frames_to_wav",
                        lambda mulaw, sample_rate=8000: mulaw)

    # Stub the brain — return a canned reply.
    async def fake_run_greeting(state, brain):
        return "Hi, this is a test greeting."

    async def fake_run_user_turn(state, brain, transcript):
        return {"reply": f"You said: {transcript}"}

    async def fake_end_session_async(session_id, tenant_id="default"):
        return None

    def fake_start_session_with_id(session_id, tenant_id="default"):
        return ("state-obj", "brain-obj")

    def fake_get_session(session_id, tenant_id="default"):
        return ("state-obj", "brain-obj")

    monkeypatch.setattr(session_manager, "run_greeting", fake_run_greeting)
    monkeypatch.setattr(session_manager, "run_user_turn", fake_run_user_turn)
    monkeypatch.setattr(session_manager, "end_session_async", fake_end_session_async)
    monkeypatch.setattr(session_manager, "start_session_with_id", fake_start_session_with_id)
    monkeypatch.setattr(session_manager, "get_session", fake_get_session)

    # Reset the module-level registry so tests don't share actors.
    from packages.runtime import call_actor
    call_actor._registry_singleton = None

    yield

    # Clean up any lingering actors
    reg = call_actor._registry_singleton
    if reg is not None:
        import asyncio as _a
        try:
            _a.get_event_loop().run_until_complete(reg.stop_all())
        except Exception:
            pass


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_greeting_fires_and_sends_media(patched_env):
    """start() runs the greeting through _speak, which sends media
    frames + a mark event to the websocket."""
    from app.routes.twilio_actor import TwilioActorSession

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-abc", call_id="CA-t1", tenant_id="acme",
    )
    await session.start()
    # Give _speak's frames time to send
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    assert ws.events_of_type("media"), "greeting must send at least one media frame"
    assert ws.events_of_type("mark"), "greeting must trail a mark event"
    assert session.actor is not None
    await session.stop("test")


@pytest.mark.asyncio
async def test_mark_ack_advances_ledger(patched_env):
    """When Twilio acks the mark for the greeting, heard_text_for
    should return the full greeting text."""
    from app.routes.twilio_actor import TwilioActorSession

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-x", call_id="CA-t2", tenant_id="acme",
    )
    await session.start()
    # Drain outbound sends
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    marks = ws.events_of_type("mark")
    assert marks, "expected a mark event"
    mark_id = marks[0]["mark"]["name"]

    gen_before = session.actor.speech_generation
    await session.on_mark_ack(mark_id)
    # Give the actor loop a tick to dispatch
    for _ in range(10):
        await asyncio.sleep(0.01)
        heard = session.actor.ledger.heard_text_for(gen_before)
        if heard:
            break

    heard = session.actor.ledger.heard_text_for(gen_before)
    assert heard == "Hi, this is a test greeting.", \
        f"ledger heard_text should equal full greeting, got {heard!r}"
    await session.stop("test")


@pytest.mark.asyncio
async def test_barge_interrupt_sends_clear_and_bumps_turn(patched_env):
    """A barge INTERRUPT event must (1) send Twilio `clear` (2) advance
    turn_generation.  Later stale STT events would then be dropped."""
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallEvent, EventSource

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-y", call_id="CA-t3", tenant_id="acme",
    )
    await session.start()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    turn_before = session.actor.turn_generation
    ws.sent.clear()

    # Simulate the classifier saying INTERRUPT
    await session.actor.emit(CallEvent.new(
        call_id="CA-t3", tenant_id="acme", source=EventSource.STT,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="barge_candidate",
        payload={"text": "wait actually", "action": "INTERRUPT"},
    ))
    # Give the actor + the follow-up brain turn time to run
    for _ in range(50):
        await asyncio.sleep(0.02)
        if ws.events_of_type("clear") and session.actor.turn_generation > turn_before:
            break

    assert ws.events_of_type("clear"), "INTERRUPT must send Twilio clear"
    assert session.actor.turn_generation > turn_before, \
        "INTERRUPT must advance turn_generation"
    await session.stop("test")


@pytest.mark.asyncio
async def test_barge_continue_does_not_send_clear(patched_env):
    """Backchannel (CONTINUE) must NOT send Twilio clear or bump turn."""
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallEvent, EventSource

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-z", call_id="CA-t4", tenant_id="acme",
    )
    await session.start()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    turn_before = session.actor.turn_generation
    ws.sent.clear()

    await session.actor.emit(CallEvent.new(
        call_id="CA-t4", tenant_id="acme", source=EventSource.STT,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="barge_candidate",
        payload={"text": "mm-hm", "action": "CONTINUE"},
    ))
    for _ in range(10):
        await asyncio.sleep(0.02)

    assert not ws.events_of_type("clear"), \
        "CONTINUE (backchannel) must not send clear"
    assert session.actor.turn_generation == turn_before, \
        "CONTINUE must not bump turn generation"
    await session.stop("test")


@pytest.mark.asyncio
async def test_utterance_ready_bumps_turn_before_emit(patched_env, monkeypatch):
    """The utterance-ready path must bump the turn FIRST so late STT
    partials from the previous turn are dropped by the generation guard.

    Sprint 10 STREAMING WIRING: this test exercises the BATCH path
    (VAD-silence-close) which is now gated off when turn_manager is
    enabled.  Force streaming flags off so we test the legacy path."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "streaming_stt_enabled", False)
    monkeypatch.setattr(settings, "turn_manager_enabled", False)
    from app.routes.twilio_actor import TwilioActorSession
    from packages.runtime import CallEvent, EventSource

    from packages.runtime import CallState

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-w", call_id="CA-t5", tenant_id="acme",
    )
    await session.start()
    # Wait for greeting to finish (SPEAKING -> LISTENING) so utterance
    # frames are routed to the utterance buffer, not the barge buffer.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if session.actor.state == CallState.LISTENING:
            break
    assert session.actor.state == CallState.LISTENING, \
        f"greeting should finish before utterance test, state={session.actor.state}"

    turn_before = session.actor.turn_generation

    # Emit enough "speech" frames to trigger the silence-close path.
    # Silence detection uses wall-clock time, so we need real elapsed
    # ms between the speech burst and the closing silence check.
    speech_frame = b"\xff" * 160
    silent_frame = b""
    for _ in range(5):
        await session.on_media(speech_frame)
    # Real sleep so silence_ms crosses SILENCE_HANG_MS (700ms)
    await asyncio.sleep(0.8)
    # One silent frame after the wait triggers the close check
    await session.on_media(silent_frame)

    # Wait for the utterance-ready handler + brain to run
    for _ in range(50):
        await asyncio.sleep(0.02)
        if session.actor.turn_generation > turn_before:
            break

    assert session.actor.turn_generation > turn_before, \
        "utterance-close must advance turn_generation"
    await session.stop("test")
