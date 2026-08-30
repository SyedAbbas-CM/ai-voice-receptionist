"""Annotation Dashboard Phase 1: call_annotations table

Revision ID: 20260830_0004
Revises: 20260825_0003
Create Date: 2026-08-30

## Why

Voice-agent training feedback loop. User records + reviewer annotates each
call with pass/fail verdict + per-turn tags + free-text notes. Later phases
(#96) auto-populate `auto_labels` via LK judges; Phase 4 (#97) matches
against a `is_gold` reference corpus for regression sweep on every deploy.

## Shape

  * `id` — PK, autoincrement so index scans are fast on recent rows.
  * `call_id` — Twilio CA SID (raw, no `twilio_` prefix). One annotation
    per call — enforced by unique index (annotator can re-save; upsert
    semantics).
  * `tenant_id` — tenant scope. Reviewer must be admin OR tenant-scoped.
  * `verdict` — 'win' | 'fail' | 'mixed' | 'unreviewed'. Free string
    (no CHECK constraint) so we can add labels later without a migration.
  * `turn_tags` — JSON list of {turn_idx, tag, comment}. Freeform now
    so the UI can iterate on tag vocabulary without schema churn.
    Example: [{"turn_idx": 3, "tag": "wrong_service_asked", "comment":
    "should have asked follow-up-of-what"}, ...]
  * `auto_labels` — JSON dict of {judge_name: {verdict, reasoning}}.
    Empty until Phase 3 wires the LK judges. Reviewer sees these
    alongside their own tags so they don't start blank.
  * `is_gold` — boolean flag for the golden corpus (Phase 4). Reviewer
    marks a well-behaved call as gold; regression sweep compares future
    similar-shaped calls against these.
  * `notes` — long-form freetext. What the reviewer wants to remember
    beyond structured tags.
  * `reviewer_id` — who annotated. Currently free-string (email, name);
    later ties to a real users table.
  * `created_at` / `updated_at` — timestamps.

## Not in v1

- No FK to sessions/transcript (call_id is enough; sessions may not exist
  for very-short calls that never got a session row).
- No RLS / per-row auth beyond tenant_id filter at query time.
- No history of edits (last-write-wins). If we need history, add
  `call_annotation_history` table later.

## Rollback

Pure additive. `downgrade` drops the table. No FK cascade to worry about.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0004"
down_revision: Union[str, None] = "20260825_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "call_annotations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String, nullable=False),
        sa.Column("tenant_id", sa.String, nullable=False, index=True),
        sa.Column(
            "verdict", sa.String, nullable=False, server_default="unreviewed",
        ),
        # JSON columns — SQLite stores as TEXT, Postgres as JSONB. Both
        # work with SQLAlchemy's sa.JSON abstract type.
        sa.Column("turn_tags", sa.JSON, nullable=True),
        sa.Column("auto_labels", sa.JSON, nullable=True),
        sa.Column(
            "is_gold", sa.Boolean, nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("reviewer_id", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        # One annotation per call. Save-again is UPDATE, not INSERT. UI
        # relies on this uniqueness for its upsert flow.
        sa.UniqueConstraint("call_id", name="uq_call_annotation_call_id"),
    )
    op.create_index(
        "idx_call_annotations_tenant_updated",
        "call_annotations",
        ["tenant_id", "updated_at"],
    )
    op.create_index(
        "idx_call_annotations_gold",
        "call_annotations",
        ["is_gold"],
    )


def downgrade() -> None:
    op.drop_index("idx_call_annotations_gold", table_name="call_annotations")
    op.drop_index(
        "idx_call_annotations_tenant_updated", table_name="call_annotations"
    )
    op.drop_table("call_annotations")
