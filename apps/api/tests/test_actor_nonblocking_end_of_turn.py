"""Sprint 12 Track A: end-of-turn handler returns fast (< 500 ms) even
though the brain job takes seconds.  The brain runs off the mailbox
so an interruption emitted during the brain job actually gets
dispatched instead of queueing behind it."""
from __future__ import annotations

import asyncio
import json

import pytest


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FakeVAD:
    def is_speech(self, f, sr, mime): return len(f) > 0


class FakeSTT:
    name = "fake"
    supports_streaming = True
    async def transcribe(self, w, sr, mime): return ""
    async def transcribe_stream(self, chunks, sample_rate=8000, encoding="linear16"):
        async for _ in chunks: pass
        return
        yield  # pragma: no cover


class FakeTTS:
    name = "fake"
    async def synthesize(self, text, voice=None):
        return b"\xff" * 4000, "audio/mulaw"


@pytest.mark.asyncio
async def test_end_of_turn_handler_returns_fast_when_brain_slow(monkeypatch):
    """The mailbox handler for END_OF_TURN spawns the brain and
    returns.  Even if the brain takes 2 seconds, subsequent mailbox
    events (like a probe) get dispatched within ~500 ms."""
    from app.routes import twilio as twilio_module
    from app.core import session_manager
    from app import providers
    from app.routes import twilio_actor as actor_module

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: FakeTTS())
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)

    slow_brain_started = asyncio.Event()

    async def slow_run_greeting(state, brain):
        return "Hello."

    async def slow_run_user_turn(state, brain, transcript):
        slow_brain_started.set()
        await asyncio.sleep(2.0)
        return {"reply": "Slow reply.", "escalated": False, "tool_results": []}

    async def _end(sid, tenant_id="default"): return None
    monkeypatch.setattr(session_manager, "run_greeting", slow_run_greeting)
    monkeypatch.setattr(session_manager, "run_user_turn", slow_run_user_turn)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    from packages.runtime import call_actor, CallEvent, EventSource, CallState
    call_actor._registry_singleton = None

    from app.routes.twilio_actor import TwilioActorSession
    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ-slow", call_id="CA-slow", tenant_id="acme",
    )
    await session.start()
    for _ in range(200):
        await asyncio.sleep(0.02)
        if session.actor.state == CallState.LISTENING:
            break
    assert session.actor.state == CallState.LISTENING

    await session.actor.emit(CallEvent.new(
        call_id="CA-slow", tenant_id="acme", source=EventSource.CONTROL,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="end_of_turn",
        payload={"text": "book me an appointment", "is_final": True},
    ))
    # 2026-08-05: brain spawn now gated by fragment-merge window
    # (_FRAGMENT_MERGE_WINDOW_MS = 2500ms).  Give it 4s to fire.
    try:
        await asyncio.wait_for(slow_brain_started.wait(), timeout=4.0)
    except asyncio.TimeoutError:
        pytest.fail("brain never started")

    # Brain is now sleeping 2s.  If Track A works, the mailbox is free.
    dispatched = asyncio.Event()
    async def probe(actor, event):
        dispatched.set()
        return True
    session.actor.handlers[(EventSource.CONTROL, "probe")] = probe
    await session.actor.emit(CallEvent.new(
        call_id="CA-slow", tenant_id="acme", source=EventSource.CONTROL,
        turn_generation=session.actor.turn_generation,
        speech_generation=session.actor.speech_generation,
        kind="probe", payload={},
    ))
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        pytest.fail("mailbox was blocked by brain — probe event never dispatched")
    await session.stop("test")
