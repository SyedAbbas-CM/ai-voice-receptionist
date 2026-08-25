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
        # 2026-08-21 NET-07: Flux connection params are SINGULAR
        # (language_hint / keyterm). Plural forms only work in runtime
        # Configure messages. Also language_hint only applies to
        # flux-general-multi — sending it to flux-general-en is
        # invalid. Previously we sent `language_hints`/`keyterms` so
        # dental keyterm boost was silently never applied.
        if self.language_hint and self.model == "flux-general-multi":
            params.append(("language_hint", self.language_hint))
        for keyterm in _DENTAL_KEYTERMS:
            params.append(("keyterm", keyterm))

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
                """2026-08-20: NO-OP on Flux.  Deepgram issue #649
                confirmed: sending `{"type":"KeepAlive"}` to Flux v2 kills
                the connection immediately with WS code 1005 (verified
                on trace CAe87b82).  Flux is kept alive by the audio
                stream itself — the caller is expected to send a
                continuous audio feed (real or mulaw silence 0xFF).
                Nova-3's KeepAlive JSON is NOT compatible with Flux v2.
                """
                try:
                    while True:
                        await asyncio.sleep(3600)
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
                            # 2026-08-21: real Flux schema is FLAT.
                            # Fields live at msg root, not msg["turn_info"]:
                            #   event: "Update"|"StartOfTurn"|"EagerEndOfTurn"|
                            #          "EndOfTurn"|"TurnResumed"
                            #   transcript: str
                            #   end_of_turn_confidence: float
                            # Verified against actual WS payload on
                            # 2026-08-21 (see /tmp/flux_survive.py output).
                            # Prior nested `turn_info.events[]` parse was
                            # dropping every real message → empty events + no
                            # text, so caller speech went nowhere.
                            event = msg.get("event")
                            text = (msg.get("transcript") or "").strip()
                            eot_conf = msg.get("end_of_turn_confidence")
                            is_final = event == "EndOfTurn"
                            # 2026-08-24 ChatGPT audit item #2: extract
                            # word-level acoustic timestamps + audio-window
                            # markers. Deepgram Flux exposes these but
                            # we've been throwing them away. Enables us
                            # to measure the REAL "mouth-close → EOT"
                            # gap: last word's `.end` timestamp is the
                            # acoustic end of caller speech; comparing to
                            # our wall-clock EOT arrival tells us pure
                            # Flux endpointing latency (was undetectable
                            # before because STT_VAD `speech_start` is
                            # not a mouth-open ground truth).
                            words = msg.get("words") or []
                            last_word_end_s = None
                            if words:
                                # `words[-1]` is the most recent word;
                                # `.end` is seconds since audio stream start
                                _lw = words[-1] if isinstance(words, list) else None
                                if isinstance(_lw, dict):
                                    last_word_end_s = _lw.get("end")
                            audio_window_end_s = msg.get("audio_window_end")
                            turn_index = msg.get("turn_index")
                            sequence_id = msg.get("sequence_id")
                            # Only log non-empty updates to reduce noise.
                            if event and event != "Update":
                                log.info(
                                    "DGF_TURN event=%s text=%r eot_conf=%s "
                                    "last_word_end_s=%s audio_window_end_s=%s "
                                    "turn_idx=%s seq=%s",
                                    event, text[:80], eot_conf,
                                    last_word_end_s, audio_window_end_s,
                                    turn_index, sequence_id,
                                )
                            elif event == "Update" and text:
                                log.info(
                                    "DGF_TURN Update text=%r eot_conf=%s "
                                    "last_word_end_s=%s",
                                    text[:80], eot_conf, last_word_end_s,
                                )

                            # Interim partial (Update event) — emit partial
                            # for the turn manager to track.
                            if event == "Update" and text:
                                await event_queue.put(STTEvent(
                                    kind="partial",
                                    text=text,
                                    is_final=False,
                                    speech_final=False,
                                ))

                            # StartOfTurn maps to our speech_start signal.
                            elif event == "StartOfTurn":
                                await event_queue.put(STTEvent(kind="speech_start"))

                            # EagerEndOfTurn — model thinks caller is done
                            # but hasn't committed.  Turn manager can fire
                            # speculative brain now.  Payload has transcript.
                            elif event == "EagerEndOfTurn":
                                await event_queue.put(STTEvent(
                                    kind="eager_end_of_turn",
                                    text=text,
                                    is_final=False,
                                    speech_final=False,
                                ))

                            # TurnResumed — caller kept talking, cancel any
                            # speculative brain fired from EagerEndOfTurn.
                            elif event == "TurnResumed":
                                await event_queue.put(STTEvent(kind="turn_resumed"))

                            # EndOfTurn — final, committed.
                            # 2026-08-21 NET-01: was emitting BOTH a
                            # synthetic 'final' AND 'end_of_turn' per
                            # audit — that made one Flux EndOfTurn
                            # dispatch through both the Nova-style final
                            # path (which fires EAGER + speculative
                            # brain) AND the native end_of_turn path
                            # (which commits the turn). Result: double
                            # brain dispatch, commit-lock races, and
                            # duplicate STT_FINAL log entries. Flux's
                            # own turn state machine is authoritative;
                            # emit only end_of_turn.
                            elif event == "EndOfTurn":
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
