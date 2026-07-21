"""Verify the Groq STT adapter rejects short clips and filters known
Whisper hallucinations ("Oh my god!", "Thank you.", etc.) before returning
garbage to the brain."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_short_clip_returns_empty_without_hitting_api(monkeypatch):
    from app.providers.stt.groq_stt import GroqSTT
    from app.core.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "stub")
    stt = GroqSTT()

    # Well under the min bytes threshold
    result = await stt.transcribe(b"\x00" * 100, mime="audio/webm;codecs=opus")
    assert result == ""


@pytest.mark.asyncio
async def test_filters_oh_my_god_hallucination(monkeypatch):
    """Whisper's most common hallucination on silent input. Must NOT reach
    the brain — otherwise a caller who taps the button by accident gets
    the AI responding to 'Oh my god!'."""
    from app.providers.stt.groq_stt import GroqSTT
    from app.core.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "stub")

    for hallucinated in ["Oh my god!", "OH MY GOD", "thank you.", "You.", "Bye.", "Thanks for watching!"]:
        stt = GroqSTT()
        fake_response = AsyncMock()
        fake_response.raise_for_status = lambda: None
        fake_response.json = lambda: {"text": hallucinated}

        class FakeClient:
            def __init__(self, timeout=None): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *args, **kwargs): return fake_response

        with patch("app.providers.stt.groq_stt.httpx.AsyncClient", FakeClient):
            # Provide enough bytes to pass the length gate
            result = await stt.transcribe(b"\x00" * 10000, mime="audio/webm;codecs=opus")
        assert result == "", f"expected filter to reject {hallucinated!r}, got {result!r}"


@pytest.mark.asyncio
async def test_real_transcript_passes_through(monkeypatch):
    from app.providers.stt.groq_stt import GroqSTT
    from app.core.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "stub")
    stt = GroqSTT()

    fake_response = AsyncMock()
    fake_response.raise_for_status = lambda: None
    fake_response.json = lambda: {"text": "I need an appointment for back pain tomorrow at 10am."}

    class FakeClient:
        def __init__(self, timeout=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *args, **kwargs): return fake_response

    with patch("app.providers.stt.groq_stt.httpx.AsyncClient", FakeClient):
        result = await stt.transcribe(b"\x00" * 20000, mime="audio/webm;codecs=opus")
    assert "back pain" in result


@pytest.mark.asyncio
async def test_strips_codec_suffix_from_mime(monkeypatch):
    """Groq gets confused by 'audio/webm;codecs=opus' — verify we send
    the clean top-level mime."""
    from app.providers.stt.groq_stt import GroqSTT
    from app.core.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "stub")
    stt = GroqSTT()

    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"text": "hello"}

    class FakeClient:
        def __init__(self, timeout=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, headers=None, files=None, data=None):
            captured["files"] = files
            captured["data"] = data
            return FakeResp()

    with patch("app.providers.stt.groq_stt.httpx.AsyncClient", FakeClient):
        await stt.transcribe(b"\x00" * 20000, mime="audio/webm;codecs=opus")

    filename, blob, sent_mime = captured["files"]["file"]
    assert sent_mime == "audio/webm", f"expected clean mime, got {sent_mime}"
    assert filename == "audio.webm"
    assert captured["data"]["temperature"] == "0"


@pytest.mark.asyncio
async def test_maps_ogg_and_wav_and_mp3(monkeypatch):
    from app.providers.stt.groq_stt import GroqSTT
    from app.core.config import settings

    monkeypatch.setattr(settings, "groq_api_key", "stub")
    stt = GroqSTT()

    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"text": "hi"}

    class FakeClient:
        def __init__(self, timeout=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, headers=None, files=None, data=None):
            captured.update({"mime": files["file"][2], "ext": files["file"][0]})
            return FakeResp()

    cases = [
        ("audio/ogg;codecs=opus", "audio/ogg", "audio.ogg"),
        ("audio/wav", "audio/wav", "audio.wav"),
        ("audio/mp3", "audio/mpeg", "audio.mp3"),
        ("audio/mpeg", "audio/mpeg", "audio.mp3"),
        ("audio/mp4", "audio/mp4", "audio.m4a"),
    ]
    for input_mime, expected_mime, expected_filename in cases:
        with patch("app.providers.stt.groq_stt.httpx.AsyncClient", FakeClient):
            await stt.transcribe(b"\x00" * 20000, mime=input_mime)
        assert captured["mime"] == expected_mime, f"{input_mime} -> {captured['mime']!r}"
        assert captured["ext"] == expected_filename
