from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx

from app.core.config import settings

from ..base import STTEvent, STTProvider


log = logging.getLogger(__name__)


class DeepgramSTT(STTProvider):
    name = "deepgram"
    supports_streaming = True

    _WS_URL = "wss://api.deepgram.com/v1/listen"
    _HTTP_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self) -> None:
        self.api_key = settings.deepgram_api_key
        self.model = settings.deepgram_model or "nova-3"

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "language": "en-US",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                self._HTTP_URL,
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": mime},
                params=params,
                content=audio_bytes,
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            return data["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError):
            return ""

    async def transcribe_stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int = 16000,
        encoding: str = "linear16",
    ) -> AsyncIterator[STTEvent]:
        """Stream audio to Deepgram's WebSocket and yield STTEvents.

        Deepgram emits Results messages every ~200-300ms with is_final=False
        (interim) or is_final=True (endpointed final). We map those to
        STTEvent(kind='partial'|'final') so the caller can react to interim
        hypotheses (barge-in) and only commit finals to the brain.

        Requires the `websockets` package (already a FastAPI dep transitively)."""
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")

        try:
            import websockets
        except ImportError as e:
            raise RuntimeError(
                "Deepgram streaming needs `pip install websockets`."
            ) from e

        params = {
            "model": self.model,
            "encoding": encoding,       # linear16 | mulaw | opus | ...
            "sample_rate": str(sample_rate),
            "channels": "1",
            "smart_format": "true",
            "punctuate": "true",
            "interim_results": "true",
            "endpointing": "300",       # ms of silence before is_final=True
            "vad_events": "true",
            "language": "en-US",
        }
        url = f"{self._WS_URL}?{urlencode(params)}"
        headers = {"Authorization": f"Token {self.api_key}"}

        # We need a producer/consumer pair: one task pushes caller audio into
        # the websocket, another reads Deepgram's messages and yields events.
        # Use an asyncio.Queue to bridge websocket reads → our async generator.
        event_queue: asyncio.Queue = asyncio.Queue()

        # Support both websockets 12+ (additional_headers) and older (extra_headers)
        try:
            _ws_ctx = websockets.connect(url, additional_headers=headers)
        except TypeError:
            _ws_ctx = websockets.connect(url, extra_headers=headers)

        ws = await _ws_ctx.__aenter__()

        try:

            async def _producer():
                """Push caller audio chunks into the WS. On end-of-iteration,
                send CloseStream so Deepgram flushes remaining transcripts."""
                try:
                    async for chunk in audio_chunks:
                        if not chunk:
                            continue
                        await ws.send(chunk)
                except Exception as e:
                    log.warning("deepgram producer failed: %s", e)
                finally:
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass

            async def _consumer():
                """Read WS messages, translate to STTEvent, push to queue.

                Use explicit await ws.recv() loop instead of async for —
                websockets v15's async iterator can starve on hot paths
                where the producer holds the event loop; explicit recv()
                yields deterministically on each read."""
                try:
                    while True:
                        try:
                            raw = await ws.recv()
                        except websockets.ConnectionClosed:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        mtype = msg.get("type")
                        if mtype == "SpeechStarted":
                            await event_queue.put(STTEvent(kind="speech_start"))
                        elif mtype == "UtteranceEnd":
                            await event_queue.put(STTEvent(kind="speech_end"))
                        elif mtype == "Results":
                            try:
                                alt = msg["channel"]["alternatives"][0]
                                text = alt.get("transcript", "").strip()
                                is_final = bool(msg.get("is_final"))
                            except (KeyError, IndexError):
                                continue
                            if text:
                                await event_queue.put(STTEvent(
                                    kind="final" if is_final else "partial",
                                    text=text, is_final=is_final,
                                ))
                        elif mtype == "Metadata":
                            continue  # opening handshake — ignore
                except Exception as e:
                    log.warning("deepgram consumer failed: %s", e)
                finally:
                    await event_queue.put(None)  # sentinel

            producer = asyncio.create_task(_producer())
            consumer = asyncio.create_task(_consumer())
            try:
                while True:
                    ev = await event_queue.get()
                    if ev is None:
                        break
                    yield ev
            finally:
                producer.cancel()
                consumer.cancel()
        finally:
            await _ws_ctx.__aexit__(None, None, None)
