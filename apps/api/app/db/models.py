"""Persisted domain models.

Sprint 6 (2026-08-01) added tenant scoping to every row:

  * `Tenant` — the top-level owner. Everything else has a `tenant_id` FK.
  * `ApiKey` — hashed API keys, one → many with tenants.
  * `IdempotencyRow` — per-tenant `Idempotency-Key` → cached response, TTL'd.

Existing rows (SessionRow / TranscriptRow / BookingRow) gained a nullable
`tenant_id` for the backfill migration; the migration then flips them to
NOT NULL after populating existing rows with tenant_id="default".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC — audit STATE-020 (`datetime.utcnow` was deprecated)."""
    return datetime.now(timezone.utc)


# ─── Tenancy ─────────────────────────────────────────────────────────────────


class Tenant(Base):
    """The top-level owner of everything else.

    id = stable slug ("acme-dental", "corvina-restaurant") used everywhere as
    the tenant_id FK.  We keep it human-readable so support engineers can
    reason about a database row without a lookup table.
    """
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, default="starter")  # starter | pro | enterprise
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class ApiKey(Base):
    """Bearer keys mapped to a tenant.  Store only the SHA-256 hash — the
    plaintext is returned exactly once at creation and never stored."""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String)  # first 12 chars for display only
    name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ─── Idempotency ─────────────────────────────────────────────────────────────


class IdempotencyRow(Base):
    """Idempotency-Key -> cached response, per tenant.

    Sprint 6d.  Applied on booking + webhook endpoints so provider retries
    (Twilio, Vapi, WhatsApp, Stripe) can't double-book / double-charge / etc.
    TTL default 24h, cleanup cron drops expired rows.
    """
    __tablename__ = "idempotency"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),
        Index("idx_idempotency_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String)  # Idempotency-Key header OR provider event_id
    scope: Mapped[str] = mapped_column(String)  # "booking" | "webhook:vapi" | "webhook:twilio" | ...
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    response_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: _utcnow() + timedelta(hours=24)
    )


# ─── Core domain (extended with tenant_id) ───────────────────────────────────


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_tenant_started", "tenant_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # nullable=True initially so the backfill migration doesn't blow up.
    # Sprint 6c flips to NOT NULL after backfill.  New rows always get one
    # from the auto-inject listener.
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    business_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    extracted: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    escalation_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    transcript: Mapped[list["TranscriptRow"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class TranscriptRow(Base):
    __tablename__ = "transcript"
    __table_args__ = (
        Index("idx_transcript_tenant_session", "tenant_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    tool_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tool_args: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    session: Mapped[SessionRow] = relationship(back_populates="transcript")


class BookingRow(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("idx_bookings_tenant_business", "tenant_id", "business_id"),
        Index("idx_bookings_tenant_scheduled", "tenant_id", "scheduled_for"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    business_id: Mapped[str] = mapped_column(String, index=True)
    caller_name: Mapped[str] = mapped_column(String)
    phone: Mapped[str] = mapped_column(String)
    service: Mapped[str] = mapped_column(String)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String, default="confirmed")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# ─── Telephony identity → tenant mapping ─────────────────────────────────────
#
# 2026-08-25 P0.4 (BACKEND-AUDIT-2026-08-25-CHATGPT.md#4):
# The Twilio WSS handler used to hardcode `tenant_id="default"` on every
# inbound call, regardless of which of a tenant's phone numbers Twilio dialed
# into. That let anyone hitting the Media Streams endpoint be treated as
# tenant "default" for the whole call lifetime — combined with the P0.1
# supertenant bypass, this leaked cross-tenant call content.
#
# This table maps `E.164 called-number` → `tenant_id`. On WSS start, the
# handler pulls the `to` custom parameter (set in the TwiML by /twilio/voice)
# and resolves the real tenant here BEFORE dispatching the call to a brain.
#
# `business_id` is optional because a tenant may operate multiple businesses
# (multi-location clinics, franchise groups) that share one Twilio number;
# routing to a specific business happens by rule inside the tenant's config.
# When set, it wins over the tenant-default business_id resolver.
class PhoneNumberMapping(Base):
    __tablename__ = "phone_number_mappings"
    __table_args__ = (
        # `phone_e164` is the primary lookup key on every inbound call —
        # must be O(1). Unique because one phone number cannot resolve
        # to two tenants (that would be catastrophic ambiguity).
        UniqueConstraint("phone_e164", name="uq_phone_e164"),
        Index("idx_phone_mapping_tenant", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # E.164 format ("+15551234567", "+351215550192"). Twilio always sends
    # the `To` field in E.164, so no normalization needed on the read path.
    phone_e164: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    # Optional — when set, all calls to this number auto-assign to this
    # business inside the tenant. Otherwise the tenant's default resolver runs.
    business_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Human label for admin UIs — "Smile Dental main line", "Ribeira Prime EN line".
    label: Mapped[str] = mapped_column(String, default="")
    # If revoked, resolver treats the number as unknown → call is refused.
    # Prevents accidentally sending calls to a paused tenant.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
