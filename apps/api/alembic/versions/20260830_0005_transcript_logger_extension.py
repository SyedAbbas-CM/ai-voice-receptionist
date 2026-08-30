"""Phase 2: transcript logger extension for LK judges (task #95)

Revision ID: 20260830_0005
Revises: 20260830_0004
Create Date: 2026-08-30

## Why

Phase 3 wires LK judges (task_completion, accuracy, tool_use). Those
judges grade AGAINST the effective instructions AT THAT TURN. Our LK
slot-capture wire (task #97) swaps the wide system prompt for a narrow
sub-agent prompt during phone-capture turns. Judge needs to know that;
otherwise it grades the sub-agent-scope turn under the wrong lens.

## Fields added (nullable, backwards-compat)

  * `sessions.opening_system_prompt: TEXT` — the wider agent persona
    prompt snapshot once at call start. Judge fallback when no delta.
  * `transcript.agent_instructions_delta: TEXT` — NULL on most turns,
    populated ONLY when instructions change. Sub-agent enter writes the
    narrow prompt here; exit writes the sentinel 'exit_slot_capture'.
    Judge walks backward from a turn to find the most recent non-null
    delta.
  * `transcript.tool_error: TEXT` — distinct from tool_result=null.
    Tool errors currently collapse to null which is ambiguous. Errors
    populate this field; successful returns leave it null.

## Storage estimate

  * opening_system_prompt: ~5KB/call × 1000 calls/day = 5MB/day
  * agent_instructions_delta: ~500B/call average (only capture turns
    write) = 500KB/day
  * tool_error: ~100B when populated = negligible

100× smaller than naive per-turn snapshot of everything.

## Rollback

Pure additive nullable fields. `downgrade` drops them cleanly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0005"
down_revision: Union[str, None] = "20260830_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # sessions.opening_system_prompt — the wider agent persona prompt.
    # Nullable so pre-existing rows work; new rows populate at call start.
    op.add_column(
        "sessions",
        sa.Column("opening_system_prompt", sa.Text, nullable=True),
    )
    # transcript.agent_instructions_delta — populated only on scope
    # changes. Non-null values include the narrow sub-agent prompt text
    # OR the sentinel 'exit_slot_capture'.
    op.add_column(
        "transcript",
        sa.Column("agent_instructions_delta", sa.Text, nullable=True),
    )
    # transcript.tool_error — distinct from tool_result=null. Contains
    # the error message when a tool call raised; null on success.
    op.add_column(
        "transcript",
        sa.Column("tool_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcript", "tool_error")
    op.drop_column("transcript", "agent_instructions_delta")
    op.drop_column("sessions", "opening_system_prompt")
