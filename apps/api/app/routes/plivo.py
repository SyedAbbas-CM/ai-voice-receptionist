"""Plivo Audio Streams handler.

Three endpoints:
  POST /plivo/voice          - PlivoXML for incoming call.  Opens an Audio
                                Stream to /plivo/stream (bidirectional,
                                µ-law 8kHz, binary frames).
  WS   /plivo/stream         - Audio Stream websocket.  Binary µ-law frames
                                in; binary µ-law frames out.  Wraps the same
                                TwilioActorSession - audio-format-agnostic.
  POST /plivo/dial           - Outbound trigger via Plivo REST /Call/.

Setup:
  1. Create a Plivo application pointing "Answer URL" at
       POST https://<your-tunnel>/plivo/voice
  2. Assign a Plivo number to that application.
  3. Env:
       PLIVO_AUTH_ID          (starts with "MA")
       PLIVO_AUTH_TOKEN
       PLIVO_PHONE_NUMBER     (E.164 from-number for outbound)
       PLIVO_APP_ID           (their Application UUID)
       PLIVO_PUBLIC_URL       (tunnel URL - used for the wss:// stream URL)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings


log = logging.getLogger(__name__)
router = APIRouter(tags=["plivo"])


def _plivoxml_stream_response(public_url: str) -> str:
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/plivo/stream"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Stream bidirectional="true" contentType="audio/x-mulaw" sampleRate="8000" streamTimeout="86400" keepCallAlive="true">{ws_url}</Stream>
</Response>"""


