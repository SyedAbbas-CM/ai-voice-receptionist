"""Tests for the deterministic post-tool reply synthesizer.

Pins:
  1. Successful booking → renders natural confirmation, skip flag True
  2. No booking tool → falls through to LLM (skip flag False)
  3. Booking with error → falls through to LLM (never confirm a failed booking)
  4. Missing critical facts → falls through (synth returns None)
  5. Date/time formatting produces natural speech
  6. Never raises on malformed input
"""
from __future__ import annotations

from packages.core_agent.next_action_synthesizer import (
    _facts_dict,
    _format_date_for_speech,
    _format_time_for_speech,
    _minute_to_words,
    _render_confirm_action,
    maybe_synthesize,
)


# ── date / time formatting ─────────────────────────────────────────


def test_format_date_iso_to_natural():
    assert _format_date_for_speech("2026-08-26") == "Wednesday, August 26th"


def test_format_date_st_nd_rd_th_suffixes():
    assert _format_date_for_speech("2026-08-01").endswith("1st")
    assert _format_date_for_speech("2026-08-02").endswith("2nd")
    assert _format_date_for_speech("2026-08-03").endswith("3rd")
    assert _format_date_for_speech("2026-08-04").endswith("4th")
    assert _format_date_for_speech("2026-08-11").endswith("11th")  # teens are th
    assert _format_date_for_speech("2026-08-21").endswith("21st")
    assert _format_date_for_speech("2026-08-22").endswith("22nd")
    assert _format_date_for_speech("2026-08-23").endswith("23rd")


def test_format_date_invalid_passes_through():
    assert _format_date_for_speech("tomorrow") == "tomorrow"
    assert _format_date_for_speech("garbage") == "garbage"


def test_format_time_on_the_hour():
    assert _format_time_for_speech("09:00") == "nine a.m."
    assert _format_time_for_speech("14:00") == "two p.m."
    assert _format_time_for_speech("00:00") == "twelve a.m."
    assert _format_time_for_speech("12:00") == "twelve p.m."


def test_format_time_with_minutes():
    assert _format_time_for_speech("09:30") == "nine thirty a.m."
    assert _format_time_for_speech("14:30") == "two thirty p.m."
    assert _format_time_for_speech("09:05") == "nine oh five a.m."
    assert _format_time_for_speech("14:15") == "two fifteen p.m."
    assert _format_time_for_speech("09:45") == "nine forty-five a.m."


def test_format_time_with_seconds():
    assert _format_time_for_speech("14:30:00") == "two thirty p.m."


def test_minute_edge_cases():
    # :00 is handled upstream (hour-only speech), so _minute_to_words(0) is
    # unreachable via the public path. Not asserting its return value —
    # implementation detail.
    assert _minute_to_words(5) == "oh five"
    assert _minute_to_words(10) == "ten"
    assert _minute_to_words(15) == "fifteen"
    assert _minute_to_words(20) == "twenty"
    assert _minute_to_words(30) == "thirty"
    assert _minute_to_words(45) == "forty-five"
    assert _minute_to_words(59) == "fifty-nine"


# ── facts parsing ──────────────────────────────────────────────────


def test_facts_dict_parses_key_value():
    result = _facts_dict(["service: cleaning", "date: 2026-08-26", "time: 14:30"])
    assert result == {"service": "cleaning", "date": "2026-08-26", "time": "14:30"}


def test_facts_dict_ignores_malformed():
    result = _facts_dict(["no_colon", "key: value", "", 123])  # type: ignore[list-item]
    assert result == {"key": "value"}


# ── confirm_action rendering ──────────────────────────────────────


def test_render_confirm_action_with_all_facts():
    r = _render_confirm_action({
        "caller_name": "Abbas",
        "service": "cleaning",
        "date": "2026-08-26",
        "time": "14:30",
    })
    assert r is not None
    assert "Abbas" in r
    assert "cleaning" in r
    assert "Wednesday, August 26th" in r
    assert "two thirty p.m." in r
    assert r.endswith("See you then!")


def test_render_confirm_action_without_name():
    r = _render_confirm_action({
        "service": "cleaning",
        "date": "2026-08-26",
        "time": "14:30",
    })
    assert r is not None
    assert r.startswith("you're booked")  # no name prefix


def test_render_confirm_action_missing_critical_returns_none():
    # Missing service
    assert _render_confirm_action({"date": "2026-08-26", "time": "14:30"}) is None
    # Missing date
    assert _render_confirm_action({"service": "cleaning", "time": "14:30"}) is None
    # Missing time
    assert _render_confirm_action({"service": "cleaning", "date": "2026-08-26"}) is None


# ── maybe_synthesize integration ──────────────────────────────────


