"""Tests for TransferCoordinator.

2026-08-27 (task #139): pure state-machine + policy tests.  Transport
dial is injected as a stub — no real Twilio.  When networking's P0.5
outbound guard lands + we wire the real conference primitives, these
tests continue to work by swapping the stub for the production
transport.
"""
from __future__ import annotations

import pytest

from packages.integrations.transfer import (
    TransferAttempt,
    TransferCoordinator,
    TransferDestination,
    TransferMode,
    TransferOutcome,
    TransferRule,
)


# ── fixtures ────────────────────────────────────────────────────


def _dest(id="agent_maria", label="Maria Chen", phone="+15551234567",
          department=None, is_default=False):
    return TransferDestination(
        id=id, label=label, phone=phone,
        department=department, is_default=is_default,
    )


def _attempt(mode=TransferMode.WARM, destination=None,
              reason="caller_requested"):
    return TransferAttempt(
        id="tx-1", call_sid="CA123", tenant_id="tenant-x",
        session_id="sess-1", caller_name="Sarah",
        caller_phone="+15559999999", reason=reason,
        mode=mode, destination=destination,
    )


def _stub_transport(outcome=TransferOutcome.BRIDGED):
    async def _dial(_attempt):
        return outcome
    return _dial


def _stub_message_fallback(msg_id="msg-1"):
    async def _take(_attempt):
        return msg_id
    return _take


# ── TransferOutcome property helpers ──────────────────────────


def test_bridged_is_success():
    assert TransferOutcome.BRIDGED.is_success is True


def test_message_taken_is_success():
    assert TransferOutcome.MESSAGE_TAKEN.is_success is True


def test_no_answer_is_not_success():
    assert TransferOutcome.NO_ANSWER.is_success is False


def test_recoverable_outcomes():
    assert TransferOutcome.NO_ANSWER.is_recoverable is True
    assert TransferOutcome.BUSY.is_recoverable is True
    assert TransferOutcome.DECLINED.is_recoverable is True
    assert TransferOutcome.TIMEOUT.is_recoverable is True


def test_non_recoverable_outcomes():
    assert TransferOutcome.POLICY_BLOCKED.is_recoverable is False
    assert TransferOutcome.INVALID_DESTINATION.is_recoverable is False
    assert TransferOutcome.FAILED.is_recoverable is False


# ── find_rule ─────────────────────────────────────────────────


def test_find_rule_specific_over_catchall():
    """A rule targeting 'complaint' beats a catch-all (empty triggers)."""
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[
            TransferRule(),  # catch-all
            TransferRule(
                trigger_reasons=frozenset({"complaint"}),
                urgency="high",
            ),
        ],
        transport_dial=_stub_transport(),
    )
    rule = coord.find_rule("complaint")
    assert rule is not None
    assert rule.urgency == "high"


def test_find_rule_catchall_matches_when_no_specific():
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[
            TransferRule(trigger_reasons=frozenset({"complaint"})),
            TransferRule(),  # catch-all
        ],
        transport_dial=_stub_transport(),
    )
    rule = coord.find_rule("some_other_reason")
    assert rule is not None
    assert rule.trigger_reasons == frozenset()


def test_find_rule_returns_none_when_no_catchall():
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[
            TransferRule(trigger_reasons=frozenset({"complaint"})),
        ],
        transport_dial=_stub_transport(),
    )
    rule = coord.find_rule("nothing_matches")
    assert rule is None


# ── resolve_destination ──────────────────────────────────────


def test_resolve_by_agent_name_label():
    coord = TransferCoordinator(
        destinations=[
            _dest(id="agent_maria", label="Maria Chen"),
            _dest(id="agent_joao", label="João Silva"),
        ],
        rules=[TransferRule()],
        transport_dial=_stub_transport(),
    )
    dest = coord.resolve_destination(
        TransferRule(), agent_name_requested="Maria",
    )
    assert dest is not None
    assert dest.id == "agent_maria"


def test_resolve_by_agent_name_id():
    coord = TransferCoordinator(
        destinations=[_dest(id="agent_maria", label="Maria Chen")],
        rules=[TransferRule()],
        transport_dial=_stub_transport(),
    )
    dest = coord.resolve_destination(
        TransferRule(), agent_name_requested="agent_maria",
    )
    assert dest is not None
    assert dest.id == "agent_maria"


