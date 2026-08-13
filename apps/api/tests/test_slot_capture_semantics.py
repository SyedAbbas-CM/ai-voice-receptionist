"""R3 P2 corrections: slot-capture semantics required by the workflow
controller architecture.

These tests pin down four properties the actor MUST enforce (per
2026-08-14 ChatGPT review):

  1. VALID auto-commits.  POSSIBLE does NOT — it fires on_confirm_needed
     so the workflow can ask "is that right?".
  2. INVALID stays inside the structured subsystem.  It never returns
     False (which would resume the general LLM).  Instead it fires
     on_stall(stage="escalate") so a deterministic recovery kicks in.
  3. Every fragment rearms the stall watchdog.  Silence → on_stall
     stage="first_prompt", then "escalate".
  4. The workflow controller (not tools) opens capture.  Tools receive
     validated values.  The API shape must expose confirm/stall hooks
     so a workflow can drive the whole recovery cycle without the LLM.

Rather than instantiate the full TwilioActorSession (which needs a
WebSocket, registry, session_manager…), we exercise the same slot
methods through a minimal shim class that inherits only the slot
methods.  If any of these tests fail, the actor's structured-input
contract is broken — do NOT loosen the assertions.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import pytest

from packages.slot_parsers import (
    SlotResult,
    SlotSource,
    SlotStatus,
    StructuredInputSession,
)


async def _noop(*args, **kwargs) -> None:
    return None


# ── minimal harness: reuse the actor's slot methods, skip all telephony ──
#
# We import the actor module lazily and grab just the slot-capture
# methods so we don't need to spin up a WebSocket / registry / STT
# bridge just to exercise slot semantics.


class _SlotHarness:
    """Duck-types enough of TwilioActorSession for the slot methods to
    work.  The slot methods only touch: self._active_slot,
    self._slot_normalizer, self._slot_on_commit, self._slot_on_stall,
    self._slot_on_confirm_needed, self._slot_stall_task, the two
    stall timeouts, self.call_id, and the module logger."""

    call_id = "TEST_CALL"

    _SLOT_STALL_FIRST_PROMPT_S = 0.05
    _SLOT_STALL_ESCALATE_S = 0.05

    def __init__(self) -> None:
        self._active_slot: Optional[StructuredInputSession] = None
        self._slot_normalizer = None
        self._slot_on_commit = None
        self._slot_on_stall = None
        self._slot_on_confirm_needed = None
        self._slot_stall_task: Optional[asyncio.Task] = None
        self._slot_stall_first_prompt_s: float = self._SLOT_STALL_FIRST_PROMPT_S
        self._slot_stall_escalate_s: float = self._SLOT_STALL_ESCALATE_S


def _bind_slot_methods() -> _SlotHarness:
    """Return a harness with the real slot methods bound to it."""
    # Import inside the function so a broken actor import doesn't fail
    # collection of unrelated tests.
    from app.routes.twilio_actor import TwilioActorSession

    h = _SlotHarness()
    for name in (
        "enter_slot_capture",
        "exit_slot_capture",
        "slot_capture_active",
        "_arm_slot_stall_watchdog",
        "_cancel_slot_stall_watchdog",
        "_slot_stall_loop",
        "_feed_active_slot",
    ):
        attr = getattr(TwilioActorSession, name)
        # property descriptor: assign at class level via a subclass trick
        if isinstance(attr, property):
            setattr(type(h), name, attr)
        else:
            setattr(h, name, attr.__get__(h, type(h)))
    return h


def _phone_config(default_region: str = "US",
                  accepted=None) -> dict:
    return {
        "phone_default_region": default_region,
        "phone_accepted_regions": accepted or [default_region],
    }


# ── (1) VALID auto-commits; POSSIBLE does NOT ────────────────────────

@pytest.mark.asyncio
async def test_valid_autocommits_and_fires_on_commit():
    h = _bind_slot_methods()
    committed: List[SlotResult] = []

    async def on_commit(result: SlotResult) -> None:
        committed.append(result)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("PK"),
        on_commit=on_commit,
    )
    # Full 11-digit PK number — VALID.
    consumed = await h._feed_active_slot(
        "03335244772", turn_gen=1, source=SlotSource.SPEECH,
    )
    assert consumed is True
    assert len(committed) == 1
    assert committed[0].status == SlotStatus.VALID
    assert committed[0].value == "+923335244772"
    # Capture must be closed after VALID commit.
    assert h.slot_capture_active is False


@pytest.mark.asyncio
async def test_possible_does_not_autocommit_fires_confirm_hook():
    """POSSIBLE (right shape, not verified) must ask the caller before
    committing.  This protects against libphonenumber metadata lag for
    newly allocated ranges — and, more generally, any future validator
    that distinguishes 'looks right' from 'is right'."""
    h = _bind_slot_methods()
    committed: List[SlotResult] = []
    confirm_asked: List[SlotResult] = []

    async def on_commit(r): committed.append(r)
    async def on_confirm(r): confirm_asked.append(r)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("US"),
        on_commit=on_commit,
        on_confirm_needed=on_confirm,
    )

    # Force a POSSIBLE result by feeding a canonical directly into a
    # session whose validator returns POSSIBLE.  We do this by
    # monkey-patching the active slot's validator so the test doesn't
    # depend on libphonenumber's allocation tables shifting under it.
    def possible_validator(canonical: str, config: dict) -> SlotResult:
        return SlotResult(
            status=SlotStatus.POSSIBLE,
            value="+15551234567",
            matched_region="US",
            reason="test: force POSSIBLE",
            raw_digits=canonical,
        )
    h._active_slot.validator = possible_validator

    consumed = await h._feed_active_slot(
        "5551234567", turn_gen=1, source=SlotSource.SPEECH,
    )
    assert consumed is True
    # Confirm hook fired; commit hook did NOT.
    assert len(confirm_asked) == 1
    assert confirm_asked[0].value == "+15551234567"
    assert committed == []
    # Capture must STILL be active so the workflow can re-enter or
    # commit after the caller confirms.
    assert h.slot_capture_active is True


@pytest.mark.asyncio
async def test_possible_without_hook_stays_in_capture():
    """If workflow forgets to pass on_confirm_needed, do NOT silently
    auto-commit.  Stay in capture and log a warning — safe default."""
    h = _bind_slot_methods()
    committed: List[SlotResult] = []

    async def on_commit(r): committed.append(r)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("US"),
        on_commit=on_commit,  # no on_confirm_needed
    )
    def possible_validator(canonical, config):
        return SlotResult(
            status=SlotStatus.POSSIBLE, value="+15551234567",
            matched_region="US", raw_digits=canonical,
        )
    h._active_slot.validator = possible_validator

    consumed = await h._feed_active_slot("5551234567", turn_gen=1)
    assert consumed is True
    assert committed == []
    assert h.slot_capture_active is True


# ── (2) INVALID stays inside structured subsystem ────────────────────

@pytest.mark.asyncio
async def test_invalid_does_not_escape_to_llm():
    """INVALID must NOT return False (which would resume Brain).  It
    fires on_stall(stage='escalate') so the workflow can decide the
    recovery path (re-prompt, DTMF fallback, transfer)."""
    h = _bind_slot_methods()
    stalls: List[Tuple[str, StructuredInputSession]] = []

    async def on_stall(stage, session):
        stalls.append((stage, session))

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("US"),
        on_commit=_noop,
        on_stall=on_stall,
    )
    def invalid_validator(canonical, config):
        return SlotResult(
            status=SlotStatus.INVALID,
            reason="test: forced INVALID",
            raw_digits=canonical,
        )
    h._active_slot.validator = invalid_validator

    consumed = await h._feed_active_slot("garbage", turn_gen=1)
    # Consumed = True — brain must NOT run.
    assert consumed is True
    # Stall handler fired with escalate.
    assert len(stalls) == 1
    assert stalls[0][0] == "escalate"
    # Capture stays active — workflow decides next step.
    assert h.slot_capture_active is True


# ── (3) Stall watchdog: two-stage recovery ───────────────────────────

@pytest.mark.asyncio
async def test_stall_watchdog_fires_first_prompt_then_escalate():
    h = _bind_slot_methods()
    events: List[str] = []

    async def on_stall(stage, session):
        events.append(stage)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("US"),
        on_commit=_noop,
        on_stall=on_stall,
        stall_first_prompt_s=0.03,
        stall_escalate_s=0.03,
    )
    # Silence — do nothing.  Give the watchdog time to fire both stages.
    await asyncio.sleep(0.15)
    assert events == ["first_prompt", "escalate"]
    # Cleanup — cancel any pending task.
    h.exit_slot_capture(reason="test-teardown")


@pytest.mark.asyncio
async def test_fragment_rearms_stall_watchdog():
    """A fragment landing during the first-prompt window must reset
    the timer so we don't nag a caller who is still dictating."""
    h = _bind_slot_methods()
    events: List[str] = []

    async def on_stall(stage, session):
        events.append(stage)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("PK"),
        on_commit=_noop,
        on_stall=on_stall,
        stall_first_prompt_s=0.05,
        stall_escalate_s=0.05,
    )
    # Send a fragment before the first-prompt fires — it should reset
    # the timer.  Then wait less than the full cycle → no stall.
    await asyncio.sleep(0.03)
    await h._feed_active_slot("zero three three", turn_gen=1)
    await asyncio.sleep(0.03)
    assert events == []  # timer was rearmed
    # Now stay silent past both stages.
    await asyncio.sleep(0.15)
    assert events == ["first_prompt", "escalate"]
    h.exit_slot_capture(reason="test-teardown")


