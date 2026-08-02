"""sprint 6: tenants + api_keys + idempotency + tenant_id on domain rows

Revision ID: 20260801_0001
Revises:
Create Date: 2026-08-01

Adds the multi-tenancy scaffolding.  Existing rows are backfilled with
tenant_id = "default" so single-tenant deployments keep working.
Sprint 6c will follow up with a NOT NULL migration once every writer path
is confirmed to auto-inject tenant_id.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260801_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. tenants ────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=False, server_default="starter"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )

    # Seed the "default" tenant so existing single-tenant deployments keep
    # working after the migration.  Every backfilled row points to this.
    op.execute(
        "INSERT INTO tenants (id, name, plan, created_at) VALUES "
        "('default', 'Default tenant (pre-multi-tenant deploys)', 'starter', CURRENT_TIMESTAMP)"
    )

    # ── 2. api_keys ───────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("key_hash", sa.String(), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    # ── 3. idempotency ────────────────────────────────────────────────
    op.create_table(
        "idempotency",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_idempotency_tenant_key", "idempotency", ["tenant_id", "key"],
    )
    op.create_index("idx_idempotency_expires", "idempotency", ["expires_at"])

    # ── 4. tenant_id on existing domain tables (nullable for backfill) ──
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(), nullable=True))
        batch.create_index("ix_sessions_tenant_id", ["tenant_id"])
        batch.create_index("idx_sessions_tenant_started", ["tenant_id", "started_at"])

    with op.batch_alter_table("transcript") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(), nullable=True))
        batch.create_index("ix_transcript_tenant_id", ["tenant_id"])
        batch.create_index("idx_transcript_tenant_session", ["tenant_id", "session_id"])

    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(), nullable=True))
        batch.create_index("ix_bookings_tenant_id", ["tenant_id"])
        batch.create_index("idx_bookings_tenant_business", ["tenant_id", "business_id"])
        batch.create_index("idx_bookings_tenant_scheduled", ["tenant_id", "scheduled_for"])

    # ── 5. Backfill existing rows → tenant_id = "default" ─────────────
    op.execute("UPDATE sessions SET tenant_id = 'default' WHERE tenant_id IS NULL")
    op.execute("UPDATE transcript SET tenant_id = 'default' WHERE tenant_id IS NULL")
    op.execute("UPDATE bookings SET tenant_id = 'default' WHERE tenant_id IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.drop_index("idx_bookings_tenant_scheduled")
        batch.drop_index("idx_bookings_tenant_business")
        batch.drop_index("ix_bookings_tenant_id")
        batch.drop_column("tenant_id")

    with op.batch_alter_table("transcript") as batch:
        batch.drop_index("idx_transcript_tenant_session")
        batch.drop_index("ix_transcript_tenant_id")
        batch.drop_column("tenant_id")

    with op.batch_alter_table("sessions") as batch:
        batch.drop_index("idx_sessions_tenant_started")
        batch.drop_index("ix_sessions_tenant_id")
        batch.drop_column("tenant_id")

    op.drop_index("idx_idempotency_expires", table_name="idempotency")
    op.drop_constraint("uq_idempotency_tenant_key", "idempotency", type_="unique")
    op.drop_table("idempotency")

    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_table("tenants")
