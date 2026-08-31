"""Per-call audio recorder — tee µ-law frames, finalize to stereo MP3.

Usage from twilio_actor:

    from packages.recording import AudioRecorder

    # On start
    self.recorder = AudioRecorder(
        call_id=call_sid, tenant_id=tenant_id,
        root_dir=Path("data/recordings"),
    )

    # On every inbound Media Streams frame
    self.recorder.append_caller(mulaw_bytes)

    # On every outbound audio frame (agent TTS)
    self.recorder.append_agent(mulaw_bytes)

    # On stop
    path, duration_ms = await self.recorder.finalize()
    # path is None on failure — safe to leave sessions.recording_path
    # NULL and move on.

## Sync strategy

Both directions accumulate independently. When a side is silent (the
agent isn't talking, or the caller isn't talking) NO bytes arrive for
that side — Twilio doesn't send inbound frames while our TTS is on
the wire, and we don't send outbound frames when we have nothing to
say. Naively concatenating the two buffers would make a 30-second
call become 15s of caller + 15s of agent stacked, not two 30-second
streams in parallel.

We fix this by wall-clock stitching. Each `append_*` records
`monotonic() - t0`. In `finalize()` we walk each side and pad any
gap-since-last-append with µ-law silence (0xFF at 8kHz).

## Failure modes handled

  - ffmpeg missing → log, return (None, 0)
  - disk full → log, return (None, 0)
  - empty buffers → return (None, 0) — nothing to encode
  - buffers too big (>50MB each = ~100min call) → truncate + warn
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 8 kHz µ-law. 8000 bytes = 1 second of audio per direction.
_SAMPLE_RATE = 8000
_MULAW_SILENCE = 0xFF  # µ-law encoding of PCM zero
_MAX_BYTES_PER_SIDE = 50 * 1024 * 1024  # ~100 min of 8kHz µ-law


@dataclass
class _SideBuffer:
    """One direction of audio. Tracks bytes + last-append-offset for
    wall-clock stitching."""
    buf: bytearray = field(default_factory=bytearray)
    last_append_ms: float = 0.0  # ms since t0 of most recent append end

    def append(self, mulaw: bytes, now_ms: float) -> None:
        """Append `mulaw` bytes, padding any silence gap first.

        `now_ms` is milliseconds since call start (t0). We pad from
        wherever this side's buffer currently sits (len_bytes ÷ 8) up
        to now_ms with µ-law silence, then append the fresh chunk.

        This means: if the caller talks 0-500ms and the agent doesn't
        talk until 1200ms, the AGENT buffer at agent's first append
        will be zero bytes → we pad 1200ms of silence, then append
        agent audio. Both sides end up wall-clock aligned.
        """
        if not mulaw:
            return
        if len(self.buf) >= _MAX_BYTES_PER_SIDE:
            return  # cap at ~100min; caller logged the warn once

        # Where does this side's buffer currently END, in ms?
        current_end_ms = len(self.buf) * (1000.0 / _SAMPLE_RATE)
        # How much silence to insert before this chunk
        gap_ms = now_ms - current_end_ms
        if gap_ms > 20.0:
            pad_bytes = int(gap_ms * (_SAMPLE_RATE / 1000.0))
            # Cap silence gaps at 30s to avoid pathological cases
            pad_bytes = min(pad_bytes, 30 * _SAMPLE_RATE)
            if pad_bytes > 0:
                self.buf.extend(bytes([_MULAW_SILENCE]) * pad_bytes)

        self.buf.extend(mulaw)
        # Retain last_append_ms for observability / metrics if we ever
        # want it. Not used for stitching anymore — buffer length is
        # the truth.
        added_ms = len(mulaw) * (1000.0 / _SAMPLE_RATE)
        self.last_append_ms = now_ms + added_ms


class AudioRecorder:
    """Buffer both directions of a call, encode to stereo MP3 at end.

    Thread-safety: caller must serialize append/finalize on the event
    loop. We do NOT lock — Twilio's WebSocket handler runs on one
    coroutine, so appends are naturally serialized.
    """

    __slots__ = (
        "call_id", "tenant_id", "root_dir",
        "_t0", "_caller", "_agent",
        "_finalized", "_disabled",
    )

    def __init__(
        self,
        *,
        call_id: str,
        tenant_id: str,
        root_dir: Path,
    ) -> None:
        self.call_id = call_id
        self.tenant_id = tenant_id or "unknown"
        self.root_dir = Path(root_dir)
        self._t0 = time.monotonic()
        self._caller = _SideBuffer()
        self._agent = _SideBuffer()
        self._finalized = False
        self._disabled = False  # set true on unrecoverable append error

    def _now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def append_caller(self, mulaw: bytes) -> None:
        if self._disabled or self._finalized:
            return
        try:
            self._caller.append(mulaw, self._now_ms())
        except Exception as e:
            log.warning("recorder append_caller failed for %s: %s", self.call_id, e)
            self._disabled = True

    def append_agent(self, mulaw: bytes) -> None:
        if self._disabled or self._finalized:
            return
        try:
            self._agent.append(mulaw, self._now_ms())
        except Exception as e:
            log.warning("recorder append_agent failed for %s: %s", self.call_id, e)
            self._disabled = True

    async def finalize(self) -> tuple[Optional[Path], int]:
        """Encode to MP3 and return (path, duration_ms). Idempotent —
        safe to call twice; second call returns (None, 0).

        Returns (None, 0) if there's nothing to record, or if ffmpeg
        fails. Never raises. On success, writes the MP3 and returns
        its path relative to nothing (absolute Path)."""
        if self._finalized:
            return None, 0
        self._finalized = True

        # Nothing to encode — dropped call, immediate hangup, etc.
        if not self._caller.buf and not self._agent.buf:
            return None, 0

        # Pad the shorter side to match the longer one with trailing
        # silence, so ffmpeg lines them up channel-for-channel.
        target_bytes = max(len(self._caller.buf), len(self._agent.buf))
        for side in (self._caller, self._agent):
            if len(side.buf) < target_bytes:
                side.buf.extend(
                    bytes([_MULAW_SILENCE]) * (target_bytes - len(side.buf))
                )

        duration_ms = int(target_bytes * (1000.0 / _SAMPLE_RATE))

        # Write raw µ-law temp files, then ffmpeg-merge to stereo MP3.
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"rec_{self.call_id}_"))
        try:
            caller_raw = tmp_dir / "caller.ulaw"
            agent_raw = tmp_dir / "agent.ulaw"
            caller_raw.write_bytes(bytes(self._caller.buf))
            agent_raw.write_bytes(bytes(self._agent.buf))

            # Output directory: data/recordings/{tenant}/
            out_dir = self.root_dir / self.tenant_id
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{self.call_id}.mp3"

            ok = await _ffmpeg_stereo_mp3(caller_raw, agent_raw, out_path)
            if not ok:
                return None, 0
            log.info(
                "RECORDING_SAVED call=%s tenant=%s path=%s duration_ms=%d",
                self.call_id, self.tenant_id, out_path, duration_ms,
            )
            return out_path, duration_ms
        except Exception as e:
            log.warning("recorder finalize failed for %s: %s", self.call_id, e)
            return None, 0
        finally:
            # Clean up temp raw files. Never leave them behind.
            for p in tmp_dir.glob("*.ulaw"):
                try:
                    p.unlink()
                except Exception:
                    pass
            try:
                tmp_dir.rmdir()
            except Exception:
                pass


async def _ffmpeg_stereo_mp3(
    caller_raw: Path, agent_raw: Path, out_path: Path,
) -> bool:
    """Merge two mono µ-law streams into a stereo MP3.

    Left channel = caller. Right channel = agent. Encoded at 64 kbps
    (voice quality, ~500 KB/min) — plenty for QA playback.

    Returns True on success.
    """
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        # Input 1: caller (mono, µ-law, 8kHz)
        "-f", "mulaw", "-ar", str(_SAMPLE_RATE), "-ac", "1", "-i", str(caller_raw),
        # Input 2: agent (mono, µ-law, 8kHz)
        "-f", "mulaw", "-ar", str(_SAMPLE_RATE), "-ac", "1", "-i", str(agent_raw),
        # Combine into stereo: [caller][agent]amerge → stereo layout
        "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]",
        "-map", "[a]",
        # MP3 encode at 64 kbps stereo
        "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "8000",
        str(out_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning(
                "ffmpeg failed rc=%d stderr=%s",
                proc.returncode, (stderr or b"").decode("utf-8", errors="replace")[:500],
            )
            return False
        # Sanity check the file exists + isn't zero-byte
        if not out_path.exists() or out_path.stat().st_size < 1024:
            log.warning("ffmpeg produced empty/tiny output: %s", out_path)
            return False
        return True
    except FileNotFoundError:
        log.warning("ffmpeg not installed — recording disabled (install: apt-get install ffmpeg)")
        return False
    except Exception as e:
        log.warning("ffmpeg subprocess failed: %s", e)
        return False
