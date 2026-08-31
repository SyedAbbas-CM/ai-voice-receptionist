"""AudioRecorder tests — buffer, silence-pad, finalize to MP3.

Uses asyncio.run() rather than pytest-asyncio (project doesn't have
the plugin installed and I don't want to add a dep just for these
tests). Requires ffmpeg. Tests skip cleanly if not installed."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from packages.recording import AudioRecorder


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not installed on this host",
)


# ─── Helpers ────────────────────────────────────────────────────────────────

_SAMPLE_RATE = 8000  # µ-law 8kHz — 8000 bytes = 1 sec
_TONE = bytes([0x7F]) * (_SAMPLE_RATE // 2)  # ~500ms of loud-ish sample


def _read_mp3_duration_ms(path: Path) -> int:
    """Ask ffprobe for the duration of an MP3, in ms."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], text=True).strip()
    return int(float(out) * 1000)


def _read_mp3_channels(path: Path) -> int:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=channels",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], text=True).strip()
    return int(out.splitlines()[0])


# ─── 1. Empty buffers ───────────────────────────────────────────────────────


def test_empty_call_produces_no_recording(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CAempty", tenant_id="test", root_dir=tmp_path)
        return await r.finalize()
    path, dur = asyncio.run(go())
    assert path is None
    assert dur == 0
    assert list(tmp_path.rglob("*.mp3")) == []


# ─── 2. Basic round-trip ────────────────────────────────────────────────────


def test_records_both_sides_and_encodes(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CAbasic", tenant_id="clinic", root_dir=tmp_path)
        r.append_caller(_TONE)
        r.append_agent(_TONE)
        r.append_caller(_TONE)
        r.append_agent(_TONE)
        return await r.finalize()
    path, dur = asyncio.run(go())
    assert path is not None, "expected an mp3 to be produced"
    assert path.exists()
    assert path.stat().st_size > 1000, "mp3 should be non-trivial"
    assert path.parent.name == "clinic"
    assert path.name == "CAbasic.mp3"
    mp3_ms = _read_mp3_duration_ms(path)
    assert abs(mp3_ms - dur) < max(200, dur // 10), f"dur={dur} mp3={mp3_ms}"


# ─── 3. Stereo output — caller left, agent right ────────────────────────────


def test_output_is_stereo(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CAstereo", tenant_id="t", root_dir=tmp_path)
        r.append_caller(_TONE)
        r.append_agent(_TONE)
        return await r.finalize()
    path, _ = asyncio.run(go())
    assert path is not None
    assert _read_mp3_channels(path) == 2, "recording must be stereo"


# ─── 4. Silence padding when one side goes quiet ────────────────────────────


def test_silence_padding_stitches_gaps(tmp_path):
    """Simulate a turn: caller talks (500ms), silence gap, agent talks
    (500ms). Total wall-clock should be at least ~1.1s, not just the
    500+500=1000ms of concat'd audio bytes."""
    async def go():
        r = AudioRecorder(call_id="CAgap", tenant_id="t", root_dir=tmp_path)
        r.append_caller(_TONE)
        await asyncio.sleep(0.6)
        r.append_agent(_TONE)
        return await r.finalize()
    path, dur = asyncio.run(go())
    assert path is not None
    assert dur >= 900, f"expected >=900ms wall-clock, got {dur}"


# ─── 5. Idempotent finalize ────────────────────────────────────────────────


def test_finalize_is_idempotent(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CAidem", tenant_id="t", root_dir=tmp_path)
        r.append_caller(_TONE)
        r.append_agent(_TONE)
        p1, d1 = await r.finalize()
        p2, d2 = await r.finalize()
        return p1, p2, d2
    p1, p2, d2 = asyncio.run(go())
    assert p1 is not None
    assert p2 is None
    assert d2 == 0


# ─── 6. Append-after-finalize is a no-op (safe) ─────────────────────────────


def test_append_after_finalize_is_safe(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CApost", tenant_id="t", root_dir=tmp_path)
        r.append_caller(_TONE)
        await r.finalize()
        r.append_caller(_TONE)
        r.append_agent(_TONE)
    asyncio.run(go())  # must not raise


# ─── 7. Bounded memory — huge inputs get capped ─────────────────────────────


def test_bounded_memory_caps_at_100min(tmp_path):
    async def go():
        r = AudioRecorder(call_id="CAhuge", tenant_id="t", root_dir=tmp_path)
        chunk = bytes([0x7F]) * (1024 * 1024)  # 1 MB
        for _ in range(200):
            r.append_caller(chunk)
        r.append_agent(_TONE)
        return await r.finalize()
    path, _ = asyncio.run(go())
    assert path is not None


# ─── 8. Cleanup — no lingering temp dirs after finalize ────────────────────


def test_no_lingering_tempdirs(tmp_path):
    before = set(Path(tempfile.gettempdir()).glob("rec_CA*"))

    async def go():
        r = AudioRecorder(call_id="CAclean", tenant_id="t", root_dir=tmp_path)
        r.append_caller(_TONE)
        r.append_agent(_TONE)
        await r.finalize()
    asyncio.run(go())

    after = set(Path(tempfile.gettempdir()).glob("rec_CA*"))
    new = after - before
    lingering = [p for p in new if "CAclean" in p.name]
    assert lingering == [], f"leaked temp dirs: {lingering}"
