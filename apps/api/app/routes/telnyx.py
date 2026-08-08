"""Telnyx voice adapter.

Three endpoints:
  POST /telnyx/voice   - TeXML webhook for incoming call.  Returns a
                         <Response><Connect><Stream url="wss://..."/></Connect></Response>
                         that tells Telnyx to open a Media Stream WS to us.
  WS   /telnyx/stream  - Media Streams websocket.  Same µ-law 8kHz base64
                         wire format as Twilio; only the JSON envelope
                         differs.  Feeds frames into the SAME
                         TwilioActorSession from twilio_actor.py.
  POST /telnyx/dial    - Outbound trigger.  Fires a Call Control API
                         POST to https://api.telnyx.com/v2/calls with a
                         Bearer TELNYX_API_KEY.

Env:
  TELNYX_API_KEY          Bearer for Call Control API (outbound)
  TELNYX_PHONE_NUMBER     Default caller ID for /telnyx/dial
  TELNYX_APP_ID           Call Control Application / Connection ID
  TELNYX_PUBLIC_URL       Public https:// URL for this API (tunnel or prod)
  TELNYX_PUBLIC_KEY       Base64 Ed25519 public key from portal (verify)
  TELNYX_SIGNATURE_ENFORCE=false to skip Ed25519 verify in dev
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings


log = logging.getLogger(__name__)
router = APIRouter(tags=["telnyx"])


async def _verify_telnyx_signature(request: Request, body: bytes) -> None:
    """Telnyx signs webhooks with Ed25519.  Header
    `telnyx-signature-ed25519` is base64 signature; `telnyx-timestamp`
    is the unix ts that was prepended to the body.  Enforcement gated
    by TELNYX_SIGNATURE_ENFORCE — default TRUE (fail closed).
    Set TELNYX_SIGNATURE_ENFORCE=false only for local dev tunnels."""
    if os.environ.get("TELNYX_SIGNATURE_ENFORCE", "true").lower() in ("0", "false", "no"):
        return
    sig_b64 = request.headers.get("telnyx-signature-ed25519", "")
    ts = request.headers.get("telnyx-timestamp", "")
    pub_b64 = settings.telnyx_public_key or ""
    if not sig_b64 or not ts:
        raise HTTPException(401, "missing telnyx signature headers")
    # 2026-08-08 SEC FIX: fail CLOSED when the pub key is missing or
    # pynacl unavailable.  Previous behaviour skipped verify and let the
    # request through — attackers could spoof any webhook.
    if not pub_b64:
        raise HTTPException(
            500,
            "TELNYX_SIGNATURE_ENFORCE=true but TELNYX_PUBLIC_KEY unset — refusing to accept unverified webhook",
        )
    # Replay protection: reject ts more than 5 min from now.
    try:
        skew = abs(int(time.time()) - int(ts))
    except ValueError:
        raise HTTPException(401, "invalid telnyx timestamp")
    if skew > 300:
        raise HTTPException(401, f"telnyx timestamp skew too large ({skew}s)")
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError  # noqa: F401
    except ImportError:
        raise HTTPException(
            500,
            "TELNYX_SIGNATURE_ENFORCE=true but pynacl not installed — refusing to accept unverified webhook",
        )
    try:
        vk = VerifyKey(base64.b64decode(pub_b64))
        vk.verify(f"{ts}|".encode() + body, base64.b64decode(sig_b64))
    except Exception as e:
        raise HTTPException(401, f"invalid telnyx signature: {e}")


def _texml_stream_response(public_url: str) -> str:
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/telnyx/stream"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>"""


