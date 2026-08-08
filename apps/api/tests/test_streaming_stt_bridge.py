"""Sprint 10 C1: StreamingSTTBridge tests.

Uses a FakeSTT to avoid network calls.  Verifies:
  * Bridge starts a consumer that reads from the audio queue
  * Fed frames flow to the STT provider
  * STT partials → CallEvent(source=STT, kind=partial)
  * STT finals → CallEvent(source=STT, kind=final)
  * stop() cancels cleanly
  * Provider without supports_streaming → bridge stays idle, no crash
  * Reconnect on transient error
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from packages.runtime import CallActor, StreamingSTTBridge
from packages.runtime.call_event import EventSource


class _STTEventStub:
    def __init__(self, kind: str, text: str = "", is_final: bool = False):
        self.kind = kind
        self.text = text
        self.is_final = is_final


class _FakeStreamingSTT:
    name = "fake-stream"
    supports_streaming = True

    def __init__(self, events):
        self.events = events
        self.frames_received = 0

    async def transcribe_stream(self, audio_chunks, sample_rate=8000, encoding="linear16"):
        async def _drain():
            async for _ in audio_chunks:
                self.frames_received += 1
        drain = asyncio.create_task(_drain())
        try:
            for ev in self.events:
                await asyncio.sleep(0.01)
                yield ev
        finally:
            drain.cancel()


class _FakeBatchSTT:
    """A provider WITHOUT supports_streaming."""
    name = "fake-batch"
    supports_streaming = False


@pytest.mark.asyncio
async def test_bridge_flows_frames_to_provider():
    actor = CallActor(call_id="CA-1", tenant_id="acme")
    await actor.start()
    stt = _FakeStreamingSTT([
        _STTEventStub("partial", "hello"),
        _STTEventStub("final", "hello there", is_final=True),
    ])
    bridge = StreamingSTTBridge(actor=actor, stt_provider=stt, mulaw_input=False)
    await bridge.start()
    for _ in range(5):
        bridge.feed(b"\x00\x00" * 80)
    await asyncio.sleep(0.15)
    await bridge.stop()
    await actor.stop()
    assert stt.frames_received >= 1


@pytest.mark.asyncio
async def test_bridge_emits_partial_and_final_events():
    actor = CallActor(call_id="CA-2", tenant_id="acme")
    seen: list[tuple[str, str]] = []

    async def _capture(a, ev):
        seen.append((ev.kind, ev.payload["text"]))
        return True

    actor.handlers[(EventSource.STT, "partial")] = _capture
    actor.handlers[(EventSource.STT, "final")] = _capture
    await actor.start()

    stt = _FakeStreamingSTT([
        _STTEventStub("partial", "he"),
        _STTEventStub("partial", "hello"),
        _STTEventStub("final", "hello there", is_final=True),
    ])
    bridge = StreamingSTTBridge(actor=actor, stt_provider=stt, mulaw_input=False)
    await bridge.start()
    bridge.feed(b"\x00" * 160)
    # Wait for events to propagate through the bridge + actor mailbox
    for _ in range(60):
        await asyncio.sleep(0.02)
        if len(seen) >= 3:
            break
    await bridge.stop()
    await actor.stop()

    kinds = [k for k, _ in seen]
    texts = [t for _, t in seen]
    assert kinds == ["partial", "partial", "final"]
    assert texts == ["he", "hello", "hello there"]


@pytest.mark.asyncio
async def test_bridge_idle_when_provider_not_streaming():
    """Non-streaming provider → bridge doesn't crash + doesn't emit."""
    actor = CallActor(call_id="CA-3", tenant_id="acme")
    await actor.start()
    stt = _FakeBatchSTT()
    bridge = StreamingSTTBridge(actor=actor, stt_provider=stt)
    await bridge.start()
    await asyncio.sleep(0.05)
    await bridge.stop()
    await actor.stop()
    # No exception is the assertion


@pytest.mark.asyncio
async def test_bridge_queue_full_drops_frames_gracefully():
    """Backpressure: bridge should not crash when queue is saturated."""
    actor = CallActor(call_id="CA-4", tenant_id="acme")
    await actor.start()

    class _SlowSTT:
        name = "slow"
        supports_streaming = True
        async def transcribe_stream(self, audio_chunks, sample_rate=8000, encoding="linear16"):
            # Never actually consume — force queue backpressure
            await asyncio.sleep(0.5)
            return
            yield  # pragma: no cover — generator syntax

    bridge = StreamingSTTBridge(actor=actor, stt_provider=_SlowSTT(), mulaw_input=False)
    await bridge.start()
    # Push way more frames than queue capacity (800)
    for _ in range(2000):
        bridge.feed(b"\x00" * 160)
    await bridge.stop()
    await actor.stop()


@pytest.mark.asyncio
async def test_bridge_reconnects_on_transient_error():
    """First stream attempt raises; bridge reconnects and succeeds."""
    actor = CallActor(call_id="CA-5", tenant_id="acme")
    seen: list[str] = []

    async def _capture(a, ev):
        seen.append(ev.payload["text"])
        return True
    actor.handlers[(EventSource.STT, "final")] = _capture
    await actor.start()

    class _FlakyStream:
        name = "flaky"
        supports_streaming = True
        def __init__(self):
            self.calls = 0
        async def transcribe_stream(self, audio_chunks, sample_rate=8000, encoding="linear16"):
            self.calls += 1
            async def _drain():
                async for _ in audio_chunks:
                    pass
            drain = asyncio.create_task(_drain())
            try:
                if self.calls == 1:
                    raise RuntimeError("transient")
                await asyncio.sleep(0.01)
                yield _STTEventStub("final", "second attempt", is_final=True)
            finally:
                drain.cancel()

    stt = _FlakyStream()
    bridge = StreamingSTTBridge(
        actor=actor, stt_provider=stt, mulaw_input=False,
        max_reconnects=3, reconnect_backoff_s=0.01,
    )
    await bridge.start()
    bridge.feed(b"\x00" * 160)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if seen:
            break
    await bridge.stop()
    await actor.stop()
    assert stt.calls >= 2
    assert seen == ["second attempt"]


@pytest.mark.asyncio
async def test_bridge_gives_up_after_max_reconnects():
    actor = CallActor(call_id="CA-6", tenant_id="acme")
    stream_failed_events: list[dict] = []

    async def _capture(a, ev):
        stream_failed_events.append(ev.payload)
        return True
    actor.handlers[(EventSource.STT, "stream_failed")] = _capture
    await actor.start()

    class _AlwaysFails:
        name = "broken"
        supports_streaming = True
        async def transcribe_stream(self, audio_chunks, sample_rate=8000, encoding="linear16"):
            raise RuntimeError("permanently down")
            yield  # pragma: no cover

    bridge = StreamingSTTBridge(
        actor=actor, stt_provider=_AlwaysFails(), mulaw_input=False,
        max_reconnects=2, reconnect_backoff_s=0.01,
    )
    await bridge.start()
    for _ in range(40):
        await asyncio.sleep(0.03)
        if stream_failed_events:
            break
    await bridge.stop()
    await actor.stop()
    assert len(stream_failed_events) == 1
    assert "reconnects" in stream_failed_events[0]
