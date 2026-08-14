"""R7 (task #355): telephony-arrival observability.

Diagnoses calls that never reach the actor — the class of failure
where the phone rings, the caller hears "connecting..." then silence,
and we have zero logs because our per-call logger keys off CallSid
which the actor emits (too late in the chain).

Twilio's inbound-call sequence, and where each event fires:

  Time  Event                          Emitter
  ----  ---------------------------    -------
  T0    POST /twilio/voice             VOICE_WEBHOOK_RECEIVED
        (Twilio hits our webhook)
  T0+   we return the <Connect><Stream>  STREAM_TWIML_RETURNED
        TwiML
  T1    Twilio opens WS to our         WEBSOCKET_CONNECTED
        /twilio/stream endpoint
  T1+   Twilio sends `start` event     TWILIO_START
        with streamSid + callSid
  T2    Twilio sends first `media`     FIRST_MEDIA
        event with mulaw audio

If the trail cuts off at any of these, we know WHERE:
  Nothing at all         -> Twilio can't reach us (DNS, TLS, firewall)
  VOICE only             -> our TwiML broke, or Twilio rejected it
  VOICE + TWIML          -> Twilio couldn't open the WS (auth, wss cert,
                            wrong URL, upgrade rejected)
  ...+ WEBSOCKET_CONN.   -> WS opened but Twilio never sent `start`
                            (rare — usually means our TwiML was wrong)
  ...+ TWILIO_START      -> `start` came but no media — mic muted, or
                            Media Streams region unreachable from carrier
  ...+ FIRST_MEDIA       -> everything's fine, actor should be running

Correlation:
  Everything before TWILIO_START has no CallSid yet (Twilio only
  reveals it in the `start` payload).  We correlate the early events
  by a `session_key` — either a request-id from the webhook (if
  present) or a monotonic counter + timestamp.  When TWILIO_START
  fires, we log a linking event that binds session_key -> CallSid so
  a downstream log search can walk both.

Emission:
  Each helper writes ONE structured log line at INFO on a dedicated
  logger name.  The per-call log file installer (packages.observability
  .per_call_logger) does NOT pick these up because they either lack a
  CallSid entirely or predate one — that's fine, they're for the
  server-wide log and the telephony-arrival dashboard, not per-call.

Zero fallback logic here — just log and return.  Making this bullet-
proof matters more than being clever.
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Optional

log = logging.getLogger("telephony.arrival")

_session_counter = itertools.count(1)


def new_session_key() -> str:
    """Cheap pre-CallSid correlator.  Format: `arr-<epoch_ms>-<counter>`.
    Not cryptographic — just needs to be unique per request in one
    process's lifetime."""
    return f"arr-{int(time.time() * 1000)}-{next(_session_counter)}"


def _line(kind: str, session_key: str, **fields) -> str:
    """Format one arrival event as a key=value log line.  Keys sorted
    so downstream log-parsing scripts see stable order."""
    parts = [f"kind={kind}", f"session_key={session_key}"]
    for k in sorted(fields):
        v = fields[k]
        if v is None or v == "":
            v = "-"
        # Escape spaces in values so grep-based tooling stays sane.
        s = str(v).replace(" ", "_")
        parts.append(f"{k}={s}")
    return "ARRIVAL " + " ".join(parts)


def voice_webhook_received(
    session_key: str,
    remote_ip: str = "",
    signature_ok: Optional[bool] = None,
) -> None:
    """T0: POST /twilio/voice arrived at our webhook."""
    log.info(_line(
        "VOICE_WEBHOOK_RECEIVED",
        session_key,
        remote_ip=remote_ip,
        signature_ok=signature_ok if signature_ok is not None else "unknown",
    ))


def stream_twiml_returned(
    session_key: str,
    ws_url: str = "",
    twiml_bytes: int = 0,
) -> None:
    """T0+: TwiML response sent back to Twilio."""
    log.info(_line(
        "STREAM_TWIML_RETURNED",
        session_key,
        ws_url=ws_url,
        twiml_bytes=twiml_bytes,
    ))


def websocket_connected(
    session_key: str,
    remote_ip: str = "",
) -> None:
    """T1: Twilio's WS handshake accepted.  No CallSid yet."""
    log.info(_line(
        "WEBSOCKET_CONNECTED",
        session_key,
        remote_ip=remote_ip,
    ))


def twilio_start(
    session_key: str,
    call_sid: str,
    stream_sid: str,
    account_sid: str = "",
    from_number: str = "",
    to_number: str = "",
) -> None:
    """T1+: Twilio sent the `start` event.  First point where CallSid
    is known — emit a linking line so log-searches on CallSid can walk
    back through the arrival chain via session_key."""
    log.info(_line(
        "TWILIO_START",
        session_key,
        call_sid=call_sid,
        stream_sid=stream_sid,
        account_sid=account_sid,
        from_number=from_number,
        to_number=to_number,
    ))
    # Linking line: makes `grep CA<sid> uvicorn-*.log` also pull the
    # arrival trail via the session_key.
    log.info(f"ARRIVAL_LINK session_key={session_key} call_sid={call_sid}")


def first_media(
    session_key: str,
    call_sid: str,
    stream_sid: str,
    frame_bytes: int,
) -> None:
    """T2: first inbound media frame arrived.  Actor SHOULD be running
    by this point.  If the actor's own logs are silent AFTER this event,
    the failure is inside the actor path (not the telephony layer)."""
    log.info(_line(
        "FIRST_MEDIA",
        session_key,
        call_sid=call_sid,
        stream_sid=stream_sid,
        frame_bytes=frame_bytes,
    ))


def arrival_error(
    session_key: str,
    at_stage: str,
    error: str,
) -> None:
    """Something failed during arrival.  `at_stage` names the step we
    were in (e.g. 'webhook_signature', 'twiml_render', 'ws_accept',
    'start_parse')."""
    log.warning(_line(
        "ARRIVAL_ERROR",
        session_key,
        at_stage=at_stage,
        error=error[:200],
    ))
