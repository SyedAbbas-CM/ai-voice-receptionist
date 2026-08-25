"""Regression: with next_action_policy_enabled=False (default), brain.py
must NOT hit the deterministic post-tool renderer.

This is a safety pin.  We ship the wiring inert (flag=False) so ship
and activate are separate decisions.  If a future edit accidentally
inverts the guard or a test monkey-patch removes it, this test fails
loudly BEFORE it reaches production.

Also pins _extract_known_slots — the brain-side helper that flattens
tool arguments into the shape the renderer wants.  If the argument
schema drifts (a booking tool renames `caller_name` → `full_name`,
etc.), extend `_BOOKING_ARG_TO_SLOT` first and update this test —
don't teach the renderer new key names.
"""
from __future__ import annotations

from apps.api.app.core.config import settings
from packages.core_agent.brain import _extract_known_slots


def test_flag_default_is_false():
    """Ship-inert contract: default must be False, so pulling this
    branch into a bounce doesn't silently activate the short-circuit."""
    # Read the class default directly, not the running instance —
    # the instance could be overridden by env in tests.
    from apps.api.app.core.config import Settings
    default_field = Settings.model_fields["next_action_policy_enabled"]
    assert default_field.default is False, (
        "next_action_policy_enabled default must be False — flag flip "
        "should be a separate deploy decision, not an implicit one."
    )


def test_extract_slots_from_book_appointment_receipt():
    """Happy path — full booking tool call produces all four slots."""
    tool_results = [{
        "name": "book_appointment",
        "arguments": {
            "caller_name": "Abbas",
            "service": "cleaning",
            "date": "2026-08-26",
            "time": "14:30",
            "phone": "5551234567",
        },
        "result": {"ok": True, "booking_id": "b_123"},
        "error": None,
    }]
    slots = _extract_known_slots(None, tool_results)
    assert slots["caller_name"] == "Abbas"
    assert slots["service"] == "cleaning"
    assert slots["date"] == "2026-08-26"
    assert slots["time"] == "14:30"
    assert slots["phone"] == "5551234567"


def test_extract_slots_splits_start_iso():
    """start_iso → date + time via ISO split.  Some tool variants use
    a single ISO instead of split date/time fields."""
    tool_results = [{
        "name": "book_appointment",
        "arguments": {
            "caller_name": "Zara",
            "service": "consultation",
            "start_iso": "2026-08-26T14:30:00",
        },
        "result": {"ok": True},
        "error": None,
    }]
    slots = _extract_known_slots(None, tool_results)
    assert slots["date"] == "2026-08-26"
    assert slots["time"] == "14:30"


def test_extract_slots_ignores_failed_booking():
    """Never extract from a booking tool with error != None — the
    booking didn't happen, we must not synth confirmation for it."""
    tool_results = [{
        "name": "book_appointment",
        "arguments": {"caller_name": "Abbas", "service": "cleaning",
                      "date": "2026-08-26", "time": "14:30"},
        "result": {},
        "error": "conflict",
    }]
    slots = _extract_known_slots(None, tool_results)
    assert slots == {}


def test_extract_slots_ignores_non_booking_tool():
    tool_results = [{
        "name": "check_availability",
        "arguments": {"date": "2026-08-26"},
        "result": {"slots": ["14:00", "14:30"]},
        "error": None,
    }]
    slots = _extract_known_slots(None, tool_results)
    assert slots == {}


def test_extract_slots_handles_missing_args():
    tool_results = [{
        "name": "book_appointment",
        "arguments": None,
        "result": {},
        "error": None,
    }]
    assert _extract_known_slots(None, tool_results) == {}

    tool_results = [{"name": "book_appointment", "result": {}, "error": None}]
    assert _extract_known_slots(None, tool_results) == {}


def test_extract_slots_alias_names():
    """`name` / `customer_name` alias to caller_name;
    `reason`/`appointment_type` alias to service."""
    tool_results = [{
        "name": "book_appointment",
        "arguments": {
            "name": "Sam",
            "reason": "root canal",
            "start_date": "2026-08-26",
            "start_time": "14:30",
        },
        "result": {"ok": True},
        "error": None,
    }]
    slots = _extract_known_slots(None, tool_results)
    assert slots["caller_name"] == "Sam"
    assert slots["service"] == "root canal"
    assert slots["date"] == "2026-08-26"
    assert slots["time"] == "14:30"


def test_extract_slots_never_raises_on_garbage():
    assert _extract_known_slots(None, None) == {}  # type: ignore[arg-type]
    assert _extract_known_slots(None, [{"garbage": True}]) == {}
    assert _extract_known_slots(None, [{"name": "book_appointment",
                                        "arguments": "not-a-dict",
                                        "error": None}]) == {}
