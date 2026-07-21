"""WhatsApp Business Cloud API channel.

Docs:
  - https://developers.facebook.com/docs/whatsapp/cloud-api/messages/audio-messages/

Setup:
  1. Meta for Developers -> WhatsApp product -> add a phone number.
  2. Grab a permanent access token via a System User (Meta Business Suite).
  3. Set webhook URL to https://<your-backend>/channels/whatsapp/webhook
     and verify token to WHATSAPP_VERIFY_TOKEN.
  4. Subscribe to `messages` field.

Env:
  WHATSAPP_ACCESS_TOKEN
  WHATSAPP_PHONE_NUMBER_ID
  WHATSAPP_VERIFY_TOKEN            (for webhook GET verification)
  WHATSAPP_GRAPH_VERSION=v22.0

Note on inbound voice: Cloud API delivers only a media `id` in the webhook.
We must call GET /{media_id} to get a short-lived URL, then GET that URL
with the auth token to fetch the actual audio bytes (usually OGG/Opus).
"""
from __future__ import annotations

import httpx

from .base import Channel, IncomingMessage, MessageKind


GRAPH_BASE = "https://graph.facebook.com"


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_version: str = "v22.0",
        timeout: float = 30.0,
    ) -> None:
        if not access_token or not phone_number_id:
            raise RuntimeError("WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID required")
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.graph_version = graph_version
        self.timeout = timeout

    @property
    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _download_media(self, media_id: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            meta = await client.get(
                f"{GRAPH_BASE}/{self.graph_version}/{media_id}",
                headers=self._auth_headers,
            )
            meta.raise_for_status()
            info = meta.json()
            url = info["url"]
            mime = info.get("mime_type", "audio/ogg")
            audio = await client.get(url, headers=self._auth_headers)
            audio.raise_for_status()
            return audio.content, mime

    async def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        """Extract inbound messages from a WhatsApp Cloud API webhook payload."""
        out: list[IncomingMessage] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for m in value.get("messages") or []:
                    kind_raw = m.get("type")
                    from_id = m.get("from")
                    msg_id = m.get("id")

                    kind = MessageKind.UNKNOWN
                    text = None
                    audio_bytes = None
                    audio_mime = None

                    if kind_raw == "text":
                        kind = MessageKind.TEXT
                        text = (m.get("text") or {}).get("body")
                    elif kind_raw in ("audio", "voice"):
                        kind = MessageKind.VOICE
                        media_id = (m.get(kind_raw) or {}).get("id")
                        if media_id:
                            audio_bytes, audio_mime = await self._download_media(media_id)
                    elif kind_raw == "image":
                        kind = MessageKind.IMAGE

                    out.append(IncomingMessage(
                        channel=self.name,
                        external_user_id=from_id,
                        external_message_id=msg_id,
                        kind=kind,
                        text=text,
                        audio_bytes=audio_bytes,
                        audio_mime=audio_mime,
                        raw=m,
                    ))
        return out

    async def send_text(self, to: str, text: str) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096]},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{GRAPH_BASE}/{self.graph_version}/{self.phone_number_id}/messages",
                headers={**self._auth_headers, "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    async def _upload_media(self, audio_bytes: bytes, mime: str) -> str:
        # WhatsApp voice notes must be OGG/Opus for the voice-message UX.
        # Any audio/* mime type still uploads; recipient just sees a playable audio bubble.
        files = {"file": ("audio", audio_bytes, mime)}
        data = {"messaging_product": "whatsapp", "type": mime}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{GRAPH_BASE}/{self.graph_version}/{self.phone_number_id}/media",
                headers=self._auth_headers,
                data=data,
                files=files,
            )
            r.raise_for_status()
            return r.json()["id"]

    async def send_voice(self, to: str, audio_bytes: bytes, mime: str) -> dict:
        media_id = await self._upload_media(audio_bytes, mime)
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id, "voice": True},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{GRAPH_BASE}/{self.graph_version}/{self.phone_number_id}/messages",
                headers={**self._auth_headers, "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()
