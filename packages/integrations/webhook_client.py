"""Generic outbound webhook client for n8n / Make / Zapier / custom.

2026-08-26 — real-estate + car-wash SMB briefs both named n8n / Make /
Zapier as required.  We were missing an outbound WebhookSink; this is
that.  Tenant configures a URL, we POST every business event with an
HMAC-SHA256 signature they can verify.  Their workflow platform (n8n
Webhook trigger, Make.com custom-webhook module, Zapier catch-hook)
consumes the payload and does whatever they want downstream.

Same retry semantics as `hubspot_client.py::_request` (2026-08-25 audit
P1 pattern): 4 attempts, Retry-After honoring, jittered exponential
backoff, non-retryable on 400/401/403/404 (validation/auth/permanent).

Signature header format:
    X-VoiceOps-Signature: t=<unix_epoch>,v1=<hmac_sha256_hex>

Where the hex is HMAC-SHA256(secret, f"{timestamp}.{raw_body}").
Follows the Stripe / Twilio pattern exactly.  Tenants verify with:

    import hmac, hashlib
    header = request.headers["X-VoiceOps-Signature"]
    ts, sig = [p.split("=")[1] for p in header.split(",")]
    expected = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(),
                         hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, sig)
    # Also assert abs(now - int(ts)) < 300  # anti-replay window

Idempotency-key header lets consumers dedup on retry (we send it,
Retry-After failures + our own retry might deliver same event twice
even on success — consumer stores keys, skips duplicates).

Event shapes are documented in `docs/WEBHOOK-EVENT-SCHEMA.md`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Optional

import httpx


class WebhookError(Exception):
    """Raised on unrecoverable failure. Sink swallows + logs."""


class WebhookClient:
    """POST signed JSON events to a tenant-configured URL.

    Mirrors HubSpotClient's retry contract so failure isolation +
    outbox behavior is uniform across all outbound integrations.
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        timeout: float = 10.0,
        source: str = "voiceops-ai-agent",
    ) -> None:
        if not url:
            raise WebhookError("WEBHOOK_URL not set")
        if not secret:
            raise WebhookError("WEBHOOK_SECRET not set")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise WebhookError(
                f"WEBHOOK_URL must start with http(s)://, got {url!r}"
            )
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self.source = source

    # Same retry constants as HubSpotClient — deliberately uniform.
    _MAX_ATTEMPTS = 4
    _RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
    _BACKOFF_BASE_S = 0.25
    _BACKOFF_MAX_S = 8.0

    @staticmethod
    def _next_backoff(attempt: int, retry_after: Optional[str]) -> float:
        """Compute sleep time before next retry. Honors Retry-After
        delta-seconds, caps at MAX*4, falls back to jittered exponential.
        Byte-for-byte the same as HubSpotClient._next_backoff so a future
        refactor can extract to shared module."""
        if retry_after:
            try:
                seconds = float(retry_after.strip())
                if 0 < seconds < WebhookClient._BACKOFF_MAX_S * 4:
                    return seconds
            except (TypeError, ValueError):
                pass
        base = WebhookClient._BACKOFF_BASE_S * (2 ** (attempt - 1))
        jitter = 0.7 + 0.3 * ((attempt * 17) % 10) / 10.0
        return min(base * jitter, WebhookClient._BACKOFF_MAX_S)

    def _sign(self, body: str, timestamp: int) -> str:
        """Return the X-VoiceOps-Signature header value."""
        signed_payload = f"{timestamp}.{body}".encode("utf-8")
        digest = hmac.new(
            self.secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        return f"t={timestamp},v1={digest}"

    @staticmethod
    def verify_signature(
        secret: str,
        signature_header: str,
        raw_body: str,
        *,
        max_age_seconds: int = 300,
    ) -> bool:
        """Static helper tenants can copy verbatim to verify webhooks.

        Called by our own /public/webhooks/verify test route AND
        documented for tenants.  Constant-time compare, anti-replay
        window default 5 minutes.
        """
        if not signature_header or not raw_body or not secret:
            return False
        try:
            parts = dict(
                p.split("=", 1) for p in signature_header.split(",")
            )
            ts = int(parts.get("t") or 0)
            provided_sig = parts.get("v1") or ""
            if not ts or not provided_sig:
                return False
            # Anti-replay: reject if timestamp too old / too far future.
            now = int(time.time())
            if abs(now - ts) > max_age_seconds:
                return False
            expected = hmac.new(
                secret.encode("utf-8"),
                f"{ts}.{raw_body}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, provided_sig)
        except (ValueError, KeyError, TypeError):
            return False

    async def emit(
        self,
        event_type: str,
        payload: dict,
        *,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Send one event.  Retries on transient failures.

        `event_type` is the canonical name from `docs/WEBHOOK-EVENT-SCHEMA.md`
        (booking.created / call.completed / missed_call / message.taken /
        transfer.requested).  Payload gets wrapped in an envelope with
        event / source / timestamp / idempotency_key / data.
        """
        import asyncio
        idem = idempotency_key or f"evt_{uuid.uuid4().hex}"
        timestamp = int(time.time())
        envelope = {
            "event": event_type,
            "source": self.source,
            "timestamp": timestamp,
            "idempotency_key": idem,
            "data": payload,
        }
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        signature = self._sign(body, timestamp)
        headers = {
            "Content-Type": "application/json",
            "X-VoiceOps-Signature": signature,
            "X-VoiceOps-Idempotency-Key": idem,
            "X-VoiceOps-Event-Type": event_type,
            "User-Agent": f"{self.source}/1.0",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            resp = None
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        self.url,
                        content=body,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(self._next_backoff(attempt, None))
                    continue
                raise WebhookError(
                    f"webhook {self.url} network error after "
                    f"{attempt} attempts: {e}"
                ) from e

            if resp.status_code < 400:
                # Any 2xx / 3xx is delivered.  Consumer may return JSON
                # (n8n often does) or empty — either is fine.
                try:
                    return resp.json() if resp.content else {"ok": True}
                except json.JSONDecodeError:
                    return {"ok": True, "raw": resp.text[:200]}

            # Non-retryable — 4xx (client-fault) except the retryable set.
            if resp.status_code not in self._RETRYABLE_STATUS:
                raise WebhookError(
                    f"webhook {self.url} -> {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            if attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(self._next_backoff(
                    attempt, resp.headers.get("Retry-After"),
                ))
                continue
            raise WebhookError(
                f"webhook {self.url} -> {resp.status_code} after "
                f"{attempt} attempts (retryable): {resp.text[:400]}"
            )
        assert last_exc is not None
        raise WebhookError(str(last_exc))
