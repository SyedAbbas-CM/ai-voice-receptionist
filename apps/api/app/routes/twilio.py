"""Twilio Media Streams handler.

Two endpoints:
  POST /twilio/voice          - TwiML for incoming call. Tells Twilio to open a
                                Media Stream to our /twilio/stream WebSocket.
  WS   /twilio/stream         - Receives caller audio as µ-law 8kHz base64
                                frames, buffers on silence, transcribes,
                                runs brain, streams TTS back.

Setup:
  1. Buy a Twilio number.
  2. Voice settings for that number:
       "A call comes in" -> Webhook -> POST https://<your-tunnel>/twilio/voice
  3. Env:
       TWILIO_ACCOUNT_SID   (optional; only needed if we make outbound calls)
       TWILIO_AUTH_TOKEN    (optional; for signature validation)
       TWILIO_PUBLIC_URL    (your tunnel URL — used to build the wss:// URL)

MVP scope (turn-based, not streaming):
  - Buffer inbound µ-law until ~700ms of silence.
  - Convert to WAV, send to STT.
  - Run brain, get reply text.
  - Synthesize with TTS, convert to µ-law 8kHz, send back in 20ms frames.
  - Repeat.

Streaming STT + barge-in interruption lands in phase 5.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import io
import json
import logging
import time
import uuid
import wave
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.core import session_manager
from app.core.config import settings
from app.providers import get_stt, get_tts


log = logging.getLogger(__name__)
router = APIRouter(tags=["twilio"])


# Twilio Media Streams protocol constants
TWILIO_SAMPLE_RATE = 8000
TWILIO_FRAME_MS = 20                     # each media frame is ~20ms of audio
SILENCE_THRESHOLD_RMS = 500              # RMS fallback threshold (used only when VAD_KIND=rms)
SILENCE_HANG_MS = 700                    # how much silence ends a turn
MAX_UTTERANCE_MS = 12000                 # hard cap so we don't buffer forever
MIN_UTTERANCE_MS = 400                   # skip too-short blips


# Lazy VAD singleton. Silero is preferred but we fall back to RMS if the
# torch/onnx deps aren't installed.
_vad_singleton = None


def _get_vad():
    global _vad_singleton
    if _vad_singleton is None:
        from packages.voice import build_vad
        _vad_singleton = build_vad(
            kind=getattr(settings, "vad_kind", None) or "auto"
        )
        log.info("Twilio VAD backend: %s", _vad_singleton.name)
    return _vad_singleton


def _twiml_stream_response(public_url: str) -> str:
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/twilio/stream"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>"""


