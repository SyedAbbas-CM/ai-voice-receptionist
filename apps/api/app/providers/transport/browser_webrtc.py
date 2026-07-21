from __future__ import annotations

from ..base import TransportProvider


class BrowserWebRTCTransport(TransportProvider):
    """The browser call simulator is the transport in Phase 2 — sessions are
    created and ended directly via the REST API, no signalling needed."""

    name = "browser"

    async def start_session(self, session_id: str) -> dict:
        return {"session_id": session_id, "transport": "browser"}

    async def end_session(self, session_id: str) -> None:
        return None
