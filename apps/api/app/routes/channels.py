"""Channel webhooks: WhatsApp and Telegram.

Same brain, same session manager, same tools — just different transport."""
from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.core import session_manager
from app.core.config import settings
from app.providers import get_stt, get_tts
from packages.channels import Channel, VoiceMessagePipeline
from packages.channels.telegram import TelegramChannel
from packages.channels.whatsapp import WhatsAppChannel


log = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])


@lru_cache(maxsize=1)
def _whatsapp_channel() -> WhatsAppChannel:
    return WhatsAppChannel(
        access_token=settings.whatsapp_access_token or "",
        phone_number_id=settings.whatsapp_phone_number_id or "",
        graph_version=settings.whatsapp_graph_version,
    )


@lru_cache(maxsize=1)
def _telegram_channel() -> TelegramChannel:
    return TelegramChannel(bot_token=settings.telegram_bot_token or "")


async def _brain_runner(session_id: str, user_text: str) -> dict:
    """Bridge the channel pipeline to the session manager's brain."""
    handle = session_manager.get_session(session_id)
    if handle is None:
        state, brain = session_manager.start_session_with_id(session_id)
    else:
        state, brain = handle
    return await session_manager.run_user_turn(state, brain, user_text)


def _build_pipeline(channel: Channel) -> VoiceMessagePipeline:
    return VoiceMessagePipeline(
        channel=channel,
        stt=get_stt(),
        tts=get_tts(),
        brain_runner=_brain_runner,
        send_voice_reply=True,
        also_send_text=True,
    )


# ─── WhatsApp ─────────────────────────────────────────────────────────────────

@router.get("/whatsapp/webhook", response_class=PlainTextResponse)
async def whatsapp_verify(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    if mode == "subscribe" and token and token == (settings.whatsapp_verify_token or ""):
        return challenge or ""
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> dict:
    payload = await request.json()
    channel = _whatsapp_channel()
    pipeline = _build_pipeline(channel)
    results = []
    for msg in await channel.parse_webhook(payload):
        try:
            results.append(await pipeline.handle(msg))
        except Exception as e:
            log.exception("whatsapp handle failed: %s", e)
            results.append({"ok": False, "error": str(e)})
    return {"ok": True, "results": results}


# ─── Telegram ─────────────────────────────────────────────────────────────────

@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    payload = await request.json()
    channel = _telegram_channel()
    pipeline = _build_pipeline(channel)
    results = []
    for msg in await channel.parse_webhook(payload):
        try:
            results.append(await pipeline.handle(msg))
        except Exception as e:
            log.exception("telegram handle failed: %s", e)
            results.append({"ok": False, "error": str(e)})
    return {"ok": True, "results": results}
