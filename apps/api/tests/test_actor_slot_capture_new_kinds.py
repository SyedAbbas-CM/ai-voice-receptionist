"""Actor auto-attach for the four new slot kinds (task #142).

Phone is already tested in test_actor_slot_capture_prompt_wire.py.
These tests exercise the same pattern for email/name/date/yes_no —
enter_slot_capture(kind=...) should build the right sub-agent
prompt automatically.
"""
from __future__ import annotations

import pytest

from apps.api.app.routes.twilio_actor import TwilioActorSession


class _FakeActor(TwilioActorSession):
    """Bypass __init__; stub the stall watchdog."""

    def __init__(self):
        self.call_id = "CAtest"
        self.tenant_id = "test-tenant"
        self._active_slot = None
        self._slot_normalizer = None
        self._slot_on_commit = None
        self._slot_on_stall = None
        self._slot_on_confirm_needed = None
        self._slot_stall_task = None
        self._slot_stall_first_prompt_s = 6.0
        self._slot_stall_escalate_s = 8.0
        self._active_slot_prompt = None

    def _arm_slot_stall_watchdog(self):
        pass

    def _cancel_slot_stall_watchdog(self):
        pass


async def _noop(result):
    pass


# ── email ─────────────────────────────────────


def test_email_kind_attaches_email_prompt():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="email", config={}, on_commit=_noop,
    )
    assert a.active_slot_prompt is not None
    assert "update_email" in a.active_slot_prompt.instructions
    assert "update_email" in a.active_slot_prompt.tools_hint


def test_email_modality_audio_mentions_dot_at():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="email", config={}, on_commit=_noop, modality="audio",
    )
    lower = a.active_slot_prompt.instructions.lower()
    assert "dot" in lower and "at" in lower


def test_email_modality_text_omits_spoken_block():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="email", config={}, on_commit=_noop, modality="text",
    )
    lower = a.active_slot_prompt.instructions.lower()
    assert "typed text" in lower
    assert "spelled letters" not in lower


# ── name ──────────────────────────────────────


def test_name_kind_attaches_name_prompt():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="name", config={}, on_commit=_noop,
    )
    assert a.active_slot_prompt is not None
    assert "update_name" in a.active_slot_prompt.instructions


def test_name_prompt_mentions_llm_junk_defense():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="name", config={}, on_commit=_noop,
    )
    lower = a.active_slot_prompt.instructions.lower()
    assert "null" in lower or "generic" in lower


# ── date ──────────────────────────────────────


def test_date_kind_attaches_date_prompt():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="date", config={"timezone": "UTC"}, on_commit=_noop,
    )
    assert a.active_slot_prompt is not None
    assert "update_date" in a.active_slot_prompt.instructions


def test_date_prompt_mentions_ambiguity():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="date", config={"timezone": "UTC"}, on_commit=_noop,
    )
    lower = a.active_slot_prompt.instructions.lower()
    assert "ambiguous" in lower or "narrow" in lower


# ── yes_no ──────────────────────────────────


def test_yes_no_kind_attaches_prompt():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="yes_no", config={}, on_commit=_noop,
    )
    assert a.active_slot_prompt is not None
    assert "update_yes_no" in a.active_slot_prompt.instructions


def test_yes_no_prompt_lists_yes_and_no_synonyms():
    a = _FakeActor()
    a.enter_slot_capture(
        kind="yes_no", config={}, on_commit=_noop,
    )
    lower = a.active_slot_prompt.instructions.lower()
    for kw in ("yeah", "sure", "nope", "cancel"):
        assert kw in lower, f"{kw!r} missing from yes/no prompt"


# ── cross-cutting: exit still clears + re-enter replaces ─────


def test_exit_clears_prompt_for_any_kind():
    for kind, config in [
        ("email", {}), ("name", {}), ("date", {"timezone": "UTC"}),
        ("yes_no", {}),
    ]:
        a = _FakeActor()
        a.enter_slot_capture(
            kind=kind, config=config, on_commit=_noop,
        )
        assert a.active_slot_prompt is not None
        a.exit_slot_capture(reason="test")
        assert a.active_slot_prompt is None


def test_switching_kinds_replaces_prompt():
    a = _FakeActor()
    a.enter_slot_capture(kind="email", config={}, on_commit=_noop)
    email_p = a.active_slot_prompt
    a.enter_slot_capture(kind="name", config={}, on_commit=_noop)
    assert a.active_slot_prompt is not email_p
    assert "update_name" in a.active_slot_prompt.instructions
    assert "update_email" not in a.active_slot_prompt.instructions
