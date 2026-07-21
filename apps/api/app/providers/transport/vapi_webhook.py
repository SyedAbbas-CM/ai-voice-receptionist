from __future__ import annotations

from ..base import TransportProvider


class VapiWebhookTransport(TransportProvider):
    """Phase 3 stub. Vapi hosts the call; we receive webhooks per turn."""

    name = "vapi"

    async def start_session(self, session_id: str) -> dict:
        raise NotImplementedError("Vapi webhook transport lands in phase 3")

    async def end_session(self, session_id: str) -> None:
        raise NotImplementedError("Vapi webhook transport lands in phase 3")