async def _verify_twilio_signature(request: Request) -> None:
    """AUDIT FIX 2026-08-01 (WH-001): verify X-Twilio-Signature so nobody
    else can hit our TwiML endpoint and drive fake call setups.

    Twilio's algorithm: HMAC-SHA1 over URL + sorted-form-values, base64
    encoded, compared constant-time against the header.  When
    TWILIO_SIGNATURE_ENFORCE=false, verification is skipped (dev only).
    """
    import os
    if os.environ.get("TWILIO_SIGNATURE_ENFORCE", "true").lower() in ("0", "false", "no"):
        return
    token = settings.twilio_auth_token or ""
    if not token:
        raise HTTPException(500, "Twilio signature enforcement is on but TWILIO_AUTH_TOKEN is not configured")
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(401, "missing X-Twilio-Signature")
    # Build the canonical URL (Twilio hashes the exact URL they POSTed to)
    public_base = (settings.twilio_public_url or "").rstrip("/")
    if not public_base:
        raise HTTPException(500, "TWILIO_PUBLIC_URL not configured for signature verification")
    url = f"{public_base}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    # Twilio appends sorted form values
    form = await request.form()
    payload = url + "".join(f"{k}{form.get(k)}" for k in sorted(form.keys()))
    import base64, hashlib, hmac
    expected = base64.b64encode(
        hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid Twilio signature")


@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    await _verify_twilio_signature(request)
    public = settings.twilio_public_url or ""
    if not public:
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Sorry, this number is not configured. Please set TWILIO_PUBLIC_URL.</Say><Hangup/></Response>""",
            media_type="application/xml",
        )
    return Response(content=_twiml_stream_response(public), media_type="application/xml")


def _mulaw_frames_to_wav(mulaw: bytes, sample_rate: int = TWILIO_SAMPLE_RATE) -> bytes:
    """µ-law -> 16-bit PCM -> WAV container."""
    pcm = audioop.ulaw2lin(mulaw, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _tts_bytes_to_mulaw(audio_bytes: bytes, mime: str) -> bytes:
    """Downsample TTS audio to 8kHz mono µ-law for Twilio playback.

    AUDIT FIX 2026-08-01 (PROV-014): every TTS provider on the Twilio
    route MUST produce audio the transport can handle.  Supported inputs:

      * WAV — Qwen3, Piper, one-shot Cartesia (`wav` container)
      * raw PCM s16le — Cartesia SSE default (`audio/pcm;rate=NNNN`)
      * µ-law 8kHz — providers that natively emit telephony audio
        (Cartesia `ulaw_8000`, Deepgram `mulaw`, ElevenLabs `ulaw_8000`)

    MP3 is NOT decoded — MP3-emitting providers (11L default, OpenAI TTS
    default) MUST be configured to return one of the above formats on the
    Twilio route.  We raise loudly rather than emit silence.
    """
    mime = (mime or "").lower()

    # --- Case 1: already µ-law 8kHz — pass through ---
    if "mulaw" in mime or "ulaw" in mime or "pcmu" in mime:
        return audio_bytes

    # --- Case 2: raw PCM s16le — extract sample rate from MIME param ---
    if mime.startswith("audio/pcm") or "pcm_s16le" in mime or "l16" in mime:
        src_rate = TWILIO_SAMPLE_RATE
        if "rate=" in mime:
            try:
                src_rate = int(mime.split("rate=", 1)[1].split(";")[0].strip())
            except (ValueError, IndexError):
                pass
        pcm = audio_bytes
        if src_rate != TWILIO_SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, TWILIO_SAMPLE_RATE, None)
        return audioop.lin2ulaw(pcm, 2)

    # --- Case 3: WAV container ---
    if "wav" in mime:
        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            pcm = w.readframes(w.getnframes())
            src_rate = w.getframerate()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
        if channels == 2:
            pcm = audioop.tomono(pcm, sampwidth, 1, 1)
        if sampwidth != 2:
            pcm = audioop.lin2lin(pcm, sampwidth, 2)
        if src_rate != TWILIO_SAMPLE_RATE:
            pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, TWILIO_SAMPLE_RATE, None)
        return audioop.lin2ulaw(pcm, 2)

    # --- Case 4: MP3 / anything else — refuse loudly ---
    raise RuntimeError(
        f"twilio stream cannot handle TTS mime {mime!r}. Supported: "
        "audio/wav, audio/pcm;rate=NNNN, audio/x-mulaw. Configure your "
        "TTS provider to emit one of these (Cartesia: use `container=raw` "
        "or `container=wav`; ElevenLabs: `output_format=ulaw_8000`)."
    )


class TwilioStreamSession:
    """Per-call state: audio buffer, silence detector, brain handle.

    Barge-in: while the agent is speaking, we still buffer caller audio
    into `barge_buffer`. Every ~500ms of speech in that buffer, we run a
    quick STT + classify_barge check. On INTERRUPT, we set
    `interrupt_flag` which the audio-streaming loop reads to abort mid-
    frame + flush. On CONTINUE (backchannel), we drop the buffer and
    keep talking.
    """

    def __init__(self, ws: WebSocket, stream_sid: str, session_id: str) -> None:
        self.ws = ws
        self.stream_sid = stream_sid
        self.session_id = session_id
        self.buffer = bytearray()
        self.last_voiced_ms: Optional[float] = None
        self.utterance_started_ms: Optional[float] = None
        self.speaking_reply = False

        # Barge-in state
        self.barge_buffer = bytearray()
        self.barge_last_voiced_ms: Optional[float] = None
        self.barge_check_interval_ms = 500
        self.barge_last_check_ms = 0.0
        self.interrupt_flag = False
        self.pending_barge_text: Optional[str] = None

    def _now_ms(self) -> float:
        return time.time() * 1000

    def _frame_is_speech(self, mulaw_frame: bytes) -> bool:
        if not mulaw_frame:
            return False
        return _get_vad().is_speech(
            mulaw_frame,
            sample_rate=TWILIO_SAMPLE_RATE,
            mime="audio/mulaw",
        )

    async def handle_media_frame(self, mulaw_frame: bytes) -> None:
        if self.speaking_reply:
            # Barge-in path: caller might be trying to interrupt or just
            # backchanneling. Buffer + periodically check.
            await self._handle_barge_frame(mulaw_frame)
            return
        now = self._now_ms()
        is_speech = self._frame_is_speech(mulaw_frame)

        if is_speech:
            if self.utterance_started_ms is None:
                self.utterance_started_ms = now
            self.last_voiced_ms = now
            self.buffer.extend(mulaw_frame)
        else:
            if self.utterance_started_ms is not None:
                self.buffer.extend(mulaw_frame)

        should_close = False
        if self.utterance_started_ms is not None:
            duration_ms = now - self.utterance_started_ms
            silence_ms = (now - self.last_voiced_ms) if self.last_voiced_ms else 0
            if duration_ms >= MAX_UTTERANCE_MS:
                should_close = True
            elif duration_ms >= MIN_UTTERANCE_MS and silence_ms >= SILENCE_HANG_MS:
                should_close = True

        if should_close:
            utterance = bytes(self.buffer)
            self.buffer.clear()
            self.utterance_started_ms = None
            self.last_voiced_ms = None
            asyncio.create_task(self._process_utterance(utterance))

    async def _process_utterance(self, mulaw_utterance: bytes) -> None:
        if len(mulaw_utterance) < 8000:  # < ~1s at 8kHz µ-law
            return
        try:
            wav = _mulaw_frames_to_wav(mulaw_utterance)
            stt = get_stt()
            transcript = await stt.transcribe(wav, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/wav")
            if not transcript.strip():
                return
            log.info("twilio %s heard: %s", self.session_id, transcript)

            handle = session_manager.get_session(self.session_id)
            if handle is None:
                state, brain = session_manager.start_session_with_id(self.session_id)
                await session_manager.run_greeting(state, brain)
            else:
                state, brain = handle

            # Fire the brain turn AND (in parallel) queue a filler clip so
            # the caller hears "one sec" if the brain takes >800ms.
            filler_task = asyncio.create_task(self._maybe_play_filler_after_delay(0.8))
            try:
                payload = await session_manager.run_user_turn(state, brain, transcript)
            finally:
                filler_task.cancel()

            reply = payload.get("reply") or ""
            if reply:
                await self.speak(reply)
        except Exception as e:
            log.exception("twilio process_utterance failed: %s", e)

    async def _maybe_play_filler_after_delay(self, delay_s: float) -> None:
        """Play a filler clip if the brain hasn't produced a reply within
        `delay_s`. Cancelled by the caller if the reply arrives first."""
        try:
            await asyncio.sleep(delay_s)
            from packages.voice import get_pool
            clip = get_pool().pick()
            if not clip or self.speaking_reply:
                return
            log.info("twilio %s playing filler: %r", self.session_id, clip.text)
            await self._stream_audio_bytes(clip.audio, clip.mime)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("filler playback failed: %s", e)

    async def _stream_audio_bytes(self, audio_bytes: bytes, mime: str) -> None:
        """Shared code path for playing pre-synthesized audio (fillers OR
        assistant replies). Aborts mid-stream on `self.interrupt_flag`."""
        try:
            mulaw = _tts_bytes_to_mulaw(audio_bytes, mime)
        except Exception as e:
            log.warning("filler mu-law convert failed: %s", e)
            return
        await self._send_frames(mulaw)

    async def speak(self, text: str) -> None:
        tts = get_tts()
        try:
            audio_bytes, mime = await tts.synthesize(text)
            if mime == "text/x-browser-speak":
                log.warning("browser TTS provider can't drive a phone call; set TTS_PROVIDER=qwen3 or similar")
                return
            mulaw = _tts_bytes_to_mulaw(audio_bytes, mime)
        except Exception as e:
            log.exception("twilio speak failed: %s", e)
            return
        await self._send_frames(mulaw)

        # If we were interrupted, process the caller's utterance as the
        # next real turn. We already have their transcribed text from the
        # barge-in check — feed it straight to the brain.
        if self.pending_barge_text:
            interrupted_text = self.pending_barge_text
            self.pending_barge_text = None
            self.interrupt_flag = False
            self.barge_buffer.clear()
            self.barge_last_voiced_ms = None
            log.info("twilio %s processing interrupt turn: %r", self.session_id, interrupted_text)
            handle = session_manager.get_session(self.session_id)
            if handle is not None:
                state, brain = handle
                try:
                    payload = await session_manager.run_user_turn(state, brain, interrupted_text)
                    reply = payload.get("reply") or ""
                    if reply:
                        await self.speak(reply)
                except Exception as e:
                    log.exception("interrupt-turn failed: %s", e)

    async def _send_frames(self, mulaw: bytes) -> None:
        """Stream µ-law audio out in 20ms frames. Checks interrupt_flag
        between every frame — on interrupt, sends Twilio `clear` to flush
        any buffered audio server-side and stops immediately."""
        self.speaking_reply = True
        self.interrupt_flag = False
        frame_bytes = int(TWILIO_SAMPLE_RATE * (TWILIO_FRAME_MS / 1000))
        try:
            for i in range(0, len(mulaw), frame_bytes):
                if self.interrupt_flag:
                    log.info("twilio %s barge-in — flushing TTS", self.session_id)
                    try:
                        # Tell Twilio to drop any audio it has buffered for
                        # this stream. Prevents leftover playback after we stop.
                        await self.ws.send_text(json.dumps({
                            "event": "clear",
                            "streamSid": self.stream_sid,
                        }))
                    except Exception:
                        pass
                    break
                chunk = mulaw[i:i + frame_bytes]
                if len(chunk) < frame_bytes:
                    chunk = chunk + b"\xff" * (frame_bytes - len(chunk))
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                }))
                await asyncio.sleep(TWILIO_FRAME_MS / 1000)
        finally:
            self.speaking_reply = False

    # ------------------------------------------------------------------
    # Barge-in detection
    # ------------------------------------------------------------------
    async def _handle_barge_frame(self, mulaw_frame: bytes) -> None:
        """Called on each incoming caller frame WHILE the agent is speaking.
        Buffers audio, periodically transcribes + classifies, and sets
        `interrupt_flag` on real interruption. On backchannel, resets buffer."""
        if not mulaw_frame:
            return
        is_speech = self._frame_is_speech(mulaw_frame)
        now = self._now_ms()

        if is_speech:
            self.barge_buffer.extend(mulaw_frame)
            self.barge_last_voiced_ms = now
        else:
            # Small tail of silence to help the classifier
            if self.barge_last_voiced_ms is not None:
                self.barge_buffer.extend(mulaw_frame)

        # Nothing to do if no voice yet
        if not self.barge_buffer:
            return

        # Rate-limit the classification calls
        if (now - self.barge_last_check_ms) < self.barge_check_interval_ms:
            return

        # Need at least ~300ms of audio to bother classifying
        if len(self.barge_buffer) < 2400:  # 300ms of µ-law @ 8kHz
            return

        self.barge_last_check_ms = now
        # Snapshot + clear so parallel frames don't corrupt
        snapshot = bytes(self.barge_buffer)
        try:
            wav = _mulaw_frames_to_wav(snapshot)
            stt = get_stt()
            text = await stt.transcribe(wav, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/wav")
        except Exception as e:
            log.warning("barge STT failed: %s", e)
            return

        if not text.strip():
            return

        from packages.voice import BargeAction, classify_barge
        action = classify_barge(text)
        log.info("twilio %s barge candidate: %r -> %s", self.session_id, text, action.value)

        if action is BargeAction.INTERRUPT:
            self.pending_barge_text = text
            self.interrupt_flag = True
            # Preserve buffer — the utterance-processor will consume it as the
            # next real user turn once TTS stops.
        elif action is BargeAction.CONTINUE:
            # Backchannel — reset buffer, keep talking
            self.barge_buffer.clear()
            self.barge_last_voiced_ms = None
        # IGNORE → do nothing, wait for more audio


@router.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket) -> None:
    await ws.accept()
    session: Optional[TwilioStreamSession] = None
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "connected":
                log.info("twilio connected: %s", event.get("protocol"))
                continue

            if kind == "start":
                stream_sid = event["start"]["streamSid"]
                call_sid = event["start"].get("callSid") or f"call_{uuid.uuid4().hex[:8]}"
                session_id = f"twilio_{call_sid}"
                session = TwilioStreamSession(ws=ws, stream_sid=stream_sid, session_id=session_id)
                log.info("twilio start: %s (%s)", call_sid, stream_sid)
                # Fire greeting immediately.
                state, brain = session_manager.start_session_with_id(session_id)
                greeting = await session_manager.run_greeting(state, brain)
                await session.speak(greeting)
                continue

            if kind == "media" and session is not None:
                payload_b64 = event["media"]["payload"]
                mulaw_frame = base64.b64decode(payload_b64)
                await session.handle_media_frame(mulaw_frame)
                continue

            if kind == "stop" and session is not None:
                log.info("twilio stop: %s", session.session_id)
                await session_manager.end_session_async(session.session_id)
                break

    except WebSocketDisconnect:
        if session:
            await session_manager.end_session_async(session.session_id)
        log.info("twilio ws disconnected")
    except Exception as e:
        log.exception("twilio ws error: %s", e)
