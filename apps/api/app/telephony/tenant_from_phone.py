"""Resolve inbound-call tenant from the dialled E.164 number.

2026-08-25 P0.4 (BACKEND-AUDIT-2026-08-25-CHATGPT.md#4).

The Twilio Media Streams WSS handler used to hardcode `tenant_id="default"`
for every inbound call. Combined with the P0.1 supertenant bypass in
session_manager.py, that meant any call to any Twilio number in the account
could observe any tenant's session state. This module closes that hole.

## How it's used

The `/twilio/voice` TwiML embeds `<Parameter name="to" value="{{To}}"/>` so
the `start` event on the WSS carries the dialled number in
`event.start.customParameters.to`. The WSS handler calls
`resolve_tenant_from_phone(to_e164)` BEFORE dispatching to a brain, and:

  * On hit: proceeds with the returned `(tenant_id, business_id)`.
  * On miss: refuses the call. Nothing runs, no cross-tenant fallback.

## Fallback semantics

There is no "default" fallback. If a number isn't mapped, the call fails.
This is deliberate — the failure mode we're avoiding is silent misrouting.
An unmapped number surfaces as a loud 4xx-like error the operator has to
notice, not a silent "call handled by the wrong tenant".

For dev/demo use, `PHONE_ROUTING_ALLOW_DEV_FALLBACK=true` re-enables the
old behaviour with a warning log line — off in production. Setting is
scoped to the environment, never per-request, so nobody can query-param
their way past routing.

## Cache

Lookup is hot path (every call start). We cache `phone → (tenant, business)`
in-process for 60s. Cache is invalidated by `invalidate_phone_cache()` which
admin routes call after mapping mutations. TTL kept short so a rotation
mistake surfaces within a minute, not on the next restart.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantRoute:
    """Result of a successful lookup. Immutable so it's safe to cache and
    to hand off to async call handlers without defensive copies."""
    tenant_id: str
    # None means "use the tenant's default business" — resolved downstream
    # by the tenant's config, not by this module.
    business_id: Optional[str] = None


# ─── in-process cache ────────────────────────────────────────────────────────
#
# {phone_e164: (route_or_none, cached_at_ts)}
# route_or_none == None caches a NEGATIVE result too — otherwise every
# unrecognized number causes a DB round-trip that always misses. Negative
# entries are important defense against a scanner dialling many numbers
# in a row.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[Optional[TenantRoute], float]] = {}


def invalidate_phone_cache() -> None:
    """Called by admin routes after mapping mutations. Bounces the whole
    cache — mappings are low-cardinality (dozens, not millions of rows),
    so a full clear is cheaper than tracking per-key invalidation."""
    _cache.clear()


def _dev_fallback_enabled() -> bool:
    """The escape hatch, off by default. Only useful in local dev where
    you're running against a test Twilio subaccount whose numbers haven't
    been seeded into the mapping table yet."""
    return os.environ.get(
        "PHONE_ROUTING_ALLOW_DEV_FALLBACK", "false"
    ).lower() in ("1", "true", "yes")


def _dev_fallback_tenant() -> str:
    """Which tenant the fallback assigns. Never used in production."""
    return os.environ.get("PHONE_ROUTING_DEV_FALLBACK_TENANT", "default")


def resolve_tenant_from_phone(phone_e164: str) -> Optional[TenantRoute]:
    """Look up the tenant that owns the given E.164 phone number.

    Returns None when the number is not mapped and dev fallback is off —
    the WSS handler MUST treat that as "refuse the call" and never fall
    back to a supertenant. The whole point of P0.4 is that unmapped
    numbers stop, not slide.

    Cache-hit path is a dict lookup + timestamp compare, no DB touched.
    Cache-miss path is one indexed SELECT on `phone_number_mappings`.
    """
    if not phone_e164:
        # An empty `to` means the TwiML didn't populate the parameter —
        # either misconfigured Twilio or someone forging an upgrade. Fail.
        log.warning("phone_routing: empty phone_e164 — refusing call")
        return None

    # Fast path — cache hit inside TTL.
    hit = _cache.get(phone_e164)
    if hit is not None:
        cached, ts = hit
        if time.time() - ts < _CACHE_TTL_S:
            return cached
        # Expired — fall through to DB.

    # Slow path — DB lookup. Import lazily so this module stays importable
    # from test contexts that don't want the ORM initialized (unit tests
    # for the resolver's cache semantics use monkeypatching).
    try:
        from app.db import PhoneNumberMapping
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            # Bypass tenant auto-filter — this is the AUTH lookup itself,
            # not a tenant-scoped read. Similar to _resolve_tenant_from_db
            # in middleware/auth.py; documented at that call site too.
            row = (
                db.query(PhoneNumberMapping)
                .execution_options(skip_tenant_filter=True)
                .filter(
                    PhoneNumberMapping.phone_e164 == phone_e164,
                    PhoneNumberMapping.revoked_at.is_(None),
                )
                .one_or_none()
            )
    except Exception as e:
        # DB unreachable is a real ops event but must not be silent —
        # a caller that a moment ago worked now can't get through. Log
        # loudly, refuse the call (don't take a supertenant shortcut).
        log.error("phone_routing: DB lookup failed for %s: %s", phone_e164, e)
        return None

    if row is None:
        if _dev_fallback_enabled():
            log.warning(
                "phone_routing: %s not mapped; DEV_FALLBACK returning "
                "tenant=%s (NEVER enable this flag in production)",
                phone_e164, _dev_fallback_tenant(),
            )
            route = TenantRoute(tenant_id=_dev_fallback_tenant(), business_id=None)
        else:
            log.warning(
                "phone_routing: %s not mapped and no dev fallback — call "
                "will be refused. Seed via /admin/phone_mappings.",
                phone_e164,
            )
            route = None
    else:
        route = TenantRoute(tenant_id=row.tenant_id, business_id=row.business_id)

    # Cache the result (including negative). Negative entries expire on
    # the same TTL so a newly-added mapping is picked up within 60s.
    _cache[phone_e164] = (route, time.time())
    return route


# ─── test helpers ────────────────────────────────────────────────────────────


def _reset_cache_for_tests() -> None:
    """Do NOT call outside test code. Named with a `_` prefix + `_for_tests`
    suffix so a grep at review time surfaces any accidental production use."""
    _cache.clear()
