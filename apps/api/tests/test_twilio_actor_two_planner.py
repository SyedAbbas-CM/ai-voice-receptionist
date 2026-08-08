"""Sprint 9e: integration tests for the two-planner path in TwilioActorSession.

Coverage:
  * With two_planner_enabled=True, _vpl_synthesize runs (uses
    synthesize_from_plan) and bumps voiceops_two_planner_hit_total.
  * With two_planner_enabled=False, direct synthesize(text) runs (no
    metric bump).
  * On perf planner error, _vpl_synthesize still ships audio (via
    synthesize_from_plan with default delivery) and bumps the fallback
    counter.
  * Provider without synthesize_from_plan degrades to synthesize(text).
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
    def is_speech(self, frame, sample_rate, mime):
        return len(frame) > 0


class FakeSTT:
    async def transcribe(self, wav, sample_rate, mime):
        return "hello there"


class ElevenLabsLikeTTS:
    """Fake TTS that mirrors the ElevenLabs provider interface AND
    supports synthesize_from_plan (the VPL path).  Records which method
    was called."""

    name = "elevenlabs"
    default_voice = "V-test"
    model = "eleven_turbo_v2_5"
    output_format = "ulaw_8000"

    def __init__(self):
        self.direct_calls = 0
        self.plan_calls = 0
        self.last_plan = None

    async def synthesize(self, text, voice=None):
        self.direct_calls += 1
        return b"\xff" * 4000, "audio/mulaw"

    async def synthesize_from_plan(self, plan, voice=None):
        self.plan_calls += 1
        self.last_plan = plan
        return b"\xff" * 4000, "audio/mulaw"


class CartesiaLikeTTS:
    """Fake TTS with name=cartesia — our _provider_supports_vpl gate
    returns False for anything other than elevenlabs (until the Cartesia
    provider integration lands in Sprint 10)."""
    name = "cartesia"

    def __init__(self):
        self.direct_calls = 0

    async def synthesize(self, text, voice=None):
        self.direct_calls += 1
        return b"\xff" * 4000, "audio/mulaw"


class FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text
        self.tool_calls = []
        self.finish_reason = "stop"
        self.raw = None


class FakePerfLLM:
    def __init__(self, text: str = '{"style":"warm","intensity":0.4}', raise_error=None):
        self._text = text
        self._raise = raise_error
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        if self._raise:
            raise self._raise
        return FakeLLMResponse(self._text)


@pytest.fixture
def patched(monkeypatch):
    from app.routes import twilio as twilio_module
    from app.routes import twilio_actor as actor_module
    from app.core import session_manager
    from app import providers

    monkeypatch.setattr(twilio_module, "_get_vad", lambda: FakeVAD())
    monkeypatch.setattr(providers, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(actor_module, "get_stt", lambda: FakeSTT())
    monkeypatch.setattr(twilio_module, "_tts_bytes_to_mulaw", lambda a, m: a)
    monkeypatch.setattr(twilio_module, "_mulaw_frames_to_wav", lambda m, sample_rate=8000: m)

    async def _rg(state, brain):
        return "Hi there, thanks for calling."

    async def _rut(state, brain, transcript):
        return {"reply": "You said: " + transcript, "escalated": False,
                "tool_results": []}

    async def _end(sid, tenant_id="default"):
        return None

    monkeypatch.setattr(session_manager, "run_greeting", _rg)
    monkeypatch.setattr(session_manager, "run_user_turn", _rut)
    monkeypatch.setattr(session_manager, "end_session_async", _end)
    monkeypatch.setattr(session_manager, "start_session_with_id",
                        lambda sid, tenant_id="default": ("s", "b"))
    monkeypatch.setattr(session_manager, "get_session",
                        lambda sid, tenant_id="default": ("s", "b"))

    # Reset actor registry
    from packages.runtime import call_actor
    call_actor._registry_singleton = None

    yield


def _counter(body: str, name: str, tenant: str, hit: str) -> float:
    """Prometheus emits labels alphabetically — hit comes before tenant_id.
    Search for the counter line matching both labels in either order."""
    pattern = (
        rf'{name}\{{[^}}]*hit="{hit}"[^}}]*tenant_id="{tenant}"[^}}]*\}}\s+([\d.]+)'
        rf'|{name}\{{[^}}]*tenant_id="{tenant}"[^}}]*hit="{hit}"[^}}]*\}}\s+([\d.]+)'
    )
    m = re.search(pattern, body)
    if not m:
        return 0.0
    return float(m.group(1) or m.group(2))


# ── flag OFF: direct synth, no metric ───────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_uses_direct_synthesize(patched, monkeypatch):
    """two_planner_enabled=False → tts.synthesize called, not plan."""
    from app.core.config import settings
    from app.routes import twilio as twilio_module
    from app.routes.twilio_actor import TwilioActorSession

    monkeypatch.setattr(settings, "two_planner_enabled", False)
    tts = ElevenLabsLikeTTS()
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: tts)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-off", tenant_id="t-off",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    assert tts.direct_calls >= 1
    assert tts.plan_calls == 0
    await session.stop("test")


# ── flag ON, elevenlabs: VPL path fires ─────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_elevenlabs_uses_plan_path(patched, monkeypatch):
    from app.core.config import settings
    from app.routes import twilio as twilio_module
    from app.routes.twilio_actor import TwilioActorSession
    from packages.core_agent.planners import PerformancePlanner
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_planner_enabled", True)
    tts = ElevenLabsLikeTTS()
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: tts)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-on", tenant_id="t-on",
    )
    # Pre-inject perf planner with a fake LLM so we don't touch Groq.
    session._perf_planner = PerformancePlanner(
        llm=FakePerfLLM(), timeout_ms=500,
    )

    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    assert tts.plan_calls >= 1, "VPL path should call synthesize_from_plan"
    assert tts.last_plan is not None
    assert tts.last_plan.provider == "elevenlabs"

    body = generate_latest(REGISTRY).decode()
    hit_true = _counter(body, "voiceops_two_planner_hit_total", "t-on", "true")
    assert hit_true >= 1, f"expected at least one hit=true, body has:\n{body[-1000:]}"

    await session.stop("test")


# ── flag ON, elevenlabs, perf planner errors: fallback path ─────────

@pytest.mark.asyncio
async def test_flag_on_perf_error_still_ships_audio(patched, monkeypatch):
    """Perf planner raises; _vpl_synthesize must still call
    synthesize_from_plan (with default delivery) and bump hit=false."""
    from app.core.config import settings
    from app.routes import twilio as twilio_module
    from app.routes.twilio_actor import TwilioActorSession
    from packages.core_agent.planners import PerformancePlanner
    from prometheus_client import generate_latest, REGISTRY

    monkeypatch.setattr(settings, "two_planner_enabled", True)
    tts = ElevenLabsLikeTTS()
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: tts)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-err", tenant_id="t-err",
    )
    session._perf_planner = PerformancePlanner(
        llm=FakePerfLLM(raise_error=RuntimeError("groq down")),
        timeout_ms=500,
    )

    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    # VPL path still fires — just with default delivery
    assert tts.plan_calls >= 1
    body = generate_latest(REGISTRY).decode()
    hit_false = _counter(body, "voiceops_two_planner_hit_total", "t-err", "false")
    assert hit_false >= 1
    await session.stop("test")


# ── flag ON, cartesia: VPL gate skips, direct synth ─────────────────

@pytest.mark.asyncio
async def test_flag_on_cartesia_still_uses_direct_synth(patched, monkeypatch):
    """The provider gate `_provider_supports_vpl` currently returns
    True only for elevenlabs.  Cartesia should keep using direct
    synthesize until Sprint 10 wires its VPL compiler through."""
    from app.core.config import settings
    from app.routes import twilio as twilio_module
    from app.routes.twilio_actor import TwilioActorSession

    monkeypatch.setattr(settings, "two_planner_enabled", True)
    tts = CartesiaLikeTTS()
    monkeypatch.setattr(twilio_module, "_get_telephony_tts", lambda: tts)

    ws = FakeWebSocket()
    session = TwilioActorSession(
        ws=ws, stream_sid="MZ", call_id="CA-ct", tenant_id="t-ct",
    )
    await session.start()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if ws.events_of_type("mark"):
            break

    assert tts.direct_calls >= 1
    await session.stop("test")
