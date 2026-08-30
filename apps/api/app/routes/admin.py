"""Admin routes — tenant onboarding.

Sprint 6f: Three endpoints for provisioning new customers.  Gated by an
admin token distinct from tenant API keys (ADMIN_TOKEN env).  Do NOT
expose these behind the same auth surface as tenant routes.

  * POST /admin/tenants                  — create a tenant
  * POST /admin/tenants/{id}/api-keys    — issue a bearer key (returns
                                            plaintext ONCE, hash stored)
  * POST /admin/tenants/{id}/businesses  — provision a business profile
                                            for a tenant

The API key returned by /api-keys is never retrievable again.  Losing it
means issuing a new one and revoking the old one.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import ApiKey, Tenant
from app.db.session import get_session, set_current_tenant, reset_current_tenant


router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(request: Request) -> None:
    """Admin routes accept ANY of:
      1. Signed session cookie `voiceops_admin` (browser login flow — task #99)
      2. Bearer token `ADMIN_TOKEN` in Authorization header (curl / CI)
    Fail-closed with a 401 that hints at the login page if BOTH missing.

    503 only when NEITHER credential type is configured — signals a
    misconfigured box vs a legitimate 401 on a real login attempt."""
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    session_secret = os.environ.get("SESSION_COOKIE_SECRET", "")

    # Both auth paths disabled → admin routes are dark.
    if not admin_token and not (password_hash and session_secret):
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin routes are disabled. Configure either ADMIN_TOKEN "
                "(bearer) or ADMIN_PASSWORD_HASH + SESSION_COOKIE_SECRET "
                "(browser login)."
            ),
        )

    # Path 1: signed session cookie (browser). Only checked if configured.
    if password_hash and session_secret:
        try:
            from app.routes.admin_login import verify_admin_session
            if verify_admin_session(request) is not None:
                return
        except Exception:
            # Session module errors must NOT block bearer-path fallback.
            pass

    # Path 2: bearer token (curl). Only checked if configured.
    if admin_token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            provided = auth.removeprefix("Bearer ").strip()
            if hmac.compare_digest(provided, admin_token):
                return

    # Neither credential accepted. If password login IS configured, hint
    # at the login page; otherwise keep the bearer-only message.
    if password_hash and session_secret:
        raise HTTPException(
            status_code=401,
            detail="Not signed in. Go to /admin/login",
        )
    raise HTTPException(status_code=401, detail="admin bearer required")


class CreateTenantRequest(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str = Field(..., min_length=1, max_length=200)
    plan: str = Field(default="starter", pattern=r"^(starter|pro|enterprise)$")


class CreateTenantResponse(BaseModel):
    id: str
    name: str
    plan: str
    created_at: datetime


class CreateApiKeyRequest(BaseModel):
    name: str = Field(default="", max_length=200)


class CreateApiKeyResponse(BaseModel):
    key: str  # PLAINTEXT — returned exactly once, never stored
    prefix: str
    created_at: datetime


class ProvisionBusinessRequest(BaseModel):
    profile: dict = Field(..., description="Business profile JSON — same shape as sample-data/*.json")


class ProvisionBusinessResponse(BaseModel):
    business_id: str
    tenant_id: str


@router.post("/tenants", response_model=CreateTenantResponse)
def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    db: Session = Depends(get_session),
) -> CreateTenantResponse:
    _require_admin(request)
    existing = db.get(Tenant, body.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"tenant '{body.id}' already exists")

    # Tenants table is a global table; the guard's allow_cross_tenant is
    # implicit because "tenants" is NOT in _TENANT_SCOPED_TABLES.
    tenant = Tenant(id=body.id, name=body.name, plan=body.plan)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return CreateTenantResponse(
        id=tenant.id, name=tenant.name, plan=tenant.plan, created_at=tenant.created_at,
    )


@router.post("/tenants/{tenant_id}/api-keys", response_model=CreateApiKeyResponse)
def create_api_key(
    tenant_id: str,
    body: CreateApiKeyRequest,
    request: Request,
    db: Session = Depends(get_session),
) -> CreateApiKeyResponse:
    _require_admin(request)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    # Generate a random URL-safe key with a prefix that identifies it as ours.
    plaintext = f"vk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    prefix = plaintext[:12]

    # api_keys table is tenant-scoped, so we need the ORM contextvar set
    # so the auto-inject listener stamps tenant_id.
    token = set_current_tenant(tenant_id)
    try:
        row = ApiKey(
            tenant_id=tenant_id,
            key_hash=key_hash,
            key_prefix=prefix,
            name=body.name,
        )
        db.add(row)
        db.commit()
    finally:
        reset_current_tenant(token)

    # Sprint 6j: invalidate the auth cache so the new key is immediately usable
    from app.middleware.auth import invalidate_key_cache
    invalidate_key_cache()

    return CreateApiKeyResponse(
        key=plaintext,   # ONLY chance to see this — never stored plaintext
        prefix=prefix,
        created_at=datetime.now(timezone.utc),
    )


@router.post("/tenants/{tenant_id}/businesses", response_model=ProvisionBusinessResponse)
def provision_business(
    tenant_id: str,
    body: ProvisionBusinessRequest,
    request: Request,
    db: Session = Depends(get_session),
) -> ProvisionBusinessResponse:
    """Register a business profile against a tenant.

    Sprint 6f interim implementation: profile is stored on the Tenant's
    metadata_json field.  Sprint 7 will introduce a first-class
    BusinessProfileRow table.  For now this endpoint exists so onboarding
    is a single API flow.
    """
    _require_admin(request)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    profile = body.profile
    business_id = profile.get("id") or ""
    if not business_id:
        raise HTTPException(status_code=400, detail="business profile must have an 'id' field")

    metadata = dict(tenant.metadata_json or {})
    businesses = list(metadata.get("businesses", []))
    # Replace if exists, else append
    businesses = [b for b in businesses if b.get("id") != business_id]
    businesses.append(profile)
    metadata["businesses"] = businesses
    tenant.metadata_json = metadata
    db.add(tenant)
    db.commit()

    return ProvisionBusinessResponse(business_id=business_id, tenant_id=tenant_id)


@router.get("/tenants/{tenant_id}")
def get_tenant(
    tenant_id: str,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    _require_admin(request)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {
        "id": tenant.id,
        "name": tenant.name,
        "plan": tenant.plan,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "disabled_at": tenant.disabled_at.isoformat() if tenant.disabled_at else None,
        "businesses": (tenant.metadata_json or {}).get("businesses", []),
    }
