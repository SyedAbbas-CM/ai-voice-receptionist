"""SignalWire Media Streams handler.

SignalWire is Twilio-API-compatible: same TwiML, same Media Streams
websocket protocol, same HMAC-SHA1 signature scheme (header renamed to
X-SignalWire-Signature).  This module mirrors twilio.py 1:1 for the
inbound-call path and reuses TwilioActorSession directly for the WS
handler — the wire bytes are byte-identical.

Endpoints:
    POST /signalwire/voice   - TwiML that opens a Media Stream
    WS   /signalwire/stream  - Media Streams handler (delegates to actor)
    POST /signalwire/dial    - Outbound call via SignalWire LaML REST

Env:
    SIGNALWIRE_SPACE_URL       (e.g. "voiceops.signalwire.com")
    SIGNALWIRE_PROJECT_ID
    SIGNALWIRE_TOKEN
    SIGNALWIRE_PHONE_NUMBER
    TWILIO_PUBLIC_URL          (shared — used to build wss:// URL)
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings


log = logging.getLogger(__name__)
router = APIRouter(tags=["signalwire"])


def _twiml_stream_response(public_url: str) -> str:
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/signalwire/stream"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>"""


async def _verify_signalwire_signature(request: Request) -> None:
    """Same HMAC-SHA1(url + sorted-form-values) scheme as Twilio, just a
    different header name and token.  When SIGNALWIRE_SIGNATURE_ENFORCE=
    false, verification is skipped (dev only)."""
    import os
    if os.environ.get("SIGNALWIRE_SIGNATURE_ENFORCE", "true").lower() in ("0", "false", "no"):
        return
    token = settings.signalwire_token or ""
    if not token:
        raise HTTPException(500, "SignalWire signature enforcement is on but SIGNALWIRE_TOKEN is not configured")
    signature = request.headers.get("X-SignalWire-Signature", "")
    if not signature:
        raise HTTPException(401, "missing X-SignalWire-Signature")
    public_base = (settings.twilio_public_url or "").rstrip("/")
    if not public_base:
        raise HTTPException(500, "TWILIO_PUBLIC_URL not configured for signature verification")
    url = f"{public_base}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    form = await request.form()
    payload = url + "".join(f"{k}{form.get(k)}" for k in sorted(form.keys()))
    import base64 as _b64, hashlib, hmac
    expected = _b64.b64encode(
        hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid SignalWire signature")


@router.post("/signalwire/voice")
async def signalwire_voice_webhook(request: Request) -> Response:
    await _verify_signalwire_signature(request)
    public = settings.twilio_public_url or ""
    if not public:
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Sorry, this number is not configured. Please set TWILIO_PUBLIC_URL.</Say><Hangup/></Response>""",
            media_type="application/xml",
        )
    return Response(content=_twiml_stream_response(public), media_type="application/xml")


@router.websocket("/signalwire/stream")
async def signalwire_stream(ws: WebSocket) -> None:
    """SignalWire Media Streams frames are byte-identical to Twilio's, so
    we route directly through TwilioActorSession.  Falls back to a minimal
    inline loop only if twilio_use_actor is off — the legacy TwilioStream
    Session is Twilio-branded and we don't rebuild it here."""
    await ws.accept()

    from app.routes.twilio_actor import TwilioActorSession

    session: Optional[TwilioActorSession] = None
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "connected":
                log.info("signalwire connected: %s", event.get("protocol"))
                continue

            if kind == "start":
                stream_sid = event["start"]["streamSid"]
                call_sid = event["start"].get("callSid") or f"call_{uuid.uuid4().hex[:8]}"
                session = TwilioActorSession(
                    ws=ws,
                    stream_sid=stream_sid,
                    call_id=call_sid,
                    tenant_id="default",
                    session_id=f"signalwire_{call_sid}",
                )
                log.info("signalwire start: %s (%s)", call_sid, stream_sid)
                await session.start()
                continue

            if kind == "media" and session is not None:
                mulaw = base64.b64decode(event["media"]["payload"])
                await session.on_media(mulaw)
                continue

            if kind == "mark" and session is not None:
                mark_name = event.get("mark", {}).get("name")
                if mark_name:
                    await session.on_mark_ack(mark_name)
                continue

            if kind == "stop" and session is not None:
                log.info("signalwire stop: %s", session.session_id)
                await session.stop("stop-event")
                break

    except WebSocketDisconnect:
        if session:
            await session.stop("ws-disconnect")
        log.info("signalwire ws disconnected")
    except Exception as e:
        log.exception("signalwire ws error: %s", e)
        if session:
            await session.stop("error")


class DialRequest(BaseModel):
    to: str = Field(..., description="E.164 phone number to call")
    from_number: Optional[str] = Field(
        default=None,
        description="Caller ID.  Defaults to settings.signalwire_phone_number.",
    )
    business_id: Optional[str] = Field(default=None)


class DialResponse(BaseModel):
    ok: bool
    call_sid: Optional[str] = None
    status: Optional[str] = None
    to: str
    from_number: str
    voice_url: str
    error: Optional[str] = None


@router.post("/signalwire/dial", response_model=DialResponse)
async def dial(req: DialRequest) -> DialResponse:
    """Fire one outbound SignalWire call.  Endpoint shape mirrors
    Twilio's Calls.json exactly — same params, same auth style."""
    if not settings.signalwire_space_url or not settings.signalwire_project_id or not settings.signalwire_token:
        raise HTTPException(500, "SignalWire credentials not configured")

    from_num = req.from_number or settings.signalwire_phone_number
    if not from_num:
        raise HTTPException(400, "no from_number and SIGNALWIRE_PHONE_NUMBER unset")

    if not settings.twilio_public_url:
        raise HTTPException(500, "TWILIO_PUBLIC_URL not configured — agent voice_url unknown")

    voice_url = f"{settings.twilio_public_url.rstrip('/')}/signalwire/voice"
    status_url = f"{settings.twilio_public_url.rstrip('/')}/signalwire/status"

    auth = base64.b64encode(
        f"{settings.signalwire_project_id}:{settings.signalwire_token}".encode(),
    ).decode()

    space = settings.signalwire_space_url.rstrip("/")
    calls_url = (
        f"https://{space}/api/laml/2010-04-01/Accounts/"
        f"{settings.signalwire_project_id}/Calls.json"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            calls_url,
            headers={"Authorization": f"Basic {auth}"},
            data={
                "To": req.to,
                "From": from_num,
                "Url": voice_url,
                "Method": "POST",
                "StatusCallback": status_url,
                "StatusCallbackMethod": "POST",
                "StatusCallbackEvent": "initiated ringing answered completed",
            },
        )
        if resp.status_code >= 400:
            log.warning("signalwire/dial %s: %s", resp.status_code, resp.text[:200])
            return DialResponse(
                ok=False, to=req.to, from_number=from_num, voice_url=voice_url,
                error=f"SignalWire HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()

    log.info("signalwire/dial fired call_sid=%s to=%s from=%s",
             data.get("sid"), req.to, from_num)
    return DialResponse(
        ok=True, call_sid=data.get("sid"), status=data.get("status"),
        to=req.to, from_number=from_num, voice_url=voice_url,
    )
