from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    IMAGE = "image"
    UNKNOWN = "unknown"


@dataclass
class IncomingMessage:
    """Channel-normalized inbound message. One shape whether it came from
    WhatsApp, Telegram, IG, etc — the brain doesn't care which."""
    channel: str                    # "whatsapp" | "telegram" | ...
    external_user_id: str           # phone / telegram user id / whatever
    external_message_id: str
    kind: MessageKind
    text: Optional[str] = None      # populated for TEXT, or after STT for VOICE
    audio_bytes: Optional[bytes] = None
    audio_mime: Optional[str] = None
    raw: Optional[dict] = None      # original webhook payload for debugging


class Channel(ABC):
    """One implementation per messaging platform. Handles inbound webhook
    parsing (returns IncomingMessage) and outbound send (text + voice)."""

    name: str = "base"

    @abstractmethod
    async def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        ...

    @abstractmethod
    async def send_text(self, to: str, text: str) -> dict:
        ...

    @abstractmethod
    async def send_voice(self, to: str, audio_bytes: bytes, mime: str) -> dict:
        ...

    def session_key(self, msg: IncomingMessage) -> str:
        """Deterministic session id per (channel, user). One conversation
        thread per user per channel."""
        return f"{msg.channel}_{msg.external_user_id}"
