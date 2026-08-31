"""Call recording columns on sessions

Revision ID: 20260831_0006
Revises: 20260830_0005
Create Date: 2026-08-31

## Why

Task #104-followup: reviewer wanted to be able to play back the audio
of each call, not just read the transcript. Twilio's server-side
<Record> verb is mutually exclusive with Media Streams, so we tee both
directions of audio in-app (packages/recording) and write a stereo MP3
to disk when the call ends.

## Shape

  * `recording_path` — relative path under data/recordings/ (e.g.
    "clinic/CA1234.mp3"). NULL when recording was disabled, failed,
    or the call had no audio.
  * `recording_duration_ms` — wall-clock duration in ms (matches the
    MP3 duration). 0 when no recording.
  * `recording_size_bytes` — MP3 file size for storage budgeting.

Nullable everywhere — existing rows carry NULL and continue to work.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260831_0006"
down_revision: Union[str, None] = "20260830_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("recording_path", sa.String, nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("recording_duration_ms", sa.Integer, nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("recording_size_bytes", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "recording_size_bytes")
    op.drop_column("sessions", "recording_duration_ms")
    op.drop_column("sessions", "recording_path")
