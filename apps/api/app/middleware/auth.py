"""API-key auth + tenant scoping middleware.

AUDIT FIX 2026-08-01 (SEC-001, SEC-002, SEC-003, SEC-004):
Every route that touches PII, paid providers, or real-world actions must be
authenticated.  A valid API key resolves to a `tenant_id` that is stashed on
`request.state.tenant_id` for downstream handlers to filter by.

Design:
    * `Authorization: Bearer <api_key>` header on every non-public route
    * Keys live in `settings.api_keys` — a `{key: tenant_id}` dict loaded from
      the `API_KEYS_JSON` env var (JSON object) or the `API_KEY` shortcut
      (single key mapped to tenant "default")
    * Public routes are explicitly allowlisted by path prefix — everything
      else is protected by default (fail-closed)
    * Constant-time comparison against every configured key

To disable in dev, set `API_AUTH_ENFORCE=false`.  Startup logs the mode
loudly so nobody accidentally ships an unprotected server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


# Paths where auth is intentionally skipped.  These are the surfaces that
# provider webhooks / browsers / health checks hit and where a bearer token
# does not make sense.  Provider webhooks have their own signature checks.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/config",             # only exposes provider NAMES (not keys)
    "/docs",
    "/redoc",
    "/openapi.json",
    "/twilio/",            # Twilio path validates X-Twilio-Signature separately
    "/vapi/",              # Vapi path validates its own bearer separately
    "/channels/",          # WhatsApp/Telegram signature-verify separately
    "/admin/",             # Admin routes have their own ADMIN_TOKEN guard
    "/call/",              # customer-facing widget static assets
    "/simulator/",         # dev-only widget static assets
    "/graph/",             # observability dashboard static assets
    "/apple-touch-icon",
    "/favicon.ico",
    # RE-AUDIT FIX 2026-08-02 (A-06/A-07 regressions): the /call widget,
    # /simulator, and /graph pages all hit these APIs.  Without them on the
    # public list, the widget got 401 on every call.  Tenant scoping is
    # still enforced at the handler layer via session_manager.get_session
    # (session_id + tenant_id lookup, CRITICAL-01 fix) — the tenant for
    # widget requests is "default".  Real per-tenant widget deployments
    # (Sprint 8) will use short-lived signed session tickets to swap the
    # widget's implicit "default" for the correct tenant.
    "/chat/",              # widget/simulator chat lifecycle
    "/voice/",             # widget/simulator STT+TTS
    "/v1/",                # ElevenLabs-compatible API (has its own compat_api_key gate)
    "/debug/",             # observability endpoints — dashboard reads these
)


def _load_api_keys() -> dict[str, str]:
    """Load `{key: tenant_id}` from env.

    Priority:
      1. `API_KEYS_JSON` — a JSON object like `{"sk_abc123": "acme-corp"}`
      2. `API_KEY` — single key, mapped to tenant "default"
    """
    raw_json = os.environ.get("API_KEYS_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if k and v}
        except Exception as e:
            log.error("API_KEYS_JSON is not valid JSON — refusing to load: %s", e)
    single = os.environ.get("API_KEY", "").strip()
    if single:
        return {single: "default"}
    return {}


def _is_public(path: str) -> bool:
    # Root path serves the simulator too
    if path == "/" or path == "":
        return True
    return any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _resolve_tenant(bearer: str, api_keys: dict[str, str]) -> Optional[str]:
    """Constant-time lookup: try every configured key, compare_digest each."""
    for key, tenant in api_keys.items():
        if hmac.compare_digest(bearer, key):
            return tenant
    return None


# ─── DB-backed key resolver (Sprint 6j) ──────────────────────────────────────
# Keys issued via POST /admin/tenants/{id}/api-keys are stored SHA-256-hashed
# in the api_keys table.  This helper hits the DB (with a 30s in-process
# cache) to look up the tenant for a bearer token.  Env-var keys still work
# as a bootstrap fallback for the very first tenant creation.

_DB_KEY_CACHE_TTL_S = 30.0
# {sha256_hash: (tenant_id, cached_at_ts)}
_db_key_cache: dict[str, tuple[str, float]] = {}


def _resolve_tenant_from_db(bearer: str) -> Optional[str]:
    """Hash the bearer, look it up in api_keys, update last_used_at."""
    key_hash = hashlib.sha256(bearer.encode()).hexdigest()

    # Cache hit — avoid DB round-trip on every request
    hit = _db_key_cache.get(key_hash)
    if hit is not None:
        tenant_id, cached_at = hit
        if time.time() - cached_at < _DB_KEY_CACHE_TTL_S:
            return tenant_id

    # DB miss — look up.  Import lazily to avoid circular deps at module load.
    try:
        from app.db import ApiKey
        from app.db.session import SessionLocal
        from datetime import datetime, timezone

        with SessionLocal() as db:
            # api_keys is tenant-scoped, so auto-filter would drop this query.
            # Bypass the filter for the auth path only.
            row = (
                db.query(ApiKey)
                .execution_options(skip_tenant_filter=True)
                .filter(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
                .one_or_none()
            )
            if row is None:
                return None
            tenant_id = row.tenant_id
            # Fire-and-forget last_used_at update
            try:
                row.last_used_at = datetime.now(timezone.utc)
                db.commit()
            except Exception:
                db.rollback()
            _db_key_cache[key_hash] = (tenant_id, time.time())
            return tenant_id
    except Exception as e:
        log.warning("DB api-key lookup failed: %s", e)
        return None


def invalidate_key_cache() -> None:
    """Called from /admin routes after key create/revoke so the cache picks
    up the change before the 30s TTL expires."""
    _db_key_cache.clear()


class AuthTenantMiddleware(BaseHTTPMiddleware):
    """Attach `request.state.tenant_id` on authenticated requests.

    Loads keys once at construction.  Reload requires a server restart —
    deliberate, so a compromised runtime cannot silently swap keys.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self.enforce = os.environ.get("API_AUTH_ENFORCE", "true").lower() not in ("0", "false", "no")
        self.api_keys = _load_api_keys()
        if self.enforce and not self.api_keys:
            log.error(
                "AUTH: enforcement is ON but no API keys configured.  Set API_KEY "
                "or API_KEYS_JSON, or set API_AUTH_ENFORCE=false for dev mode.  "
                "Every non-public route will return 401 until keys are configured."
            )
        elif not self.enforce:
            log.warning(
                "AUTH: enforcement is OFF (API_AUTH_ENFORCE=false).  Every route "
                "is world-open.  ONLY safe for local dev; NEVER in production."
            )
        else:
            log.info("AUTH: enforcement ON with %d configured API key(s)", len(self.api_keys))

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public paths bypass auth
        if _is_public(path):
            request.state.tenant_id = None
            return await call_next(request)

        # Dev/testing bypass
        if not self.enforce:
            request.state.tenant_id = "dev"
            # Sprint 6c: propagate to ORM contextvar so auto-inject sees it
            return await self._call_with_tenant("dev", request, call_next)

        # Authenticated path
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"detail": "missing Authorization: Bearer <api_key> header"},
                status_code=401,
            )
        provided = auth.removeprefix("Bearer ").strip()
        # Try env-configured keys first (bootstrap), then DB-issued keys.
        tenant = _resolve_tenant(provided, self.api_keys)
        if tenant is None:
            tenant = _resolve_tenant_from_db(provided)
        if tenant is None:
            return JSONResponse({"detail": "invalid API key"}, status_code=401)

        request.state.tenant_id = tenant
        return await self._call_with_tenant(tenant, request, call_next)

    async def _call_with_tenant(self, tenant: str, request: Request, call_next):
        """Set the ORM contextvar for the duration of this request.

        Sprint 6c: DB event listeners (auto-inject tenant_id on insert,
        cross-tenant leak guard) read this contextvar.  Reset in finally
        so the token doesn't leak into the next request on this worker.
        """
        # Import here to avoid circular import at module load
        from app.db.session import set_current_tenant, reset_current_tenant
        token = set_current_tenant(tenant)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)


def get_tenant_id(request: Request) -> str:
    """Handler dependency — returns the tenant_id for the current request.
    Raises 500 if middleware isn't wired (fail-loud on config drift)."""
    tenant = getattr(request.state, "tenant_id", None)
    if tenant is None:
        raise HTTPException(500, "auth middleware not wired — tenant_id missing")
    return tenant
