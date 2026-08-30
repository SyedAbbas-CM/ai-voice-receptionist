"""Phase 2 acceptance — transcript logger extension for LK judges (task #95).

Tests:
  1. New columns exist on the models (schema contract)
  2. TranscriptTurn schema accepts the new optional fields
  3. persist_session writes agent_instructions_delta + tool_error when
     the Turn has them
  4. persist_session leaves NULL when Turn doesn't
  5. SessionRow.opening_system_prompt written from state at first
     persist, not overwritten on subsequent persists
  6. Backwards-compat: old Turn objects without the new fields still
     persist cleanly (no AttributeError)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ─── 1. Schema contract ─────────────────────────────────────────────────────


def test_session_row_has_opening_system_prompt():
    from app.db import SessionRow
    assert hasattr(SessionRow, "opening_system_prompt")


def test_transcript_row_has_new_columns():
    from app.db import TranscriptRow
    assert hasattr(TranscriptRow, "agent_instructions_delta")
    assert hasattr(TranscriptRow, "tool_error")


# ─── 2. TranscriptTurn schema ───────────────────────────────────────────────


def test_transcript_turn_accepts_new_fields():
    from packages.schemas.call import TranscriptTurn, TurnRole
    turn = TranscriptTurn(
        role=TurnRole.ASSISTANT,
        text="hi",
        agent_instructions_delta="You are only a single step...",
        tool_error=None,
    )
    assert turn.agent_instructions_delta == "You are only a single step..."


def test_transcript_turn_defaults_new_fields_to_none():
    from packages.schemas.call import TranscriptTurn, TurnRole
    turn = TranscriptTurn(role=TurnRole.USER, text="hello")
    assert turn.agent_instructions_delta is None
    assert turn.tool_error is None


# ─── 3-6. Persistence round-trip ────────────────────────────────────────────


def _mk_state(session_id: str, tenant_id: str = "clinic"):
    """Build a minimal CallState-like object for persist_session."""
    from packages.schemas.call import CallState, CallStatus
    return CallState(
        session_id=session_id,
        tenant_id=tenant_id,
        business_id="clinic-main",
        status=CallStatus.ACTIVE,
        transcript=[],
        started_at=datetime.now(timezone.utc),
    )


def test_persist_writes_delta_and_tool_error(tmp_path, monkeypatch):
    """Turn with agent_instructions_delta + tool_error → those land in
    the corresponding TranscriptRow columns."""
    from packages.schemas.call import TranscriptTurn, TurnRole
    from app.core.session_manager import _persist_session as persist_session
    from app.db import TranscriptRow
    from app.db.session import SessionLocal

    session_id = f"twilio_CAphase2_{tmp_path.name[:6]}"
    state = _mk_state(session_id)
    state.transcript = [
        TranscriptTurn(
            role=TurnRole.ASSISTANT,
            text="Give me your phone number, one digit at a time.",
            agent_instructions_delta="You are only a single step in a broader system, responsible solely for capturing a phone number.",
        ),
        TranscriptTurn(
            role=TurnRole.TOOL,
            text='{"error": "libphonenumber parse failed"}',
            tool_name="update_phone",
            tool_args={"phone": "abc"},
            tool_result=None,
            tool_error="libphonenumber parse failed: invalid country code",
        ),
    ]

    persist_session(state)

    with SessionLocal() as db:
        rows = (
            db.query(TranscriptRow)
            .filter(TranscriptRow.session_id == session_id)
            .order_by(TranscriptRow.id.asc())
            .all()
        )
    assert len(rows) == 2
    # First turn: delta populated, no tool_error
    assert rows[0].agent_instructions_delta is not None
    assert "single step" in rows[0].agent_instructions_delta
    assert rows[0].tool_error is None
    # Second turn: tool_error populated, no delta
    assert rows[1].agent_instructions_delta is None
    assert rows[1].tool_error == "libphonenumber parse failed: invalid country code"


def test_persist_leaves_null_when_turn_has_no_new_fields(tmp_path):
    """Old-shape Turn (no delta, no error) → both columns null."""
    from packages.schemas.call import TranscriptTurn, TurnRole
    from app.core.session_manager import _persist_session as persist_session
    from app.db import TranscriptRow
    from app.db.session import SessionLocal

    session_id = f"twilio_CAphase2b_{tmp_path.name[:6]}"
    state = _mk_state(session_id)
    state.transcript = [
        TranscriptTurn(role=TurnRole.USER, text="hi"),
        TranscriptTurn(role=TurnRole.ASSISTANT, text="hello!"),
    ]
    persist_session(state)

    with SessionLocal() as db:
        rows = (
            db.query(TranscriptRow)
            .filter(TranscriptRow.session_id == session_id)
            .all()
        )
    for r in rows:
        assert r.agent_instructions_delta is None
        assert r.tool_error is None


def test_opening_system_prompt_written_once(tmp_path):
    """First persist with `_opening_system_prompt` on state → written.
    Subsequent persists with a DIFFERENT `_opening_system_prompt` do
    NOT overwrite (only sub-agent scope deltas may follow)."""
    from packages.schemas.call import TranscriptTurn, TurnRole
    from app.core.session_manager import _persist_session as persist_session
    from app.db import SessionRow
    from app.db.session import SessionLocal

    session_id = f"twilio_CAphase2c_{tmp_path.name[:6]}"
    state = _mk_state(session_id)
    state._opening_system_prompt = "You are the friendly receptionist for Smile Dental Clinic..."
    state.transcript = [TranscriptTurn(role=TurnRole.ASSISTANT, text="hi")]
    persist_session(state)

    with SessionLocal() as db:
        row = db.query(SessionRow).filter(SessionRow.id == session_id).one()
    first = row.opening_system_prompt
    assert first is not None
    assert "Smile Dental" in first

    # Second persist with DIFFERENT opening (simulating a sub-agent
    # scope swap trying to overwrite). Should be a NO-OP for this
    # column — sub-agent scope goes into agent_instructions_delta
    # instead, not into opening_system_prompt.
    state._opening_system_prompt = "DIFFERENT — should not overwrite"
    persist_session(state)
    with SessionLocal() as db:
        row2 = db.query(SessionRow).filter(SessionRow.id == session_id).one()
    assert row2.opening_system_prompt == first, (
        "opening_system_prompt should be written ONCE at call start, "
        "not overwritten on subsequent persists. Sub-agent scope swaps "
        "belong in per-turn agent_instructions_delta."
    )


def test_persist_backwards_compat_turn_without_new_attrs(tmp_path):
    """A Turn-like duck-typed object WITHOUT the new fields (from a
    pre-Phase-2 caller) must persist cleanly — no AttributeError."""
    from packages.schemas.call import TurnRole
    from app.core.session_manager import _persist_session as persist_session
    from app.db import TranscriptRow
    from app.db.session import SessionLocal

    class LegacyTurn:
        """Simulates a hand-rolled Turn from before Phase 2 fields existed."""
        def __init__(self):
            self.role = TurnRole.USER
            self.text = "legacy caller"
            self.timestamp = datetime.now(timezone.utc)
            self.tool_name = None
            self.tool_args = None
            self.tool_result = None
            # NOTE: no agent_instructions_delta, no tool_error

    session_id = f"twilio_CAphase2d_{tmp_path.name[:6]}"
    state = _mk_state(session_id)
    state.transcript = [LegacyTurn()]  # type: ignore

    # Should not raise
    persist_session(state)

    with SessionLocal() as db:
        rows = (
            db.query(TranscriptRow)
            .filter(TranscriptRow.session_id == session_id)
            .all()
        )
    assert len(rows) == 1
    assert rows[0].agent_instructions_delta is None
    assert rows[0].tool_error is None
