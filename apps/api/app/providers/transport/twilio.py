from __future__ import annotations

from ..base import TransportProvider


class TwilioMediaStreamsTransport(TransportProvider):
    """Phase 4 stub. Twilio Media Streams + OpenAI Realtime via websocket proxy."""

    name = "twilio"

    async def start_session(self, session_id: str) -> dict:
        raise NotImplementedError("Twilio transport lands in phase 4")

    async def end_session(self, session_id: str) -> None:
        raise NotImplementedError("Twilio transport lands in phase 4")
