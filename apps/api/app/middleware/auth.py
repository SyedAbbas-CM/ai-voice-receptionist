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

import hmac
import json
import logging
import os
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
    "/call/",              # customer-facing widget static assets
    "/simulator/",         # dev-only widget static assets
    "/graph/",             # observability dashboard static assets
    "/apple-touch-icon",
    "/favicon.ico",
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
            return await call_next(request)

        # Authenticated path
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"detail": "missing Authorization: Bearer <api_key> header"},
                status_code=401,
            )
        provided = auth.removeprefix("Bearer ").strip()
        tenant = _resolve_tenant(provided, self.api_keys)
        if tenant is None:
            return JSONResponse({"detail": "invalid API key"}, status_code=401)

        request.state.tenant_id = tenant
        return await call_next(request)


def get_tenant_id(request: Request) -> str:
    """Handler dependency — returns the tenant_id for the current request.
    Raises 500 if middleware isn't wired (fail-loud on config drift)."""
    tenant = getattr(request.state, "tenant_id", None)
    if tenant is None:
        raise HTTPException(500, "auth middleware not wired — tenant_id missing")
    return tenant
