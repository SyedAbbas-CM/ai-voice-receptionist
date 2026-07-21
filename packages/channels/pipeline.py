from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional, Protocol

from .base import Channel, IncomingMessage, MessageKind

log = logging.getLogger(__name__)


class STTLike(Protocol):
    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str: ...


class TTSLike(Protocol):
    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]: ...


BrainRunner = Callable[[str, str], Awaitable[dict]]
"""Function signature: (session_id, user_text) -> reply payload dict with a 'reply' key."""


class VoiceMessagePipeline:
    """Shared pipeline used by every channel. Given an IncomingMessage,
    figures out whether to transcribe, runs the brain, sends the reply
    (text and/or voice) back over the channel."""

    def __init__(
        self,
        channel: Channel,
        stt: Optional[STTLike],
        tts: Optional[TTSLike],
        brain_runner: BrainRunner,
        send_voice_reply: bool = True,
        also_send_text: bool = True,
    ) -> None:
        self.channel = channel
        self.stt = stt
        self.tts = tts
        self.brain_runner = brain_runner
        self.send_voice_reply = send_voice_reply
        self.also_send_text = also_send_text

    async def handle(self, msg: IncomingMessage) -> dict:
        user_text = msg.text or ""

        if msg.kind == MessageKind.VOICE:
            if not msg.audio_bytes:
                log.warning("voice message without audio_bytes on %s", msg.channel)
                return {"ok": False, "reason": "no audio"}
            if not self.stt:
                log.error("channel %s got voice but no STT configured", msg.channel)
                return {"ok": False, "reason": "no stt configured"}
            user_text = await self.stt.transcribe(
                msg.audio_bytes, mime=msg.audio_mime or "audio/ogg"
            )

        if not user_text.strip():
            return {"ok": False, "reason": "empty user text"}

        session_id = self.channel.session_key(msg)
        payload = await self.brain_runner(session_id, user_text)
        reply = (payload or {}).get("reply") or ""

        results: dict = {"transcript": user_text, "reply": reply, "sent": []}

        if self.send_voice_reply and self.tts and reply:
            try:
                audio_bytes, mime = await self.tts.synthesize(reply)
                if mime != "text/x-browser-speak" and audio_bytes:
                    r = await self.channel.send_voice(msg.external_user_id, audio_bytes, mime)
                    results["sent"].append({"kind": "voice", "result": r})
            except Exception as e:
                log.warning("channel %s send_voice failed: %s", self.channel.name, e)

        if self.also_send_text and reply:
            try:
                r = await self.channel.send_text(msg.external_user_id, reply)
                results["sent"].append({"kind": "text", "result": r})
            except Exception as e:
                log.warning("channel %s send_text failed: %s", self.channel.name, e)

        return results