@router.post("/telnyx/voice")
async def telnyx_voice_webhook(request: Request) -> Response:
    body = await request.body()
    await _verify_telnyx_signature(request, body)
    public = settings.telnyx_public_url or ""
    if not public:
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Sorry, this number is not configured. Please set TELNYX_PUBLIC_URL.</Say><Hangup/></Response>""",
            media_type="application/xml",
        )
    return Response(content=_texml_stream_response(public), media_type="application/xml")


@router.websocket("/telnyx/stream")
async def telnyx_stream(ws: WebSocket) -> None:
    """Telnyx Media Streaming websocket.  Envelope shape:

      {event:"connected", ...}
      {event:"start", stream_id:"...", start:{call_control_id:"..."}}
      {event:"media", media:{payload:"<base64 µ-law>", track:"inbound_track"}}
      {event:"dtmf", dtmf:{digit:"5"}}
      {event:"clear"}
      {event:"stop", ...}

    Outbound frames use the same envelope minus `track`.  Reuses
    TwilioActorSession verbatim — Telnyx sends the same µ-law 8kHz
    payload Twilio does; only the wrapper JSON differs."""
    await ws.accept()

    from app.routes.twilio_actor import TwilioActorSession

    session: Optional[TwilioActorSession] = None
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "connected":
                log.info("telnyx connected: %s", event.get("version"))
                continue

            if kind == "start":
                start = event.get("start", {}) or {}
                stream_id = event.get("stream_id") or start.get("stream_id") or f"telnyx_{uuid.uuid4().hex[:12]}"
                call_id = start.get("call_control_id") or start.get("call_leg_id") or f"call_{uuid.uuid4().hex[:8]}"
                session = TwilioActorSession(
                    ws=ws,
                    stream_sid=stream_id,
                    call_id=call_id,
                    tenant_id="default",
                )
                log.info("telnyx start: call=%s stream=%s", call_id, stream_id)
                await session.start()
                continue

            if kind == "media" and session is not None:
                media = event.get("media", {}) or {}
                track = media.get("track", "inbound_track")
                if track != "inbound_track":
                    continue
                payload_b64 = media.get("payload") or ""
                if not payload_b64:
                    continue
                mulaw = base64.b64decode(payload_b64)
                await session.on_media(mulaw)
                continue

            if kind == "dtmf" and session is not None:
                digit = (event.get("dtmf") or {}).get("digit")
                log.info("telnyx dtmf: %s", digit)
                continue

            if kind == "mark" and session is not None:
                mark_name = (event.get("mark") or {}).get("name")
                if mark_name:
                    await session.on_mark_ack(mark_name)
                continue

            if kind == "clear":
                log.info("telnyx clear")
                continue

            if kind == "stop" and session is not None:
                log.info("telnyx stop: %s", session.session_id)
                await session.stop("stop-event")
                break

    except WebSocketDisconnect:
        if session:
            await session.stop("ws-disconnect")
        log.info("telnyx ws disconnected")
    except Exception as e:
        log.exception("telnyx ws error: %s", e)
        if session:
            await session.stop("error")


class TelnyxDialRequest(BaseModel):
    to: str = Field(..., description="E.164 phone number to call")
    from_number: Optional[str] = Field(default=None, description="Caller ID — defaults to TELNYX_PHONE_NUMBER")
    connection_id: Optional[str] = Field(default=None, description="Overrides TELNYX_APP_ID")


class TelnyxDialResponse(BaseModel):
    ok: bool
    call_control_id: Optional[str] = None
    call_leg_id: Optional[str] = None
    to: str
    from_number: str
    webhook_url: str
    error: Optional[str] = None


@router.post("/telnyx/dial", response_model=TelnyxDialResponse)
async def telnyx_dial(req: TelnyxDialRequest) -> TelnyxDialResponse:
    """Fire one outbound call via Telnyx Call Control API."""
    if not settings.telnyx_api_key:
        raise HTTPException(500, "TELNYX_API_KEY not configured")
    from_num = req.from_number or settings.telnyx_phone_number
    if not from_num:
        raise HTTPException(400, "no from_number and TELNYX_PHONE_NUMBER unset")
    connection_id = req.connection_id or settings.telnyx_app_id
    if not connection_id:
        raise HTTPException(500, "TELNYX_APP_ID (connection_id) not configured")
    if not settings.telnyx_public_url:
        raise HTTPException(500, "TELNYX_PUBLIC_URL not configured")
    webhook_url = f"{settings.telnyx_public_url.rstrip('/')}/telnyx/voice"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.telnyx.com/v2/calls",
            headers={
                "Authorization": f"Bearer {settings.telnyx_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "to": req.to,
                "from": from_num,
                "connection_id": connection_id,
                "webhook_url": webhook_url,
            },
        )
        if resp.status_code >= 400:
            log.warning("telnyx/dial %s: %s", resp.status_code, resp.text[:200])
            return TelnyxDialResponse(
                ok=False, to=req.to, from_number=from_num, webhook_url=webhook_url,
                error=f"Telnyx HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = (resp.json() or {}).get("data", {}) or {}

    log.info("telnyx/dial fired call_control_id=%s to=%s from=%s",
             data.get("call_control_id"), req.to, from_num)
    return TelnyxDialResponse(
        ok=True,
        call_control_id=data.get("call_control_id"),
        call_leg_id=data.get("call_leg_id"),
        to=req.to,
        from_number=from_num,
        webhook_url=webhook_url,
    )
