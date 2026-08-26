"""Short-lived HMAC-signed authorization tickets.

2026-08-25 P0.3/P0.4 (BACKEND-AUDIT-2026-08-25-CHATGPT.md).

## What this replaces

Two long-standing anti-patterns in the codebase converged on the same fix:

  1. **P0.4 Twilio WSS unsigned** — the Media Streams WebSocket accepted
     any client, hardcoded `tenant_id="default"`. Twilio's own signing
     format for WSS upgrades is fragile across SDK versions, so instead
     of chasing their spec we chain trust: `/twilio/voice` (already HMAC-
     verified via `_verify_twilio_signature`) mints a ticket, embeds it
     in the TwiML `<Stream url="wss://.../twilio/stream?token=...">`, and
     the WSS handler verifies OUR ticket before accepting.

  2. **P0.3 public AI endpoints fail-open** — `/chat/*`, `/voice/*`, and
     the dashboard's `?token=` parameter accepted long-lived API keys in
     URLs. URLs leak: server logs, browser history, Referer headers on
     outbound clicks. Same ticket module backs signed short-lived
     dashboard session tokens.

## Format

Tickets are opaque strings shaped `<payload-b64url>.<signature-b64url>`.
Payload is JSON: `{"t": tenant_id, "s": subject, "e": exp_unix_seconds}`.
Signature is `HMAC-SHA256(secret, payload-b64url).b64url`.

Compact — under 200 bytes for a typical ticket. Small enough for a query
string, doesn't inflate the TwiML.

## Non-goals

This is NOT a JWT. No issuer chain, no key rotation via `kid`, no
algorithm negotiation, no third-party validation. Everything a JWT does
that we don't need = attack surface. Rotation is done by rolling the
secret env var and bouncing — the whole cluster invalidates in one step.

Not encrypted — payload is base64-encoded, readable to anyone who
intercepts the URL. Do NOT put sensitive data in `tenant_id` or `subject`.
Both are opaque identifiers, safe to log.

## Threat model

Assumed attacker: someone who can watch/replay HTTP traffic between the
public tunnel and Lightsail. Because the ticket is signed but not sealed,
they can:

  * Read the tenant_id and subject from any ticket they intercept.
    → Fine. Both are already exposed to that party via other signals.
  * Replay a ticket before it expires (default 120s for WSS, 900s for
    dashboard).
    → Acceptable. The short TTL bounds blast radius. Single-use
      redemption is a future enhancement (needs a shared store).
  * Forge a ticket without the secret.
    → Impossible without breaking HMAC-SHA256.
  * Forge a ticket after cracking the secret from a log leak.
    → Prevented by the secret NEVER being logged. Verified below.

## API

    mint_ticket(tenant_id, subject, ttl_s=120) -> str
    verify_ticket(token) -> TicketPayload             # raises on bad/expired

Callers that want a non-raising API can catch `TicketError` (parent of
both `TicketInvalid` and `TicketExpired`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# ─── Exceptions ──────────────────────────────────────────────────────────────


class TicketError(Exception):
    """Base for anything wrong with a ticket. Callers that don't care
    about the specific failure mode can `except TicketError:`."""


class TicketInvalid(TicketError):
    """Malformed, wrong signature, or missing required fields."""


class TicketExpired(TicketError):
    """Signature was valid but the `exp` timestamp has passed."""


# ─── Payload type ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TicketPayload:
    """Immutable — no mutation after verify. Both fields are opaque IDs
    already logged elsewhere; safe to include in structured logs."""
    tenant_id: str
    subject: str        # e.g. "twilio_stream:CA<sid>" or "dashboard:sess_<id>"
    exp: int            # unix seconds


# ─── Secret loading ──────────────────────────────────────────────────────────


def _get_secret() -> bytes:
    """Read SHORT_TICKET_SECRET from env. Fail loudly if unset — signing
    or verifying with an empty secret would silently make every ticket
    "valid" (empty-secret HMAC is a real string), which is worse than
    not having tickets at all.

    Not cached — pydantic-settings could reload, and this is called at
    most a few thousand times per second on the mint side. os.environ
    lookup is O(1).
    """
    raw = os.environ.get("SHORT_TICKET_SECRET", "").strip()
    if not raw:
        raise TicketError(
            "SHORT_TICKET_SECRET env var is unset. Cannot mint or verify "
            "tickets. Set it to at least 32 random bytes (e.g. "
            "`openssl rand -hex 32`) before starting the server."
        )
    if len(raw) < 32:
        raise TicketError(
            f"SHORT_TICKET_SECRET is only {len(raw)} chars — too short for "
            f"HMAC-SHA256. Use `openssl rand -hex 32` for a proper key."
        )
    return raw.encode("utf-8")


# ─── Base64url helpers (no padding, URL-safe) ───────────────────────────────


def _b64u_encode(b: bytes) -> str:
    """Base64url without padding — standard for compact web tokens."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    """Add back the padding the encoder stripped, then decode. Input can
    have or not have padding — we normalize both."""
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


# ─── Mint / verify ──────────────────────────────────────────────────────────


def mint_ticket(tenant_id: str, subject: str, ttl_s: int = 120) -> str:
    """Sign a new ticket. Default TTL is 120s — long enough for a Twilio
    WSS upgrade to arrive (typically < 1s from voice-webhook), short
    enough that a leaked URL is worthless within 2 minutes.

    Longer TTLs are legitimate for dashboard sessions (call sites pass
    ttl_s=900 for a 15-minute browser session). Do NOT default anywhere
    above 900s — that undoes the "short-lived" property.
    """
    if not tenant_id:
        raise TicketError("tenant_id is required")
    if not subject:
        raise TicketError("subject is required")
    if ttl_s <= 0:
        raise TicketError(f"ttl_s must be positive, got {ttl_s}")
    if ttl_s > 3600:
        # A ticket that lives an hour isn't "short-lived" any more. If
        # a caller needs longer they should re-mint, or the surface
        # should use a proper session. This ceiling is intentional.
        raise TicketError(
            f"ttl_s={ttl_s} exceeds 3600s cap. Short tickets are for "
            "brief authorization windows; longer sessions need a "
            "different mechanism."
        )

    secret = _get_secret()

    # `t/s/e` keys are single-char to shave 2-4 bytes per ticket. Only
    # matters at scale but the URL stays shorter and log lines less noisy.
    payload = {"t": tenant_id, "s": subject, "e": int(time.time()) + ttl_s}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64u_encode(payload_json.encode("utf-8"))

    sig = hmac.new(
        secret,
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    sig_b64 = _b64u_encode(sig)

    return f"{payload_b64}.{sig_b64}"


def verify_ticket(token: str) -> TicketPayload:
    """Verify signature + expiration, return the payload. Raises
    TicketInvalid on any structural problem or signature mismatch,
    TicketExpired on a valid-but-past-exp ticket.

    Callers on hot paths (WSS accept, dashboard nav) should catch both
    and return a generic 401 — never leak WHICH check failed to the
    client, as that helps an attacker narrow their forgery attempts.
    """
    if not token or not isinstance(token, str):
        raise TicketInvalid("empty or non-string token")

    # Structure: exactly one '.' separator.
    parts = token.split(".")
    if len(parts) != 2:
        raise TicketInvalid("malformed token (expected payload.signature)")
    payload_b64, sig_b64 = parts
    if not payload_b64 or not sig_b64:
        raise TicketInvalid("empty payload or signature segment")

    # Verify signature FIRST. Even a well-formed expired ticket must
    # have its signature verified before we trust ANY field on it —
    # otherwise "expired" leaks payload structure to attackers.
    try:
        secret = _get_secret()
    except TicketError:
        # Re-raise as invalid so callers only ever see TicketInvalid or
        # TicketExpired. TicketError is the parent — this is a subclass
        # semantic decision, not a swallowed exception.
        raise TicketInvalid("server misconfigured (secret unset)")

    expected_sig = hmac.new(
        secret,
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided_sig = _b64u_decode(sig_b64)
    except (ValueError, TypeError):
        raise TicketInvalid("signature is not valid base64url")

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise TicketInvalid("signature mismatch")

    # Now safe to parse the payload.
    try:
        payload_bytes = _b64u_decode(payload_b64)
        payload = json.loads(payload_bytes)
    except (ValueError, TypeError) as e:
        raise TicketInvalid(f"payload is not valid JSON: {e}")

    if not isinstance(payload, dict):
        raise TicketInvalid("payload is not a JSON object")

    # Field extraction. Missing fields = invalid, not expired — we can't
    # trust a payload that doesn't have the shape we mint.
    for required in ("t", "s", "e"):
        if required not in payload:
            raise TicketInvalid(f"missing required field: {required!r}")

    tenant_id = payload["t"]
    subject = payload["s"]
    exp = payload["e"]

    if not isinstance(tenant_id, str) or not tenant_id:
        raise TicketInvalid("tenant_id must be a non-empty string")
    if not isinstance(subject, str) or not subject:
        raise TicketInvalid("subject must be a non-empty string")
    if not isinstance(exp, int):
        raise TicketInvalid("exp must be an integer")

    # Expiration LAST — a good signature on a past ticket is a distinct
    # failure mode (retry might work with a fresh mint), separate from
    # a malformed ticket (never going to work).
    if int(time.time()) >= exp:
        raise TicketExpired(f"ticket expired at {exp}")

    return TicketPayload(tenant_id=tenant_id, subject=subject, exp=exp)


def try_verify_ticket(token: str) -> Optional[TicketPayload]:
    """Non-raising variant for surfaces that just want to know
    "is this good?" without discriminating between reasons."""
    try:
        return verify_ticket(token)
    except TicketError:
        return None