@pytest.mark.asyncio
async def test_exit_cancels_stall_watchdog():
    h = _bind_slot_methods()
    events: List[str] = []
    async def on_stall(stage, session): events.append(stage)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("US"),
        on_commit=_noop,
        on_stall=on_stall,
        stall_first_prompt_s=0.05,
        stall_escalate_s=0.05,
    )
    h.exit_slot_capture(reason="test-early-exit")
    await asyncio.sleep(0.2)
    assert events == []


# ── (4) Workflow controller ergonomics ───────────────────────────────

@pytest.mark.asyncio
async def test_second_enter_replaces_prior_capture():
    """Workflow controller may switch slots (e.g. phone → email).  A
    second enter_slot_capture must cleanly close the prior one.  This
    is the API surface a workflow needs — one active slot at a time."""
    h = _bind_slot_methods()

    h.enter_slot_capture(
        kind="phone", config=_phone_config("US"),
        on_commit=_noop,
    )
    first = h._active_slot
    h.enter_slot_capture(
        kind="phone", config=_phone_config("PK"),
        on_commit=_noop,
    )
    assert h._active_slot is not first
    assert h.slot_capture_active is True
    h.exit_slot_capture(reason="test-teardown")


@pytest.mark.asyncio
async def test_no_feed_returns_false_when_no_active_slot():
    """When no slot is open, brain runs as usual.  This is the ONLY
    path that returns False."""
    h = _bind_slot_methods()
    consumed = await h._feed_active_slot("hello", turn_gen=1)
    assert consumed is False
