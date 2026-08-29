"""Task #97 second half: NextActionPolicy ASK_SLOT → actor.enter_slot_capture.

2026-08-29: The final wire that closes the LK-style slot capture loop
end-to-end.  Before this, all the pieces were in place:
  * NextActionPolicy could decide ASK_SLOT(slot='phone')
  * actor.enter_slot_capture(kind='phone') opened a real capture
  * The brain injected the LK sub-agent prompt when a capture was
    active on state

But nothing connected the policy decision to enter_slot_capture, so
the Christiaan scenario ("agent should ask for my number and capture
it structurally") still fell back to the wide-scope brain and
Christiaan's Dutch phone got misheard.

Wire flow:
  1. Actor stages state before dispatch: sets state._on_policy_decision
     to actor._on_policy_decision_callback.
  2. Brain runs NextActionPolicy, awaits the callback with the
     decision.
  3. On ASK_SLOT(slot='phone'), actor fires enter_slot_capture(
     kind='phone', ...) with tenant phone-region config.
  4. Next caller turn feeds the structured slot session; brain sees
     the narrow sub-agent prompt via state._slot_capture_prompt.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeBusiness:
    """Minimal business profile stub for phone-region config."""
    phone_default_region = "NL"
    phone_accepted_regions = ["NL", "DE", "US", "GB"]


class _FakeSlotResult:
    def __init__(self, value: str):
        self.value = value


def _fake_actor():
    """Build a minimal actor that has just the surface enter_slot_capture
    + _on_policy_decision_callback need to work."""
    from apps.api.app.routes.twilio_actor import TwilioActorSession
    a = TwilioActorSession.__new__(TwilioActorSession)
    a.call_id = "CAtest"
    a.tenant_id = "test-tenant"
    a._active_slot = None
    a._slot_normalizer = None
    a._slot_on_commit = None
    a._slot_on_stall = None
    a._slot_on_confirm_needed = None
    a._slot_stall_task = None
    a._slot_stall_first_prompt_s = 6.0
    a._slot_stall_escalate_s = 8.0
    a._active_slot_prompt = None
    a.business = _FakeBusiness()
    # Watchdog stubs so we don't need an event loop.
    a._arm_slot_stall_watchdog = lambda: None
    a._cancel_slot_stall_watchdog = lambda: None
    return a


# ── stage_state_for_brain_dispatch attaches the callback ────


class _State:
    """Plain object stand-in — MagicMock auto-creates attributes on
    access which defeats the callback-identity check."""
    pass


def test_stage_state_attaches_policy_decision_callback():
    """Actor stages `_on_policy_decision` on state so brain can call
    back into the actor after policy runs."""
    a = _fake_actor()
    state = _State()
    a._stage_state_for_brain_dispatch(state)
    # Bound methods on the same instance compare equal (not is-equal
    # since each access creates a new bound-method wrapper).
    assert state._on_policy_decision == (
        a._on_policy_decision_callback
    )
    # Confirm it's really the actor's bound method, not something else.
    assert state._on_policy_decision.__self__ is a
    assert state._on_policy_decision.__func__ is (
        type(a)._on_policy_decision_callback
    )


def test_stage_state_attaches_slot_prompt():
    """The other half of the stage — active_slot_prompt copied to state."""
    a = _fake_actor()
    state = _State()
    # Empty case first.
    a._stage_state_for_brain_dispatch(state)
    assert state._slot_capture_prompt is None
    # Now open a capture and re-stage.
    a.enter_slot_capture(
        kind="phone",
        config={"default_region": "NL"},
        on_commit=a._default_slot_on_commit,
    )
    a._stage_state_for_brain_dispatch(state)
    assert state._slot_capture_prompt is not None
    assert state._slot_capture_prompt is a.active_slot_prompt


# ── callback fires enter_slot_capture on ASK_SLOT(phone) ─────


@pytest.mark.asyncio
async def test_callback_opens_capture_on_ask_slot_phone():
    """The Christiaan trigger: policy decides ASK_SLOT(phone) →
    actor opens the phone capture with tenant region config."""
    from packages.dialogue.next_action_policy import (
        ConversationAction, ConversationNextAction, DeliveryIntent,
    )
    a = _fake_actor()
    assert a._active_slot is None
    decision = ConversationNextAction(
        action=ConversationAction.ASK_SLOT,
        requested_slot="phone",
        delivery_intent=DeliveryIntent.STANDARD,
        max_tokens=40,
    )
    await a._on_policy_decision_callback(decision)
    # Capture is now active.
    assert a._active_slot is not None
    assert a._active_slot.slot_type == "phone"
    # And uses the tenant's phone region config, not hard-coded US.
    assert a._active_slot.config["default_region"] == "NL"
    assert "NL" in a._active_slot.config["accepted_regions"]
    # And the LK sub-agent prompt attached.
    assert a.active_slot_prompt is not None
    assert "update_phone_number" in (
        a.active_slot_prompt.instructions
    )


@pytest.mark.asyncio
async def test_callback_noop_on_ask_slot_other_slot():
    """ASK_SLOT for a slot that isn't phone should NOT open a phone
    capture.  Only phone is wired end-to-end today; name/email/date
    follow via task #98."""
    from packages.dialogue.next_action_policy import (
        ConversationAction, ConversationNextAction, DeliveryIntent,
    )
    a = _fake_actor()
    decision = ConversationNextAction(
        action=ConversationAction.ASK_SLOT,
        requested_slot="email",
        delivery_intent=DeliveryIntent.STANDARD,
        max_tokens=40,
    )
    await a._on_policy_decision_callback(decision)
    assert a._active_slot is None


