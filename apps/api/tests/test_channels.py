from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from packages.channels import MessageKind
from packages.channels.pipeline import VoiceMessagePipeline
from packages.channels.telegram import TelegramChannel
from packages.channels.whatsapp import WhatsAppChannel


WA_TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "biz-1",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1555", "phone_number_id": "pn1"},
                "messages": [{
                    "from": "15551234567",
                    "id": "wamid.abc",
                    "type": "text",
                    "text": {"body": "hi book me tomorrow"},
                }],
            },
        }],
    }],
}


WA_VOICE_PAYLOAD = {
    "entry": [{
        "changes": [{
            "value": {
                "messages": [{
                    "from": "15551234567",
                    "id": "wamid.def",
                    "type": "voice",
                    "voice": {"id": "media-id-42", "mime_type": "audio/ogg"},
                }],
            },
        }],
    }],
}


TG_TEXT_PAYLOAD = {
    "update_id": 1,
    "message": {
        "message_id": 10,
        "chat": {"id": 987654, "type": "private"},
        "text": "book me at 10am",
    },
}


TG_VOICE_PAYLOAD = {
    "update_id": 2,
    "message": {
        "message_id": 11,
        "chat": {"id": 987654, "type": "private"},
        "voice": {"file_id": "AwACAg..."},
    },
}


@pytest.mark.asyncio
async def test_whatsapp_parse_text():
    ch = WhatsAppChannel(access_token="fake", phone_number_id="pn1")
    msgs = await ch.parse_webhook(WA_TEXT_PAYLOAD)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.channel == "whatsapp"
    assert m.external_user_id == "15551234567"
    assert m.kind == MessageKind.TEXT
    assert m.text == "hi book me tomorrow"


@pytest.mark.asyncio
async def test_whatsapp_parse_voice_downloads_media():
    ch = WhatsAppChannel(access_token="fake", phone_number_id="pn1")
    fake_bytes = b"OggS-fake-audio-body"
    with patch.object(ch, "_download_media", new=AsyncMock(return_value=(fake_bytes, "audio/ogg"))):
        msgs = await ch.parse_webhook(WA_VOICE_PAYLOAD)
    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.VOICE
    assert msgs[0].audio_bytes == fake_bytes
    assert msgs[0].audio_mime == "audio/ogg"


@pytest.mark.asyncio
async def test_telegram_parse_text():
    ch = TelegramChannel(bot_token="fake")
    msgs = await ch.parse_webhook(TG_TEXT_PAYLOAD)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.channel == "telegram"
    assert m.external_user_id == "987654"
    assert m.kind == MessageKind.TEXT
    assert m.text == "book me at 10am"


@pytest.mark.asyncio
async def test_telegram_parse_voice_downloads_file():
    ch = TelegramChannel(bot_token="fake")
    with patch.object(ch, "_download_voice", new=AsyncMock(return_value=(b"opus", "audio/ogg"))):
        msgs = await ch.parse_webhook(TG_VOICE_PAYLOAD)
    assert msgs[0].kind == MessageKind.VOICE
    assert msgs[0].audio_bytes == b"opus"


@pytest.mark.asyncio
async def test_session_key_is_stable_per_user():
    ch = TelegramChannel(bot_token="fake")
    msgs = await ch.parse_webhook(TG_TEXT_PAYLOAD)
    assert ch.session_key(msgs[0]) == "telegram_987654"


@pytest.mark.asyncio
async def test_pipeline_runs_text_message_end_to_end():
    ch = TelegramChannel(bot_token="fake")

    class FakeSTT:
        async def transcribe(self, audio_bytes, sample_rate=16000, mime="audio/wav"):
            return "should not be called"

    class FakeTTS:
        async def synthesize(self, text, voice=None):
            return b"WAV-BYTES", "audio/wav"

    sent_calls: list[dict] = []

    async def fake_send_text(to, text):
        sent_calls.append({"kind": "text", "to": to, "text": text})
        return {"ok": True}

    async def fake_send_voice(to, audio, mime):
        sent_calls.append({"kind": "voice", "to": to, "mime": mime, "len": len(audio)})
        return {"ok": True}

    async def brain(session_id, user_text):
        return {"reply": f"got: {user_text}", "extracted": {}, "tool_results": [], "escalated": False, "status": "active"}

    ch.send_text = fake_send_text  # type: ignore
    ch.send_voice = fake_send_voice  # type: ignore

    pipeline = VoiceMessagePipeline(channel=ch, stt=FakeSTT(), tts=FakeTTS(), brain_runner=brain)
    msgs = await ch.parse_webhook(TG_TEXT_PAYLOAD)
    result = await pipeline.handle(msgs[0])

    assert result["transcript"] == "book me at 10am"
    assert result["reply"] == "got: book me at 10am"
    kinds = [c["kind"] for c in sent_calls]
    assert "text" in kinds and "voice" in kinds


@pytest.mark.asyncio
async def test_pipeline_runs_voice_message_end_to_end():
    ch = TelegramChannel(bot_token="fake")

    class FakeSTT:
        async def transcribe(self, audio_bytes, sample_rate=16000, mime="audio/wav"):
            return "book me tomorrow please"

    class FakeTTS:
        async def synthesize(self, text, voice=None):
            return b"WAV", "audio/wav"

    async def brain(session_id, user_text):
        assert user_text == "book me tomorrow please"
        return {"reply": "sure, what time?", "extracted": {}, "tool_results": [], "escalated": False, "status": "active"}

    ch.send_text = AsyncMock(return_value={"ok": True})  # type: ignore
    ch.send_voice = AsyncMock(return_value={"ok": True})  # type: ignore

    with patch.object(ch, "_download_voice", new=AsyncMock(return_value=(b"opus-audio", "audio/ogg"))):
        msgs = await ch.parse_webhook(TG_VOICE_PAYLOAD)

    pipeline = VoiceMessagePipeline(channel=ch, stt=FakeSTT(), tts=FakeTTS(), brain_runner=brain)
    result = await pipeline.handle(msgs[0])

    assert result["transcript"] == "book me tomorrow please"
    assert result["reply"] == "sure, what time?"