def test_maybe_synthesize_booking_success_returns_reply():
    tool_results = [{"name": "book_appointment", "ok": True, "result": {}}]
    known = {
        "caller_name": "Abbas",
        "service": "cleaning",
        "date": "2026-08-26",
        "time": "14:30",
    }
    reply, skip = maybe_synthesize(tool_results, known)
    assert skip is True
    assert reply is not None
    assert "Abbas" in reply
    assert "cleaning" in reply


def test_maybe_synthesize_no_booking_tool_falls_through():
    tool_results = [{"name": "check_availability", "ok": True, "result": {}}]
    reply, skip = maybe_synthesize(tool_results, {"service": "cleaning"})
    assert skip is False
    assert reply is None


def test_maybe_synthesize_booking_failed_falls_through():
    """Never confirm a failed booking — safety-critical."""
    tool_results = [{"name": "book_appointment", "ok": False, "result": {"error": "conflict"}}]
    reply, skip = maybe_synthesize(tool_results, {
        "caller_name": "Abbas",
        "service": "cleaning",
        "date": "2026-08-26",
        "time": "14:30",
    })
    assert skip is False
    assert reply is None


def test_maybe_synthesize_missing_facts_falls_through():
    tool_results = [{"name": "book_appointment", "ok": True, "result": {}}]
    reply, skip = maybe_synthesize(tool_results, {"caller_name": "Abbas"})  # no service/date/time
    assert skip is False
    assert reply is None


def test_maybe_synthesize_never_raises_on_garbage():
    """Safe fallback on any exception."""
    reply, skip = maybe_synthesize(None, None)  # type: ignore[arg-type]
    assert skip is False
    assert reply is None

    reply, skip = maybe_synthesize([{"garbage": True}], {})
    assert skip is False
    assert reply is None


def test_maybe_synthesize_empty_tool_results_falls_through():
    reply, skip = maybe_synthesize([], {"service": "cleaning"})
    assert skip is False
    assert reply is None


# ── render_from_semantic_plan — planner-native entry point ─────────


def _make_confirm_plan(**facts) -> "SemanticPlan":  # noqa: F821
    from packages.dialogue.plan import (
        PlannedFact, PlanOperation, SemanticPlan,
    )
    return SemanticPlan(
        active_task_id="booking-abc123",
        operation=PlanOperation.CONFIRM_ACTION,
        facts=[
            PlannedFact(claim=f"{k}: {v}", source=f"tool:{k}", critical=True)
            for k, v in facts.items()
        ],
    )


def test_render_from_semantic_plan_confirm_action_renders():
    from packages.core_agent.next_action_synthesizer import (
        render_from_semantic_plan,
    )
    plan = _make_confirm_plan(
        caller_name="Abbas",
        service="cleaning",
        date="2026-08-26",
        time="14:30",
    )
    reply = render_from_semantic_plan(plan)
    assert reply is not None
    assert "Abbas" in reply
    assert "cleaning" in reply
    assert "Wednesday, August 26th" in reply
    assert reply.endswith("See you then!")


def test_render_from_semantic_plan_non_confirm_returns_none():
    """Plan with non-CONFIRM operation must NOT synthesize — falls
    through to LLM realizer."""
    from packages.core_agent.next_action_synthesizer import (
        render_from_semantic_plan,
    )
    from packages.dialogue.plan import (
        PlannedFact, PlannedQuestion, PlanOperation, SemanticPlan,
    )
    plan = SemanticPlan(
        operation=PlanOperation.ASK_SLOT,
        question=PlannedQuestion(purpose="ask_phone", text_goal="phone?"),
    )
    assert render_from_semantic_plan(plan) is None


def test_render_from_semantic_plan_missing_critical_returns_none():
    from packages.core_agent.next_action_synthesizer import (
        render_from_semantic_plan,
    )
    plan = _make_confirm_plan(caller_name="Abbas")  # no service/date/time
    assert render_from_semantic_plan(plan) is None


def test_render_from_semantic_plan_never_raises_on_garbage():
    from packages.core_agent.next_action_synthesizer import (
        render_from_semantic_plan,
    )
    assert render_from_semantic_plan(None) is None  # type: ignore[arg-type]
    assert render_from_semantic_plan("not-a-plan") is None  # type: ignore[arg-type]
    assert render_from_semantic_plan(42) is None  # type: ignore[arg-type]


# ── SlotProposalRenderer (check_availability path) ──────────────────


def _availability_result(open_slots, **overrides):
    """Build a check_availability tool receipt for the tests."""
    return [{
        "name": "check_availability",
        "arguments": {"date": "2026-08-26", "service": "cleaning"},
        "result": {
            "date": overrides.get("date", "2026-08-26"),
            "service": "cleaning",
            "open_slots": open_slots,
            **{k: v for k, v in overrides.items() if k not in ("date",)},
        },
        "error": None,
    }]


