"""Telegram Bot API channel.

Docs: https://core.telegram.org/bots/api

Setup:
  1. @BotFather -> /newbot -> save the token.
  2. Set webhook: POST https://api.telegram.org/bot<TOKEN>/setWebhook
     with url=https://<your-backend>/channels/telegram/webhook

Env:
  TELEGRAM_BOT_TOKEN
"""
from __future__ import annotations

import httpx

from .base import Channel, IncomingMessage, MessageKind


TG_BASE = "https://api.telegram.org"


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, bot_token: str, timeout: float = 30.0) -> None:
        if not bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN required")
        self.bot_token = bot_token
        self.timeout = timeout

    @property
    def _api(self) -> str:
        return f"{TG_BASE}/bot{self.bot_token}"

    @property
    def _file_api(self) -> str:
        return f"{TG_BASE}/file/bot{self.bot_token}"

    async def _download_voice(self, file_id: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            meta = await client.get(f"{self._api}/getFile", params={"file_id": file_id})
            meta.raise_for_status()
            file_path = meta.json()["result"]["file_path"]
            audio = await client.get(f"{self._file_api}/{file_path}")
            audio.raise_for_status()
            # Telegram voice messages are OGG/Opus by default
            return audio.content, "audio/ogg"

    async def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        msg = payload.get("message") or payload.get("edited_message") or {}
        if not msg:
            return []

        chat_id = str((msg.get("chat") or {}).get("id") or "")
        msg_id = str(msg.get("message_id") or "")

        kind = MessageKind.UNKNOWN
        text = None
        audio_bytes = None
        audio_mime = None

        if "voice" in msg:
            kind = MessageKind.VOICE
            audio_bytes, audio_mime = await self._download_voice(msg["voice"]["file_id"])
        elif "audio" in msg:
            kind = MessageKind.VOICE
            audio_bytes, audio_mime = await self._download_voice(msg["audio"]["file_id"])
        elif "text" in msg:
            kind = MessageKind.TEXT
            text = msg["text"]
        elif "photo" in msg:
            kind = MessageKind.IMAGE

        return [IncomingMessage(
            channel=self.name,
            external_user_id=chat_id,
            external_message_id=msg_id,
            kind=kind,
            text=text,
            audio_bytes=audio_bytes,
            audio_mime=audio_mime,
            raw=msg,
        )]

    async def send_text(self, to: str, text: str) -> dict:
        payload = {"chat_id": to, "text": text[:4096]}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self._api}/sendMessage", json=payload)
            r.raise_for_status()
            return r.json()

    async def send_voice(self, to: str, audio_bytes: bytes, mime: str) -> dict:
        # Telegram sendVoice expects OGG/Opus. WAV/MP3 will be rejected or fall back to sendAudio.
        ext = "ogg" if "ogg" in mime else "wav" if "wav" in mime else "mp3"
        endpoint = "sendVoice" if ext == "ogg" else "sendAudio"
        files = {"voice" if endpoint == "sendVoice" else "audio": (f"reply.{ext}", audio_bytes, mime)}
        data = {"chat_id": to}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self._api}/{endpoint}", data=data, files=files)
            r.raise_for_status()
            return r.json()
