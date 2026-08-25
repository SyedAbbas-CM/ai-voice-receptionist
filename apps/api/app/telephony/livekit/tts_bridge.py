"""Outbound audio bridge — ElevenLabs μ-law 8k → LiveKit room audio_out.

STUB. See docs/LIVEKIT-INTEGRATION-PLAN.md for architecture.

## Purpose
Replaces the Twilio-specific _send_audio_frames path with LiveKit's
rtc.AudioSource / rtc.LocalAudioTrack publish flow. Same input
contract (μ-law 8kHz mulaw bytes from ElevenLabs), different output
(publish PCM 48kHz frames into the LiveKit room).

## Un-defer implementation sketch

```python
import asyncio
import audioop
from livekit import rtc

# LiveKit expects: 48kHz sample rate, 1 channel, 20ms frames = 960 samples
LK_SAMPLE_RATE = 48000
LK_FRAME_MS = 20
LK_FRAME_SAMPLES = 960  # 48000 * 0.020

class LiveKitTTSBridge:
    def __init__(self, room: rtc.Room):
        self._room = room
        self._audio_source: rtc.AudioSource | None = None
        self._upsample_state = None  # persistent across frames

    async def start(self):
        # Publish an outbound audio track on the room.
        self._audio_source = rtc.AudioSource(LK_SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track(
            "receptionist-tts", self._audio_source,
        )
        await self._room.local_participant.publish_track(track)

    async def send_mulaw_frames(self, mulaw_bytes: bytes) -> None:
        # μ-law 8k → linear16 8k → linear16 48k → publish 20ms frames
        try:
            pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
            pcm_48k, self._upsample_state = audioop.ratecv(
                pcm_8k, 2, 1, 8000, 48000, self._upsample_state,
            )
        except Exception:
            return
        # Split into 20ms LiveKit frames and capture.
        # PCM 48k int16 mono = 96 bytes per ms = 1920 bytes per 20ms frame.
        frame_bytes = LK_FRAME_SAMPLES * 2
        for i in range(0, len(pcm_48k), frame_bytes):
            chunk = pcm_48k[i:i + frame_bytes]
            if len(chunk) < frame_bytes:
                # pad the last short frame with silence
                chunk = chunk + b"\\x00" * (frame_bytes - len(chunk))
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=LK_SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=LK_FRAME_SAMPLES,
            )
            await self._audio_source.capture_frame(frame)
```

## Integration point in existing code
Currently TwilioActorSession._send_audio_frames does the Twilio JSON
wire send. On the LiveKit path we need session_adapter to substitute
LiveKitTTSBridge.send_mulaw_frames wherever _send_audio_frames is
called. Options:
- (a) Subclass TwilioActorSession, override _send_audio_frames
- (b) Add a transport-provider abstraction and pick at construction
- (c) Duck-type — LiveKitCallSession implements the same
      _send_audio_frames signature and delegates to this bridge

(c) is least-invasive to existing Twilio code.

## Do NOT run this file yet
`livekit` package not in venv. Import will fail.
"""

raise NotImplementedError(
    "tts_bridge.py is scaffold-only. See docs/LIVEKIT-INTEGRATION-PLAN.md."
)
