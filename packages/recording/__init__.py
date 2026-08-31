"""Call recording — tee-and-flush audio buffers, one per call.

Twilio Media Streams is mutually exclusive with Twilio's server-side
<Record> TwiML verb, so we can't ask Twilio to record for us. Instead
we tee both audio directions inside the WebSocket handler:

  * `AudioRecorder.append_caller(mulaw)`  — every inbound frame
  * `AudioRecorder.append_agent(mulaw)`   — every outbound frame

On call end, `AudioRecorder.finalize()` runs ffmpeg to interleave the
two mono µ-law streams into a stereo MP3 (caller on left channel,
agent on right), writes it to `data/recordings/{tenant}/{call_id}.mp3`,
returns the path + duration.

Design decisions:
  - Buffers are `bytearray` in-memory. At 8kHz µ-law that's 8KB/sec
    per side, so a 10-minute call is 4.8 MB in RAM per side — fine.
  - We do NOT sync the two streams tick-for-tick. The caller has silence
    when the agent is talking (Twilio pauses inbound during TTS) and
    vice versa. We interleave by wall-clock offset from call start —
    each append records `time.monotonic() - self._t0` and the flush
    step pads gaps with µ-law silence (0xFF).
  - ffmpeg is invoked in a subprocess so the event loop isn't blocked
    while it encodes.
  - Failure to finalize (ffmpeg missing, disk full, etc.) MUST NOT
    crash the call — we log and swallow.
"""
from packages.recording.recorder import AudioRecorder

__all__ = ["AudioRecorder"]