def test_resolve_by_rule_destination_id():
    coord = TransferCoordinator(
        destinations=[
            _dest(id="agent_maria", label="Maria"),
            _dest(id="agent_joao", label="João"),
        ],
        rules=[TransferRule()],
        transport_dial=_stub_transport(),
    )
    rule = TransferRule(destination_id="agent_joao")
    dest = coord.resolve_destination(rule)
    assert dest is not None
    assert dest.id == "agent_joao"


def test_resolve_falls_back_to_default():
    coord = TransferCoordinator(
        destinations=[
            _dest(id="default_line", label="On-call line", is_default=True),
        ],
        rules=[TransferRule()],
        transport_dial=_stub_transport(),
    )
    dest = coord.resolve_destination(TransferRule())
    assert dest is not None
    assert dest.id == "default_line"


def test_resolve_returns_none_when_empty():
    coord = TransferCoordinator(
        destinations=[],
        rules=[TransferRule()],
        transport_dial=_stub_transport(),
    )
    dest = coord.resolve_destination(TransferRule())
    assert dest is None


# ── initiate_transfer state machine ──────────────────────────


@pytest.mark.asyncio
async def test_warm_bridge_success():
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.BRIDGED),
    )
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(
        attempt=a, rule=TransferRule(),
    )
    assert result.outcome == TransferOutcome.BRIDGED


@pytest.mark.asyncio
async def test_no_answer_with_message_fallback_becomes_message_taken():
    """The critical humanness moment — human didn't pick up, we don't lie,
    we take a message instead."""
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.NO_ANSWER),
        take_message_fallback=_stub_message_fallback("msg-9"),
    )
    rule = TransferRule(message_if_failed=True)
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(attempt=a, rule=rule)
    assert result.outcome == TransferOutcome.MESSAGE_TAKEN
    assert result.fallback_message_id == "msg-9"


@pytest.mark.asyncio
async def test_no_answer_without_fallback_stays_failed():
    """If tenant disabled the message fallback, we surface the failure
    honestly so the LLM verbalizer says 'they didn't pick up' not
    'connected'."""
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.NO_ANSWER),
        # No message fallback registered.
    )
    rule = TransferRule(message_if_failed=False)
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(attempt=a, rule=rule)
    assert result.outcome == TransferOutcome.NO_ANSWER


@pytest.mark.asyncio
async def test_message_fallback_disabled_by_rule():
    """Rule says no fallback → failure stays failure even with take_message
    registered."""
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.NO_ANSWER),
        take_message_fallback=_stub_message_fallback(),
    )
    rule = TransferRule(message_if_failed=False)
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(attempt=a, rule=rule)
    assert result.outcome == TransferOutcome.NO_ANSWER


@pytest.mark.asyncio
async def test_policy_blocked_no_fallback():
    """POLICY_BLOCKED is non-recoverable — even with fallback registered,
    we don't try to take a message.  Might be quiet-hours / DNC / kill."""
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.POLICY_BLOCKED),
        take_message_fallback=_stub_message_fallback(),
    )
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(
        attempt=a, rule=TransferRule(message_if_failed=True),
    )
    assert result.outcome == TransferOutcome.POLICY_BLOCKED


@pytest.mark.asyncio
async def test_invalid_destination_no_bridge():
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.BRIDGED),
    )
    a = _attempt(destination=None)  # invalid
    result = await coord.initiate_transfer(
        attempt=a, rule=TransferRule(),
    )
    assert result.outcome == TransferOutcome.INVALID_DESTINATION


@pytest.mark.asyncio
async def test_callback_mode_schedules_without_dial():
    """CALLBACK mode doesn't try to bridge — schedules an outbound
    without going through the transport."""
    dial_called = False
    async def _watching_dial(_a):
        nonlocal dial_called
        dial_called = True
        return TransferOutcome.BRIDGED
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_watching_dial,
    )
    a = _attempt(mode=TransferMode.CALLBACK, destination=_dest())
    result = await coord.initiate_transfer(attempt=a, rule=TransferRule())
    assert result.outcome == TransferOutcome.CALLBACK_SCHEDULED
    assert dial_called is False


@pytest.mark.asyncio
async def test_transport_exception_becomes_failed_outcome():
    """Any exception from the transport becomes FAILED — never propagates."""
    async def _raiser(_a):
        raise RuntimeError("network kaboom")
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_raiser,
    )
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(
        attempt=a, rule=TransferRule(message_if_failed=False),
    )
    assert result.outcome == TransferOutcome.FAILED
    assert "network kaboom" in (result.failure_detail or "")