@pytest.mark.asyncio
async def test_callback_noop_on_non_ask_slot_action():
    """Other actions (ANSWER, CONFIRM_ACTION, END_CALL, etc) don't
    open captures."""
    from packages.dialogue.next_action_policy import (
        ConversationAction, ConversationNextAction, DeliveryIntent,
    )
    a = _fake_actor()
    for action in (
        ConversationAction.ANSWER,
        ConversationAction.CONFIRM_ACTION,
        ConversationAction.END_CALL,
        ConversationAction.ACKNOWLEDGE,
    ):
        decision = ConversationNextAction(
            action=action,
            delivery_intent=DeliveryIntent.STANDARD,
            max_tokens=40,
        )
        await a._on_policy_decision_callback(decision)
        assert a._active_slot is None, (
            f"action={action.value} unexpectedly opened capture"
        )


@pytest.mark.asyncio
async def test_callback_idempotent_when_already_capturing_phone():
    """If a phone capture is already active, another ASK_SLOT(phone)
    is a no-op — do NOT close + reopen (would drop the accumulated
    digit buffer)."""
    from packages.dialogue.next_action_policy import (
        ConversationAction, ConversationNextAction, DeliveryIntent,
    )
    a = _fake_actor()
    # Manually open a phone capture.
    a.enter_slot_capture(
        kind="phone",
        config={"default_region": "NL"},
        on_commit=a._default_slot_on_commit,
    )
    prior_session = a._active_slot
    # Fire the callback with ASK_SLOT(phone).
    decision = ConversationNextAction(
        action=ConversationAction.ASK_SLOT,
        requested_slot="phone",
        delivery_intent=DeliveryIntent.STANDARD,
        max_tokens=40,
    )
    await a._on_policy_decision_callback(decision)
    # Same session — not replaced.
    assert a._active_slot is prior_session


@pytest.mark.asyncio
async def test_callback_uses_us_defaults_when_business_missing():
    """No business attached → US default_region.  Safe fallback."""
    from packages.dialogue.next_action_policy import (
        ConversationAction, ConversationNextAction, DeliveryIntent,
    )
    a = _fake_actor()
    a.business = None
    decision = ConversationNextAction(
        action=ConversationAction.ASK_SLOT,
        requested_slot="phone",
        delivery_intent=DeliveryIntent.STANDARD,
        max_tokens=40,
    )
    await a._on_policy_decision_callback(decision)
    assert a._active_slot is not None
    assert a._active_slot.config["default_region"] == "US"


@pytest.mark.asyncio
async def test_callback_never_raises():
    """Malformed decision → warning log, no exception."""
    a = _fake_actor()
    # Missing attributes we'd normally read.
    class _MalformedDecision:
        pass
    await a._on_policy_decision_callback(_MalformedDecision())
    # Slot NOT opened, no crash.
    assert a._active_slot is None


# ── default on_commit ─────────────────────────────────────


@pytest.mark.asyncio
async def test_default_on_commit_stashes_validated_phone():
    """When capture commits, the E.164 value lands on the actor for
    downstream booking-flow to pick up."""
    a = _fake_actor()
    result = _FakeSlotResult(value="+31625007600")
    await a._default_slot_on_commit(result)
    assert a._last_validated_phone == "+31625007600"
