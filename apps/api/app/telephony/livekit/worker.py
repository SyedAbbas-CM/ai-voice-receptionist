"""LiveKit Agent worker — entrypoint for LiveKit-side calls.

STUB. See docs/LIVEKIT-INTEGRATION-PLAN.md for architecture.

## Purpose
Runs as a SEPARATE process from uvicorn (LiveKit Agents SDK spawns
per-call subprocess jobs, so integrating into FastAPI request handlers
is not the right model). Launched by scripts/run_livekit_worker.sh when
`TELEPHONY_PROVIDER in {"livekit", "both"}`.

## Un-defer implementation sketch

```python
from livekit import agents, rtc
from .session_adapter import LiveKitCallSession

async def entrypoint(ctx: agents.JobContext):
    # 1. Connect to the room LiveKit created for this caller
    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)

    # 2. Wait for the caller (SIP participant) to join
    participant = await ctx.wait_for_participant()

    # 3. Build our session — same CallActor/StreamingSTTBridge/brain
    #    that Twilio path uses, plumbed to LiveKit audio bridges.
    session = LiveKitCallSession(ctx=ctx, participant=participant)
    await session.start()

    # 4. Session's own lifecycle owns everything from here — brain,
    #    STT, TTS, filler, farewell, hangup. Worker exits when
    #    session.stop() fires.

if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="receptionist",   # must match dispatch rule
        )
    )
```

## Warmup considerations
First LiveKit call after worker boot may be 500-1500ms slower (SDK JIT +
first-connect handshake). Consider a boot-time synthetic warmup:
- Create a throwaway room, connect, disconnect
- Would hide cold-start latency from the first real caller

## Do NOT run this file yet
`livekit-agents` is not installed in venv. Import will fail. Un-defer
first per LIVEKIT-INTEGRATION-PLAN.md.
"""

raise NotImplementedError(
    "LiveKit worker is scaffold-only. See docs/LIVEKIT-INTEGRATION-PLAN.md "
    "before enabling. Do NOT set TELEPHONY_PROVIDER=livekit until this is "
    "written."
)