def test_availability_three_slots_renders_natural_list():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability(
        _availability_result(["10:00", "14:30", "15:00"]),
    )
    assert skip is True
    assert reply is not None
    # All three slots present as spoken form.
    assert "ten" in reply
    assert "two thirty" in reply
    assert "three" in reply
    # Day rendered as weekday for orientation.
    assert "Wednesday" in reply
    assert reply.endswith("Which works?")


def test_availability_one_slot():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability(
        _availability_result(["10:00"]),
    )
    assert skip is True
    assert reply is not None
    assert "ten" in reply


def test_availability_two_slots_uses_or_not_comma():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability(
        _availability_result(["10:00", "14:00"]),
    )
    assert skip is True
    assert reply is not None
    assert " or " in reply
    # Both times present, joined with " or " (not comma).
    assert "ten" in reply and "two" in reply
    # No comma in the choice list itself (may appear elsewhere in text
    # like "Wednesday, which works?" if we ever change phrasing, but
    # the "A or B" fragment must not contain one).
    or_fragment = reply.split(" on ")[0]  # everything before " on Wednesday"
    assert or_fragment.count(",") == 0


def test_availability_two_slots_same_meridiem_strips_suffix():
    """Both slots a.m. → 'ten or eleven a.m.' not 'ten a.m. or eleven a.m.'
    Humanness win — real receptionists don't say meridiem twice when it's
    the same."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability(
        _availability_result(["10:00", "11:00"]),
    )
    assert skip is True
    assert reply is not None
    # Only ONE "a.m." should appear (on the final item).
    assert reply.count("a.m.") == 1
    # Neither p.m. nor spurious meridiem.
    assert "p.m." not in reply


def test_availability_many_slots_picks_spread():
    """8 slots → we shouldn't dump all 8 at a caller. Pick 3 spread."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    slots = ["09:00", "09:30", "10:00", "10:30",
             "11:00", "13:00", "14:00", "15:00"]
    reply, skip = maybe_synthesize_availability(_availability_result(slots))
    assert skip is True
    assert reply is not None
    # Should include first + middle + last.
    assert "nine" in reply    # 09:00
    assert "three" in reply    # 15:00
    # Exactly two commas + one "or" in the list join (3 items in list).
    # Rough sanity — don't over-constrain phrasing, just check count.
    assert reply.count(", ") <= 2


def test_availability_empty_slots_falls_through():
    """Empty open_slots → LLM should say 'we're full that day' using
    prompt rules + FAQ.  Synth must not proposition an empty list."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability(_availability_result([]))
    assert skip is False
    assert reply is None


def test_availability_date_unparseable_falls_through():
    """Tool signalled bad date → LLM must clarify with caller."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    tr = [{
        "name": "check_availability",
        "arguments": {"date": "next flurbday"},
        "result": {"date_unparseable": True, "reason": "no idea"},
        "error": None,
    }]
    reply, skip = maybe_synthesize_availability(tr)
    assert skip is False
    assert reply is None


def test_availability_date_ambiguous_falls_through():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    tr = [{
        "name": "check_availability",
        "arguments": {"date": "next Friday"},
        "result": {
            "date_ambiguous": True,
            "candidates": ["2026-08-29", "2026-09-05"],
        },
        "error": None,
    }]
    reply, skip = maybe_synthesize_availability(tr)
    assert skip is False
    assert reply is None


def test_availability_tool_error_falls_through():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    tr = [{
        "name": "check_availability",
        "arguments": {"date": "2026-08-26"},
        "result": {},
        "error": "connection reset",
    }]
    reply, skip = maybe_synthesize_availability(tr)
    assert skip is False
    assert reply is None


def test_availability_no_tool_call_falls_through():
    """Turn had no check_availability at all → synth must not fire."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, skip = maybe_synthesize_availability([
        {"name": "lookup_faq", "result": {"answer": "hi"}, "error": None},
    ])
    assert skip is False
    assert reply is None


def test_availability_never_raises_on_garbage():
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    assert maybe_synthesize_availability(None)[1] is False  # type: ignore[arg-type]
    assert maybe_synthesize_availability([{"garbage": True}])[1] is False
    assert maybe_synthesize_availability([{
        "name": "check_availability",
        "result": {"open_slots": "not-a-list"},
        "error": None,
    }])[1] is False


def test_availability_speaks_only_slots_returned():
    """CORE INVARIANT: the reply must never contain a time that isn't
    in the input `open_slots` list.  This is the hallucination fix's
    entire reason for being."""
    from packages.core_agent.next_action_synthesizer import (
        maybe_synthesize_availability,
    )
    reply, _ = maybe_synthesize_availability(
        _availability_result(["11:00", "13:30"]),
    )
    assert reply is not None
    # Neither of the phrases below should ever appear because
    # 10:00 and 14:00 are NOT in the input.
    assert "ten " not in reply.lower()   # no "ten a.m."
    assert "two " not in reply.lower()   # no "two p.m."
    # Positive check — the actual slots are present.
    assert "eleven" in reply
    assert "one thirty" in reply
