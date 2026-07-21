from __future__ import annotations

from ..base import TransportProvider


class LiveKitTransport(TransportProvider):
    """Phase 5 stub. LiveKit Agents framework + SIP for telephony."""

    name = "livekit"

    async def start_session(self, session_id: str) -> dict:
        raise NotImplementedError("LiveKit transport lands in phase 5")

    async def end_session(self, session_id: str) -> None:
        raise NotImplementedError("LiveKit transport lands in phase 5")
