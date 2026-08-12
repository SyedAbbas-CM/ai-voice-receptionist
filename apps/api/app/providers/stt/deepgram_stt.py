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


# S13-B: keyterm boost list for Deepgram Nova-3.  Boost weights make
# Deepgram prefer these tokens when acoustic evidence is ambiguous.
# Add specialty vocabulary here; move to per-tenant config once we
# have multiple verticals in production.
_DENTAL_KEYTERMS: tuple[str, ...] = (
    # Dental services
    "implant", "implants", "tooth implant", "dental implant",
    "cleaning", "prophy", "prophylaxis",
    "crown", "crowns", "bridge", "veneer", "veneers",
    "root canal", "endodontic", "endo",
    "extraction", "wisdom tooth", "wisdom teeth",
    "filling", "cavity", "cavities",
    "orthodontics", "braces", "invisalign",
    "periodontal", "gum disease", "gingivitis",
    "whitening", "bleaching",
    "denture", "dentures",
    "x-ray", "x-rays", "panoramic",
    # Business terms
    "consultation", "checkup", "exam", "hygienist",
    "appointment", "reschedule", "cancel",
    "insurance", "copay", "deductible",
    # Sample business names
    "Smile Dental", "Cedar Ridge",
)


class DeepgramSTT(STTProvider):
    name = "deepgram"
    supports_streaming = True

    _WS_URL = "wss://api.deepgram.com/v1/listen"
    _HTTP_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, language: Optional[str] = None) -> None:
        self.api_key = settings.deepgram_api_key
        self.model = settings.deepgram_model or "nova-3"
        # 2026-08-10: per-language STT.  Nova-3 supports en, nl, hi, ur,
        # es, fr, de, pt, ja, ko + many more and code-switching between
        # them.  Business profile picks the language for the tenant;
        # defaults to en-US.  When switching Nova-3 → Flux, this same
        # constructor param carries over unchanged.
        self.language = (language or settings.deepgram_language or "en-US").strip()

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "language": self.language,
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

        params = [
            ("model", self.model),
            ("encoding", encoding),
            ("sample_rate", str(sample_rate)),
            ("channels", "1"),
            ("smart_format", "true"),
            ("punctuate", "true"),
            ("interim_results", "true"),
            # 2026-08-13: back to 150ms after adding transcript-superset
            # dedupe in twilio_actor._run_brain_from_text.  The dedupe
            # catches the "Yeah. Can you tell me about" → "...about tooth
            # implants?" double-fire at the brain-dispatch layer, so we
            # can keep endpointing aggressive for latency.
            ("endpointing", "150"),
            # 2026-08-08: added utterance_end_ms. Per Deepgram docs
            # (https://developers.deepgram.com/docs/utterance-end) +
            # docs/rnd-2026-08/53-fast-stt-alternatives.md root-cause
            # analysis: without this, if the FIRST audio bytes don't
            # get recognized as speech (format drift, silent front, or
            # WS-handshake timing), only silence-based endpointing
            # fires — and if silence isn't ever detected we sit for
            # 20-40s waiting.  1000ms UtteranceEnd = "if I haven't
            # seen a speech_final in 1s of continuous audio, force one."
            ("utterance_end_ms", "1000"),
            ("vad_events", "true"),
            ("language", self.language),
        ]
        # S13-B: keyterm boosting for dental / medical vocabulary.
        # Nova-3 supports repeated `keyterm` params — kills mishearings
        # like "student plans" for "tooth implants".  See
        # https://developers.deepgram.com/docs/keyterm
        for keyterm in _DENTAL_KEYTERMS:
            params.append(("keyterm", keyterm))
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

        # Sprint 12: track whether we're shutting down cleanly (audio_chunks
        # ran out because caller closed the stream) vs Deepgram abnormally
        # closed on us (idle timeout, remote error, network drop).  The
        # bridge's reconnect ladder only fires when this function RAISES,
        # so an abnormal close must propagate — a normal one must not.
        abnormal_close: dict[str, str | None] = {"reason": None}

        try:

            async def _producer():
                """Push caller audio chunks into the WS. On end-of-iteration,
                send CloseStream so Deepgram flushes remaining transcripts."""
                # 2026-08-08: count frames sent so we can see if audio flow
                # actually reaches Deepgram.  Log every 500 frames = every
                # ~10 sec at 20ms Twilio cadence.
                _frames = 0
                try:
                    async for chunk in audio_chunks:
                        if not chunk:
                            continue
                        await ws.send(chunk)
                        _frames += 1
                        if _frames % 500 == 0:
                            log.info("DG_PRODUCER sent %d frames (%d bytes total)",
                                     _frames, _frames * len(chunk))
                except websockets.ConnectionClosed as e:
                    # Provider closed the socket on us — force reconnect.
                    abnormal_close["reason"] = f"producer.send: {e}"
                except Exception as e:
                    log.warning("deepgram producer failed: %s", e)
                    abnormal_close["reason"] = f"producer: {e}"
                finally:
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass

            async def _keepalive():
                """2026-08-07: Deepgram closes the WS with 1011 after ~10s
                of no audio.  During agent TTS playback (when we mute the
                mic to avoid echo) this fires every call.  Send a KeepAlive
                every 5s — official Deepgram protocol message that resets
                their idle timer without adding audio.
                See https://developers.deepgram.com/docs/audio-keep-alive
                """
                try:
                    while True:
                        await asyncio.sleep(5.0)
                        try:
                            await ws.send(json.dumps({"type": "KeepAlive"}))
                        except websockets.ConnectionClosed:
                            return
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    return

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
                        except websockets.ConnectionClosed as e:
                            # Distinguish clean close (code 1000, we sent
                            # CloseStream) from abnormal (1011 idle timeout,
                            # 1006 network drop, etc.).  Abnormal → reconnect.
                            code = getattr(e, "code", None)
                            if code not in (None, 1000, 1001):
                                abnormal_close["reason"] = (
                                    f"consumer.recv: code={code} {e}"
                                )
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        mtype = msg.get("type")
                        # 2026-08-08: LOUD Deepgram observability so we
                        # can see EVERY message the WS sends us.  Silent
                        # STT hangs used to be invisible — now every event
                        # prints so we know if DG is dropping audio, hearing
                        # empty, or just not responding.
                        if mtype == "SpeechStarted":
                            log.info("DG_EVT SpeechStarted")
                            await event_queue.put(STTEvent(kind="speech_start"))
                        elif mtype == "UtteranceEnd":
                            log.info("DG_EVT UtteranceEnd last_word_end=%s",
                                     msg.get("last_word_end"))
                            await event_queue.put(STTEvent(kind="speech_end"))
                        elif mtype == "Results":
                            try:
                                alt = msg["channel"]["alternatives"][0]
                                text = alt.get("transcript", "").strip()
                                confidence = alt.get("confidence", 0.0)
                                is_final = bool(msg.get("is_final"))
                                speech_final = bool(msg.get("speech_final"))
                            except (KeyError, IndexError):
                                continue
                            # Log even empty Results — tells us DG is
                            # processing audio but hearing silence.
                            if text or is_final:
                                log.info(
                                    "DG_EVT Results text=%r is_final=%s speech_final=%s conf=%.2f",
                                    text[:60], is_final, speech_final, confidence,
                                )
                            if text:
                                await event_queue.put(STTEvent(
                                    kind="final" if is_final else "partial",
                                    text=text, is_final=is_final,
                                    speech_final=speech_final,
                                ))
                        elif mtype == "Metadata":
                            log.info("DG_EVT Metadata sha256=%s duration=%s channels=%s",
                                     (msg.get("sha256") or "")[:12],
                                     msg.get("duration"), msg.get("channels"))
                            continue  # opening handshake
                        else:
                            # Any other message type — log so we don't miss
                            # a new Deepgram protocol message silently.
                            log.info("DG_EVT other type=%s keys=%s",
                                     mtype, list(msg.keys())[:5])
                except Exception as e:
                    log.warning("deepgram consumer failed: %s", e)
                finally:
                    await event_queue.put(None)  # sentinel

            producer = asyncio.create_task(_producer())
            consumer = asyncio.create_task(_consumer())
            keepalive = asyncio.create_task(_keepalive())
            try:
                while True:
                    ev = await event_queue.get()
                    if ev is None:
                        break
                    yield ev
            finally:
                producer.cancel()
                consumer.cancel()
                keepalive.cancel()
        finally:
            await _ws_ctx.__aexit__(None, None, None)

        # Sprint 12: outside the ws context, if the close was abnormal,
        # raise so the bridge's reconnect ladder can fire.  A clean
        # close (caller stopped audio_chunks) exits normally.
        if abnormal_close["reason"] is not None:
            raise RuntimeError(f"deepgram stream closed abnormally: {abnormal_close['reason']}")
