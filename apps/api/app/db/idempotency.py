"""Idempotency layer — Sprint 6d.

Two entry points:

  * `@idempotent(scope="booking")` — decorator for handler functions.
    Reads `Idempotency-Key` header, caches the JSON response for 24h.
    Provider retries with the same key return the cached response.

  * `idempotent_webhook(scope, event_id, fn)` — inline helper for webhook
    handlers where the "idempotency key" is a provider event_id (Twilio
    CallSid, Vapi call.id, WhatsApp message.id) rather than an HTTP header.

Storage: `idempotency` table (Sprint 6a).  Key uniqueness is
(tenant_id, key) so different tenants sharing the same provider event
don't collide (they shouldn't, but we're defensive).

The cached response is the raw JSON body + status.  Not appropriate for
streaming responses — those are decorator-skipped and callers get a
non-idempotent path (which is fine for a WebSocket call session anyway,
retries don't semantically apply).
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from .models import IdempotencyRow
from .session import SessionLocal

log = logging.getLogger(__name__)

_DEFAULT_TTL = timedelta(hours=24)


def _hash_body(body: Any) -> str:
    """SHA-256 of the JSON-normalized body for cache validation."""
    try:
        payload = json.dumps(body, sort_keys=True, default=str).encode()
    except Exception:
        payload = repr(body).encode()
    return hashlib.sha256(payload).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lookup(tenant_id: str, key: str, scope: str) -> Optional[IdempotencyRow]:
    """Return the cached row if fresh, or None."""
    with SessionLocal() as db:
        row = (
            db.query(IdempotencyRow)
            .filter(
                IdempotencyRow.tenant_id == tenant_id,
                IdempotencyRow.key == key,
                IdempotencyRow.scope == scope,
            )
            .one_or_none()
        )
        if row is None:
            return None
        if row.expires_at and row.expires_at < _now().replace(tzinfo=None):
            return None
        return row


def _persist(
    tenant_id: str,
    key: str,
    scope: str,
    response_status: int,
    response_json: dict,
    ttl: timedelta = _DEFAULT_TTL,
) -> None:
    """Persist an idempotency record.  Silently no-ops on race (another
    request beat us to insert the same key)."""
    with SessionLocal() as db:
        row = IdempotencyRow(
            tenant_id=tenant_id,
            key=key,
            scope=scope,
            response_status=response_status,
            response_json=response_json,
            expires_at=_now().replace(tzinfo=None) + ttl,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            log.info("idempotency race: tenant=%s key=%s already stored", tenant_id, key)


def idempotent(scope: str, header: str = "Idempotency-Key"):
    """Decorator for FastAPI handlers.

    Usage:

        @router.post("/bookings")
        @idempotent(scope="booking")
        async def create_booking(request: Request, body: BookingRequest) -> dict:
            ...

    Behavior:
      * If the Idempotency-Key header is present AND we have a cached
        response for (tenant_id, key, scope), return that response.
      * Otherwise call the handler, cache the response, return it.
      * If the handler raises, we DON'T cache — provider retries can retry.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # FastAPI passes Request as either positional or keyword — find it
            request: Optional[Request] = kwargs.get("request")
            if request is None:
                for a in args:
                    if isinstance(a, Request):
                        request = a
                        break
            if request is None:
                # Handler doesn't accept Request; can't do idempotency, just call
                return await fn(*args, **kwargs)

            tenant_id = getattr(request.state, "tenant_id", None) or "default"
            key = request.headers.get(header)
            if not key:
                # No key = not idempotent; just call
                return await fn(*args, **kwargs)

            cached = _lookup(tenant_id, key, scope)
            if cached is not None:
                log.info(
                    "idempotency HIT: tenant=%s scope=%s key=%s",
                    tenant_id, scope, key[:24],
                )
                return JSONResponse(
                    content=cached.response_json or {},
                    status_code=cached.response_status,
                    headers={"Idempotent-Replay": "true"},
                )

            result = await fn(*args, **kwargs)

            # Cache successful responses.  Handler may return a dict, a
            # BaseModel, or a Response — normalize to (status, json).
            try:
                if isinstance(result, JSONResponse):
                    body = json.loads(result.body)
                    _persist(tenant_id, key, scope, result.status_code, body)
                elif hasattr(result, "model_dump"):
                    body = result.model_dump()
                    _persist(tenant_id, key, scope, 200, body)
                elif isinstance(result, dict):
                    _persist(tenant_id, key, scope, 200, result)
                else:
                    log.debug("skipping idempotency cache: unsupported return type %s", type(result))
            except Exception as e:
                log.warning("idempotency cache write failed: %s", e)

            return result

        return wrapper

    return decorator


async def check_or_reserve_webhook_event(
    tenant_id: str,
    scope: str,
    event_id: str,
) -> Optional[dict]:
    """Webhook dedup — provider event_id as the key.

    Returns the cached response dict if the event was already processed,
    or None if this is the first time we've seen the event.  Caller MUST
    then process AND `record_webhook_result` to persist the response.

    This is a "check-then-set" — race-prone.  For now we accept that the
    first-writer-wins is close enough; Sprint 6d follow-up will add a
    proper `SELECT ... FOR UPDATE` on Postgres.
    """
    cached = _lookup(tenant_id, event_id, scope)
    if cached is not None:
        return {
            "replay": True,
            "status": cached.response_status,
            "body": cached.response_json or {},
        }
    return None


def record_webhook_result(
    tenant_id: str,
    scope: str,
    event_id: str,
    response_status: int,
    response_body: dict,
) -> None:
    _persist(tenant_id, event_id, scope, response_status, response_body)
