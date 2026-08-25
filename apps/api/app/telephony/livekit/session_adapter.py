"""LiveKitCallSession — binds a LiveKit room job to our CallActor + brain.

STUB. See docs/LIVEKIT-INTEGRATION-PLAN.md for architecture.

## Purpose
Parallel to TwilioActorSession (apps/api/app/routes/twilio_actor.py).
Same downstream contract:
- Owns a CallActor for generation control (bump_turn, bump_speech,
  register_speech_task, all the state-machine primitives)
- Owns a StreamingSTTBridge for Deepgram Flux streaming
- Registers the same event handlers (STT partial/final, turn events,
  interruption, brain_completed, speech_completed)
- Runs the same session_manager + ReceptionistBrain
- All fixes shipped this week (interrupt-wait-for-final, cache-poisoning
  suppression, pumper-task-cancel, TTS stream watchdog, idle-followup
  guard, filler kill, no-reflex-opener prompt, etc.) work unchanged
  because they live in shared components below the transport layer.

The ONLY differences from TwilioActorSession are:
- Inbound audio arrives via LiveKitAudioBridge, not Twilio media WSS
- Outbound audio leaves via LiveKitTTSBridge, not Twilio JSON send
- No TwiML webhook (LiveKit dispatch rule creates the room)
- No FIRST40 mark (Twilio-specific — replace with LiveKit's own playout
  ACK if we want equivalent telemetry)

## Un-defer implementation sketch

```python
from livekit import agents, rtc
from packages.runtime.call_actor import CallActor
from packages.runtime.streaming_stt_bridge import StreamingSTTBridge
from app.core import session_manager
from app.providers import get_stt
from .audio_bridge import LiveKitAudioBridge
from .tts_bridge import LiveKitTTSBridge

class LiveKitCallSession:
    def __init__(self, ctx: agents.JobContext, participant: rtc.RemoteParticipant):
        self._ctx = ctx
        self._participant = participant
        self._call_id = ctx.room.name  # LiveKit's per-call room name
        self._session_id = f"livekit_{self._call_id}"

        # Same CallActor as Twilio path — brain doesn't care what
        # transport it lives behind.
        self.actor = CallActor(call_id=self._call_id, tenant_id="default")

        # Same STT bridge — Deepgram Flux with all fixes.
        # (mulaw_input=True because audio_bridge downsamples to μ-law 8k
        # before feeding, matching Twilio-shape input semantics.)
        self._stt_bridge = StreamingSTTBridge(
            actor=self.actor,
            stt_provider=get_stt(),
            mulaw_input=True,
        )

        self._audio_bridge = LiveKitAudioBridge(self._stt_bridge)
        self._tts_bridge = LiveKitTTSBridge(ctx.room)

    async def start(self):
        # 1. Attach audio bridge to caller's mic
        # 2. Start STT bridge (opens Deepgram Flux WS)
        # 3. Publish outbound TTS track
        # 4. Register the same handlers TwilioActorSession registers
        #    (turn events, STT native turn events, brain/speech job
        #    completion, etc.). Copy from twilio_actor.py:880-940.
        # 5. Fire greeting (same greeting-cache lookup as Twilio path)
        # 6. Actor.run() — same main loop
        raise NotImplementedError("see docstring")

    # ... same lifecycle methods as TwilioActorSession, but with
    # send_audio_frames delegating to _tts_bridge.send_mulaw_frames
    # instead of Twilio JSON writes.
```

## Duck-typing note
The plumbing that keeps our fixes working — brain, gate, cache, filler,
farewell, watchdogs — all interacts with the actor + STT bridge, not
the transport. So LiveKitCallSession only needs to reimplement the
transport-facing methods (audio in, audio out, mark ACKs). Everything
else calls into the same modules.

## Do NOT run this file yet
Imports livekit; not installed. See LIVEKIT-INTEGRATION-PLAN.md.
"""

raise NotImplementedError(
    "session_adapter.py is scaffold-only. See docs/LIVEKIT-INTEGRATION-PLAN.md."
)
