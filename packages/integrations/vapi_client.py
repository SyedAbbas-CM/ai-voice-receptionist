"""Vapi outbound call client.

The repo already RECEIVES Vapi webhooks in apps/api/app/routes/vapi.py
(the custom-LLM endpoint at /vapi/chat/completions and the events endpoint
at /vapi/events). But it couldn't ORIGINATE calls. This module adds that
half.

Ports the SubtoDealz n8n `VAPI - Make Call3` HTTP node — same request
shape — but reads the API key from env instead of hardcoding it in the
JSON like the original graph did.

Usage:
    client = VapiClient(api_key=settings.vapi_private_key)
    result = await client.dispatch_call(
        assistant_id="asst_...",
        phone_number_id="pn_...",
        customer_number="+15551234567",
        variable_values={"lead_name": "Bob", "property_address": "123 Elm St"},
    )
    print(result["id"])  # Vapi call id — save to link the incoming webhook back
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


VAPI_BASE = "https://api.vapi.ai"


class VapiError(Exception):
    pass


@dataclass
class DispatchResult:
    id: str            # Vapi call id (use as session identifier)
    status: str        # "queued" / "ringing" / etc — Vapi lifecycle state
    raw: dict          # full response body for logging / debugging


class VapiClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise VapiError("VAPI_API_KEY (or VAPI_PRIVATE_KEY) not configured")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def dispatch_call(
        self,
        assistant_id: str,
        phone_number_id: str,
        customer_number: str,
        variable_values: Optional[dict] = None,
        assistant_overrides: Optional[dict] = None,
    ) -> DispatchResult:
        """Fire an outbound call. Non-blocking — Vapi returns immediately with
        a call id; the actual call happens asynchronously and results arrive
        via the /vapi/events end-of-call-report webhook.

        assistant_overrides lets you override any field on the assistant config
        for this specific call (first_message, model, voice, etc). variable_values
        is a shortcut for `assistant_overrides.variableValues` — the map of
        template variables the assistant's system prompt references (e.g.
        {{ lead_name }}, {{ property_address }}, {{ rent_amount }}).
        """
        overrides = dict(assistant_overrides or {})
        if variable_values:
            overrides.setdefault("variableValues", {}).update(variable_values)

        payload: dict = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": customer_number},
        }
        if overrides:
            payload["assistantOverrides"] = overrides

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{VAPI_BASE}/call/phone", headers=self._headers, json=payload)

        if r.status_code >= 400:
            raise VapiError(f"Vapi POST /call/phone -> {r.status_code}: {r.text[:400]}")

        body = r.json()
        return DispatchResult(id=body.get("id", ""), status=body.get("status", "unknown"), raw=body)

    async def get_call(self, call_id: str) -> dict:
        """Fetch a call by id. Rarely needed in production because /vapi/events
        pushes end-of-call reports, but handy for debugging or reconciling."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{VAPI_BASE}/call/{call_id}", headers=self._headers)
        if r.status_code >= 400:
            raise VapiError(f"Vapi GET /call/{call_id} -> {r.status_code}: {r.text[:400]}")
        return r.json()
