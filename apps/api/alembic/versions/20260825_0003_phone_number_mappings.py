"""P0.4: phone_number_mappings table for tenant-from-caller resolution

Revision ID: 20260825_0003
Revises: 20260802_0002
Create Date: 2026-08-25

Ships one table: phone_number_mappings.  Every inbound Twilio call resolves
its tenant via the E.164 dialled number against this table BEFORE the WSS
handler dispatches to a brain.  Closes the P0.4 hole where every WSS call
was hardcoded `tenant_id="default"`.

Kept SEPARATE from the larger Day-4 schema bundle (SmsConsent, HIPAA fields,
integration_outbox, reception_messages, session.has_booking, bookings.status
CHECK) because P0.4 is a security-critical block for real customer traffic
and shouldn't wait on the humanness/CRM schema work to sequence itself out.

Safe to run:
  * Pure additive — new table, no existing-row backfill, no FK-touching.
  * Seeding happens via /admin/phone_mappings AFTER the migration lands.
  * Existing WSS callers continue working during migration because the
    resolver's dev-fallback env flag (PHONE_ROUTING_ALLOW_DEV_FALLBACK)
    can be flipped on for the changeover window.

Rollback:
  * downgrade drops the table.  Any WSS calls in flight when the table
    disappears will fail-closed and log the DB-lookup error.  Fine, this
    is a rollback.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260825_0003"
down_revision: Union[str, None] = "20260802_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phone_number_mappings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("phone_e164", sa.String, nullable=False),
        sa.Column(
            "tenant_id",
            sa.String,
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("business_id", sa.String, nullable=True),
        sa.Column("label", sa.String, nullable=False, server_default=""),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint("phone_e164", name="uq_phone_e164"),
    )
    op.create_index(
        "idx_phone_mapping_tenant",
        "phone_number_mappings",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_phone_mapping_tenant", table_name="phone_number_mappings")
    op.drop_table("phone_number_mappings")
