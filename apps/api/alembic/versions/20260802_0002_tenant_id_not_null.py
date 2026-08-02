"""sprint 6i: tenant_id NOT NULL on all tenant-scoped tables

Revision ID: 20260802_0002
Revises: 20260801_0001
Create Date: 2026-08-02

The backfill in 20260801_0001 populated existing rows with tenant_id="default".
Every writer path now goes through the auto-inject event listener.  This
migration flips the columns to NOT NULL as a defense-in-depth guarantee
against orphan rows in future.

Safe to run:
  * Backfill migration is required prior (Alembic enforces via depends_on).
  * If somehow a row still has NULL tenant_id, the migration will fail with
    a clear error — investigate before proceeding.

Rollback:
  * Downgrade re-widens to nullable.  Existing rows keep their tenant_id.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260802_0002"
down_revision: Union[str, None] = "20260801_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Safety check: any NULL tenant_id row would break the migration.  Log
    # a helpful error via a pre-check before ALTER.
    conn = op.get_bind()
    for table in ("sessions", "transcript", "bookings"):
        result = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id IS NULL")
        ).scalar()
        if result and result > 0:
            raise RuntimeError(
                f"cannot tighten {table}.tenant_id to NOT NULL — {result} "
                f"rows still have NULL tenant_id.  Run "
                f"'UPDATE {table} SET tenant_id = ''default'' WHERE tenant_id "
                f"IS NULL' or investigate why 20260801_0001 backfill missed them."
            )

    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=False)

    with op.batch_alter_table("transcript") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=False)

    with op.batch_alter_table("bookings") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=True)

    with op.batch_alter_table("transcript") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=True)

    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(), nullable=True)
