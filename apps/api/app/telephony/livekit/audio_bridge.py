"""Inbound audio bridge — LiveKit room mic → StreamingSTTBridge.

STUB. See docs/LIVEKIT-INTEGRATION-PLAN.md for architecture.

## Purpose
LiveKit delivers caller audio as PCM 48kHz mono 20ms frames (int16, 1920
bytes per frame, 960 samples). Our StreamingSTTBridge expects μ-law
8kHz frames (160 bytes each, 20ms). This module bridges the two, using
audioop with persistent state to avoid click artifacts across frames
(same pattern as NET-02 SmartTurn μ-law decode fix).

## Un-defer implementation sketch

```python
import audioop
from livekit import rtc
from packages.runtime.streaming_stt_bridge import StreamingSTTBridge

class LiveKitAudioBridge:
    def __init__(self, stt_bridge: StreamingSTTBridge):
        self._stt_bridge = stt_bridge
        # Persistent state across frames — prevents downsample click.
        self._downsample_state = None

    async def attach_to_track(self, track: rtc.RemoteAudioTrack):
        # Subscribe to caller's audio and forward every frame.
        audio_stream = rtc.AudioStream(track)
        async for event in audio_stream:
            frame = event.frame  # rtc.AudioFrame (PCM 48k int16 mono)
            self._on_frame(bytes(frame.data))

    def _on_frame(self, pcm_48k: bytes) -> None:
        # LK 48k → 8k linear16 → μ-law 8k
        try:
            pcm_8k, self._downsample_state = audioop.ratecv(
                pcm_48k, 2, 1, 48000, 8000, self._downsample_state,
            )
            mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
        except Exception:
            return  # drop malformed frame
        self._stt_bridge.feed(mulaw_8k)
```

## Encoding choice: mulaw@8k (measured 2026-08-23)
Feed StreamingSTTBridge μ-law 8kHz — NOT linear16@48k, even though
LiveKit delivers 48k natively.

Bench (`scripts/bench_flux_encoding.py`, 3 trials, 15s speech sample):
  - mulaw@8k first_update:    3360ms median (bench-artifact absolute)
  - linear16@48k first_update: 3304ms median
  - delta: 56ms, INSIDE per-trial variance (trial 3 flipped sign)

Verdict: latency-NEUTRAL. But upstream bandwidth is 12x smaller with
mulaw (8 KB/s vs 96 KB/s), which matters for reliability on flaky
uplinks. Keep the audioop downsample+encode step in `_on_frame` —
zero latency cost, big bandwidth win.

Only revisit this choice if we later find evidence that Flux delivers
significantly better transcription accuracy on 48kHz input for our
call profile. The current bench didn't measure accuracy (transcripts
were empty due to bench sample choice, not a real signal).

## Do NOT run this file yet
`livekit` package not in venv. Import will fail.
"""

raise NotImplementedError(
    "audio_bridge.py is scaffold-only. See docs/LIVEKIT-INTEGRATION-PLAN.md."
)