@pytest.mark.asyncio
async def test_message_fallback_double_failure():
    """Transport fails AND message fallback fails — original outcome
    surfaces with a double-failure note."""
    async def _bad_take(_a):
        raise RuntimeError("db down")
    coord = TransferCoordinator(
        destinations=[_dest()],
        rules=[TransferRule()],
        transport_dial=_stub_transport(TransferOutcome.NO_ANSWER),
        take_message_fallback=_bad_take,
    )
    a = _attempt(destination=_dest())
    result = await coord.initiate_transfer(
        attempt=a, rule=TransferRule(message_if_failed=True),
    )
    assert result.outcome == TransferOutcome.NO_ANSWER
    assert "message fallback failed" in (result.failure_detail or "")


# ── claim-truth guards ──────────────────────────────────────


def test_can_say_connected_only_on_bridged():
    a_bridged = _attempt(destination=_dest())
    a_bridged.outcome = TransferOutcome.BRIDGED
    assert TransferCoordinator.can_llm_say_connected(a_bridged) is True

    a_no_answer = _attempt(destination=_dest())
    a_no_answer.outcome = TransferOutcome.NO_ANSWER
    assert TransferCoordinator.can_llm_say_connected(a_no_answer) is False

    a_message = _attempt(destination=_dest())
    a_message.outcome = TransferOutcome.MESSAGE_TAKEN
    # Message taken is NOT the same as connected.
    assert TransferCoordinator.can_llm_say_connected(a_message) is False


def test_can_say_message_taken_only_on_message_taken():
    a = _attempt(destination=_dest())
    a.outcome = TransferOutcome.MESSAGE_TAKEN
    assert TransferCoordinator.can_llm_say_message_taken(a) is True
    a.outcome = TransferOutcome.BRIDGED
    assert TransferCoordinator.can_llm_say_message_taken(a) is False


def test_can_say_callback_scheduled():
    a = _attempt(destination=_dest())
    a.outcome = TransferOutcome.CALLBACK_SCHEDULED
    assert TransferCoordinator.can_llm_say_callback_scheduled(a) is True
    a.outcome = TransferOutcome.BRIDGED
    assert TransferCoordinator.can_llm_say_callback_scheduled(a) is False


# ── render_honest_reply ────────────────────────────────────


def test_render_bridged_reply_names_destination():
    a = _attempt(destination=_dest(label="Dr. Chen"))
    a.outcome = TransferOutcome.BRIDGED
    reply = TransferCoordinator.render_honest_reply(a)
    assert "Dr. Chen" in reply
    assert "Connecting" in reply


def test_render_no_answer_offers_message():
    a = _attempt(destination=_dest(label="Maria"))
    a.outcome = TransferOutcome.NO_ANSWER
    reply = TransferCoordinator.render_honest_reply(a)
    assert "Maria" in reply
    assert "message" in reply.lower() or "call you back" in reply.lower()
    # Critical: must NOT say "connected"
    assert "connected" not in reply.lower()


def test_render_message_taken():
    a = _attempt(destination=_dest(label="the sales team"))
    a.outcome = TransferOutcome.MESSAGE_TAKEN
    reply = TransferCoordinator.render_honest_reply(a)
    assert "the sales team" in reply
    assert "message" in reply.lower()


def test_render_callback_scheduled():
    a = _attempt(destination=_dest(label="Maria"))
    a.outcome = TransferOutcome.CALLBACK_SCHEDULED
    reply = TransferCoordinator.render_honest_reply(a)
    assert "Maria" in reply
    assert "call you back" in reply.lower()


def test_render_policy_blocked_takes_message():
    a = _attempt(destination=_dest(label="Maria"))
    a.outcome = TransferOutcome.POLICY_BLOCKED
    reply = TransferCoordinator.render_honest_reply(a)
    assert "message" in reply.lower()
    assert "connected" not in reply.lower()


def test_render_failed_falls_back_gracefully():
    """A FAILED outcome should not admit 'transfer failed' — take a
    message honestly.  Never say 'connected'."""
    a = _attempt(destination=_dest(label="the office"))
    a.outcome = TransferOutcome.FAILED
    reply = TransferCoordinator.render_honest_reply(a)
    assert "connected" not in reply.lower()
    assert "message" in reply.lower()


def test_render_null_destination_still_returns_string():
    a = _attempt(destination=None)
    a.outcome = TransferOutcome.NO_ANSWER
    reply = TransferCoordinator.render_honest_reply(a)
    assert isinstance(reply, str)
    assert len(reply) > 0
    assert "our team" in reply.lower()