async def _verify_plivo_signature(request: Request) -> None:
    """Plivo signature V2: HMAC-SHA256 over (url + nonce) using auth token,
    base64 encoded, delivered via X-Plivo-Signature-V2 + X-Plivo-Signature-V2-Nonce.
    2026-08-08 SEC FIX: default is now TRUE (fail closed).  Set
    PLIVO_SIGNATURE_ENFORCE=false only for local dev tunnels."""
    if os.environ.get("PLIVO_SIGNATURE_ENFORCE", "true").lower() in ("0", "false", "no"):
        return
    token = settings.plivo_auth_token or ""
    if not token:
        raise HTTPException(500, "Plivo signature enforcement is on but PLIVO_AUTH_TOKEN is not configured")
    signature = request.headers.get("X-Plivo-Signature-V2", "")
    nonce = request.headers.get("X-Plivo-Signature-V2-Nonce", "")
    if not signature or not nonce:
        raise HTTPException(401, "missing X-Plivo-Signature-V2")
    public_base = (settings.plivo_public_url or "").rstrip("/")
    if not public_base:
        raise HTTPException(500, "PLIVO_PUBLIC_URL not configured for signature verification")
    url = f"{public_base}{request.url.path}"
    payload = (url + nonce).encode("utf-8")
    expected = base64.b64encode(
        hmac.new(token.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid Plivo signature")


@router.post("/plivo/voice")
async def plivo_voice_webhook(request: Request) -> Response:
    await _verify_plivo_signature(request)
    public = settings.plivo_public_url or ""
    if not public:
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Speak>Sorry, this number is not configured. Please set PLIVO_PUBLIC_URL.</Speak><Hangup/></Response>""",
            media_type="application/xml",
        )
    return Response(content=_plivoxml_stream_response(public), media_type="application/xml")


class _PlivoWSProxy:
    """Adapts the Twilio-shaped ws.send_text(json) calls that
    TwilioActorSession makes into Plivo-native binary µ-law frames.

    Twilio path sends three event types:
      * media  {"event":"media","media":{"payload":"<b64 mulaw>"}}
      * mark   {"event":"mark","mark":{"name":"..."}}
      * clear  {"event":"clear"}

    Plivo's bidirectional stream accepts:
      * binary µ-law 8kHz frames back on the WS (audio)
      * JSON control messages: {"event":"clearAudio"} to flush
    Mark acks are inferred locally when the audio has been sent,
    since Plivo has no equivalent to Twilio's mark webhook."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    @property
    def real_ws(self) -> WebSocket:
        return self._ws

    async def send_text(self, text: str) -> None:
        try:
            event = json.loads(text)
        except Exception:
            await self._ws.send_text(text)
            return
        kind = event.get("event")
        if kind == "media":
            payload_b64 = ((event.get("media") or {}).get("payload") or "")
            if payload_b64:
                await self._ws.send_bytes(base64.b64decode(payload_b64))
            return
        if kind == "clear":
            try:
                await self._ws.send_text(json.dumps({"event": "clearAudio"}))
            except Exception:
                pass
            return
        if kind == "mark":
            return
        await self._ws.send_text(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def close(self, *a, **kw) -> None:
        await self._ws.close(*a, **kw)


@router.websocket("/plivo/stream")
async def plivo_stream(ws: WebSocket) -> None:
    """Plivo Audio Stream WS.  Two wire modes supported:
      * binary: raw µ-law 8kHz bytes on the WS (preferred, lower overhead)
      * json:   {"event":"media","media":{"payload":"<b64>"}} - Twilio-shaped
    We accept both; outbound frames are always sent as binary."""
    await ws.accept()

    from app.routes.twilio_actor import TwilioActorSession

    proxy = _PlivoWSProxy(ws)
    session: Optional[TwilioActorSession] = None
    stream_id: Optional[str] = None
    call_id: Optional[str] = None

    async def _ensure_session() -> TwilioActorSession:
        nonlocal session, stream_id, call_id
        if session is not None:
            return session
        if stream_id is None:
            stream_id = f"plivo_{uuid.uuid4().hex[:12]}"
        if call_id is None:
            call_id = stream_id
        session = TwilioActorSession(
            ws=proxy, stream_sid=stream_id, call_id=call_id,
            tenant_id="default",
        )
        await session.start()
        return session

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            data = msg.get("bytes")
            text = msg.get("text")

            if data is not None:
                s = await _ensure_session()
                await s.on_media(data)
                continue

            if text is None:
                continue

            try:
                event = json.loads(text)
            except Exception:
                log.warning("plivo ws non-JSON text frame ignored")
                continue

            kind = event.get("event")

            if kind in ("start", "connected"):
                if kind == "start":
                    start = event.get("start") or {}
                    stream_id = (
                        start.get("streamId")
                        or event.get("streamId")
                        or f"plivo_{uuid.uuid4().hex[:12]}"
                    )
                    call_id = start.get("callId") or event.get("callId") or stream_id
                    await _ensure_session()
                    log.info("plivo start: %s (%s)", call_id, stream_id)
                continue

            if kind == "media":
                s = await _ensure_session()
                media = event.get("media") or {}
                payload_b64 = media.get("payload") or ""
                if payload_b64:
                    await s.on_media(base64.b64decode(payload_b64))
                continue

            if kind in ("stop", "hangup", "disconnected") and session is not None:
                log.info("plivo stop: %s", session.session_id)
                await session.stop(reason="hangup")
                break

    except WebSocketDisconnect:
        if session:
            await session.stop(reason="ws_disconnect")
        log.info("plivo ws disconnected")
    except Exception as e:
        log.exception("plivo ws error: %s", e)
        if session:
            try:
                await session.stop(reason="error")
            except Exception:
                pass


class PlivoDialRequest(BaseModel):
    to: str = Field(..., description="E.164 phone number to call")
    from_number: Optional[str] = Field(
        default=None, description="Caller ID.  Defaults to settings.plivo_phone_number.",
    )


class PlivoDialResponse(BaseModel):
    ok: bool
    request_uuid: Optional[str] = None
    message: Optional[str] = None
    to: str
    from_number: str
    answer_url: str
    error: Optional[str] = None


@router.post("/plivo/dial", response_model=PlivoDialResponse)
async def plivo_dial(req: PlivoDialRequest) -> PlivoDialResponse:
    """Fire ONE outbound Plivo call.  The agent takes over on answer."""
    if not settings.plivo_auth_id or not settings.plivo_auth_token:
        raise HTTPException(500, "Plivo credentials not configured")

    from_num = req.from_number or settings.plivo_phone_number
    if not from_num:
        raise HTTPException(400, "no from_number and PLIVO_PHONE_NUMBER unset")

    if not settings.plivo_public_url:
        raise HTTPException(500, "PLIVO_PUBLIC_URL not configured - agent answer_url unknown")

    answer_url = f"{settings.plivo_public_url.rstrip('/')}/plivo/voice"

    auth = base64.b64encode(
        f"{settings.plivo_auth_id}:{settings.plivo_auth_token}".encode(),
    ).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.plivo.com/v1/Account/{settings.plivo_auth_id}/Call/",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_num,
                "to": req.to,
                "answer_url": answer_url,
                "answer_method": "POST",
            },
        )
        if resp.status_code >= 400:
            log.warning("plivo/dial Plivo %s: %s", resp.status_code, resp.text[:200])
            return PlivoDialResponse(
                ok=False, to=req.to, from_number=from_num, answer_url=answer_url,
                error=f"Plivo HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()

    log.info("plivo/dial fired request_uuid=%s to=%s from=%s",
             data.get("request_uuid"), req.to, from_num)
    return PlivoDialResponse(
        ok=True,
        request_uuid=data.get("request_uuid"),
        message=data.get("message"),
        to=req.to,
        from_number=from_num,
        answer_url=answer_url,
    )
