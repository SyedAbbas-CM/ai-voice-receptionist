"""Deepgram Flux streaming STT adapter (2026-08-11, task #316).

Flux is Deepgram's voice-agent-optimized model.  Same account/key as
Nova-3 but different URL (`/v2/listen`), different message schema
(everything lives in `TurnInfo`), and native end-of-turn events —
which lets us bypass our 400ms confirm-window entirely.

Key differences from Nova-3:
  - URL:  /v2/listen  (not /v1/listen)
  - Model:  flux-general-en  (English) or flux-general-multi
  - Events: TurnInfo.events = [StartOfTurn|Update|EagerEndOfTurn|
    EndOfTurn|TurnResumed]  instead of Results/SpeechStarted/etc.
  - Config:  eot_threshold, eager_eot_threshold, eot_timeout_ms  for
    turn-taking tuning
  - No smart-format / punctuate / interim_results params — Flux handles
    all of that internally.

Callers get STTEvent(kind='eager_end_of_turn'|'end_of_turn'|
'turn_resumed') on top of the existing partial/final/speech_start/end
kinds so the turn manager can trust Flux's native decisions instead of
running its own confirm-window heuristic.

Urdu is NOT supported by Flux (2026-08-11 — flux-general-multi covers
en, es, fr, de, hi, ja, ko, nl, pt, and one more but NOT ur).  Nova-3
stays as the default; Flux is opt-in via settings.deepgram_use_flux
or per-tenant business profile flag.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

from app.core.config import settings

from ..base import STTEvent, STTProvider
from .deepgram_stt import _DENTAL_KEYTERMS


log = logging.getLogger(__name__)


class DeepgramFluxSTT(STTProvider):
    """Streaming-only.  batch transcribe() falls back to Nova-3 REST."""
    name = "deepgram_flux"
    supports_streaming = True

    _WS_URL = "wss://api.deepgram.com/v2/listen"

    def __init__(self, language: Optional[str] = None) -> None:
        self.api_key = settings.deepgram_api_key
        # Flux model IDs: flux-general-en (English), flux-general-multi (10 langs).
        # Default English since Urdu isn't supported yet and Dutch/Hindi work
        # on both but multi has slightly higher latency per Deepgram.
        lang = (language or settings.deepgram_language or "en-US").lower()
        if lang.startswith("en"):
            self.model = "flux-general-en"
            self.language_hint = None
        else:
            self.model = "flux-general-multi"
            self.language_hint = lang.split("-")[0]  # ISO-639-1 e.g. "nl"

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000, mime: str = "audio/wav") -> str:
        # Flux is streaming-only.  For batch, fall back to Nova-3 REST.
        from .deepgram_stt import DeepgramSTT
        return await DeepgramSTT().transcribe(audio_bytes, sample_rate, mime)

    async def transcribe_stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        sample_rate: int = 16000,
        encoding: str = "linear16",
    ) -> AsyncIterator[STTEvent]:
        """Stream to Flux /v2/listen and yield STTEvents.  Native turn
        events map to eager_end_of_turn / end_of_turn / turn_resumed
        kinds — turn manager should trust these directly and skip its
        own confirm window."""
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")

        try:
            import websockets
        except ImportError as e:
            raise RuntimeError(
                "Deepgram Flux streaming needs `pip install websockets`."
            ) from e

        # 2026-08-11: Flux REJECTS `channels` param (unlike Nova-3 which
        # requires it).  Handshake bench in /tmp/test_flux_handshake.py
        # confirmed: every other param works, `channels=1` returns HTTP 400.
        params: list[tuple[str, str]] = [
            ("model", self.model),
            ("encoding", encoding),
            ("sample_rate", str(sample_rate)),
            # Turn-taking tuning.  eot_threshold gates how confident the
            # model needs to be that the caller is done.  0.7 is the
            # documented default; can drop for snappier turns (more
            # false-EOT) or raise for stubbornly-quiet callers.
            ("eot_threshold", str(settings.deepgram_flux_eot_threshold)),
            ("eager_eot_threshold", str(settings.deepgram_flux_eager_eot_threshold)),
            ("eot_timeout_ms", str(settings.deepgram_flux_eot_timeout_ms)),
        ]
        if self.language_hint:
            params.append(("language_hints", self.language_hint))
        # Same keyterm boost list Nova-3 uses — Flux supports it identically.
        for keyterm in _DENTAL_KEYTERMS:
            params.append(("keyterms", keyterm))

        url = f"{self._WS_URL}?{urlencode(params)}"
        headers = {"Authorization": f"Token {self.api_key}"}

        try:
            _ws_ctx = websockets.connect(url, additional_headers=headers)
        except TypeError:
            _ws_ctx = websockets.connect(url, extra_headers=headers)

        ws = await _ws_ctx.__aenter__()
        event_queue: asyncio.Queue = asyncio.Queue()
        abnormal_close: dict[str, str | None] = {"reason": None}

        try:

            async def _producer():
                _frames = 0
                try:
                    async for chunk in audio_chunks:
                        if not chunk:
                            continue
                        await ws.send(chunk)
                        _frames += 1
                        if _frames % 500 == 0:
                            log.info(
                                "DGF_PRODUCER sent %d frames (%d bytes total)",
                                _frames, _frames * len(chunk),
                            )
                except websockets.ConnectionClosed as e:
                    abnormal_close["reason"] = f"producer.send: {e}"
                except Exception as e:
                    log.warning("deepgram-flux producer failed: %s", e)
                    abnormal_close["reason"] = f"producer: {e}"
                finally:
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass

            async def _keepalive():
                """Same as Nova-3 — 5s cadence to keep idle WS alive."""
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
                """Read TurnInfo messages and translate to STTEvent."""
                try:
                    while True:
                        try:
                            raw = await ws.recv()
                        except websockets.ConnectionClosed as e:
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

                        if mtype == "TurnInfo":
                            ti = msg.get("turn_info") or {}
                            events = ti.get("events") or []
                            text = (ti.get("transcript") or "").strip()
                            is_final = bool(ti.get("is_final"))
                            log.info(
                                "DGF_TURN events=%s text=%r is_final=%s",
                                events, text[:80], is_final,
                            )

                            # Interim partial (Update event) — emit partial
                            # for the turn manager to track.
                            if "Update" in events and text:
                                await event_queue.put(STTEvent(
                                    kind="partial",
                                    text=text,
                                    is_final=False,
                                    speech_final=False,
                                ))

                            # StartOfTurn maps to our speech_start signal.
                            if "StartOfTurn" in events:
                                await event_queue.put(STTEvent(kind="speech_start"))

                            # EagerEndOfTurn — model thinks caller is done
                            # but hasn't committed.  Turn manager can fire
                            # speculative brain now.  Payload has transcript.
                            if "EagerEndOfTurn" in events:
                                await event_queue.put(STTEvent(
                                    kind="eager_end_of_turn",
                                    text=text,
                                    is_final=False,
                                    speech_final=False,
                                ))

                            # TurnResumed — caller kept talking, cancel any
                            # speculative brain fired from EagerEndOfTurn.
                            if "TurnResumed" in events:
                                await event_queue.put(STTEvent(kind="turn_resumed"))

                            # EndOfTurn — final, committed.  This is the
                            # authoritative "commit this turn" signal.
                            # Emit both a 'final' (for existing turn
                            # manager code that expects final events) AND
                            # a new 'end_of_turn' (so kernels that trust
                            # Flux can skip the confirm window).
                            if "EndOfTurn" in events:
                                if text:
                                    await event_queue.put(STTEvent(
                                        kind="final",
                                        text=text,
                                        is_final=True,
                                        speech_final=True,
                                    ))
                                await event_queue.put(STTEvent(
                                    kind="end_of_turn",
                                    text=text,
                                    is_final=True,
                                    speech_final=True,
                                ))
                        elif mtype == "Connected":
                            log.info("DGF_EVT Connected")
                        elif mtype == "ConfigureSuccess":
                            log.info("DGF_EVT ConfigureSuccess")
                        elif mtype == "ConfigureFailure":
                            log.warning("DGF_EVT ConfigureFailure: %s", msg)
                        elif mtype == "FatalError":
                            log.error("DGF_EVT FatalError: %s", msg)
                            abnormal_close["reason"] = f"FatalError: {msg}"
                            break
                        else:
                            log.info("DGF_EVT other type=%s keys=%s",
                                     mtype, list(msg.keys())[:5])
                except Exception as e:
                    log.warning("deepgram-flux consumer failed: %s", e)
                finally:
                    await event_queue.put(None)

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

        if abnormal_close["reason"] is not None:
            raise RuntimeError(
                f"deepgram-flux stream closed abnormally: {abnormal_close['reason']}"
            )
