"""Deterministic reply synthesis from NextActionPolicy decisions.

Wires the P7 policy scaffold into a runtime intercept: when the policy
returns an action with `must_include_facts` (currently only
CONFIRM_ACTION), we can synthesize the reply text directly from the
facts + a template — skipping the 2nd LLM roundtrip on booking-
confirmation turns.

Est. saving on booking-confirm turns: 600-1200ms (ChatGPT audit #1
priority — "wire NextActionPolicy + deterministic post-tool renderer";
see also `VOICE-AGENT-SUB-1.5S-RD-ROADMAP-2026-08-23.md` §A2).

Wiring point: `packages/core_agent/brain.py` `handle_user_turn`
immediately after tool_results are populated but BEFORE the 2nd LLM
call fires (the "no tool_calls" branch that would otherwise generate
the wording pass).

Feature-flagged via `settings.next_action_policy_enabled` (default
False) so we can ship code without activating globally. Flip to True
per-tenant or globally once verified on real calls.

## Two entry points

- `maybe_synthesize(tool_results, known_slots)` — ambient-runtime
  path.  Used when we don't yet have a full `SemanticPlan` for the
  turn (most current callsites in brain.py).  Builds a minimal
  `ConversationDecisionState`, asks `NextActionPolicy` for the action,
  renders if it's CONFIRM_ACTION.  This is what brain.py wires in.

- `render_from_semantic_plan(plan, tool_receipts)` — planner-native
  path.  Preferred when a `packages/dialogue/plan.py::SemanticPlan`
  is already available for the turn (planners/semantic_v2.py path in
  the roadmap).  Honors `plan.requires_deterministic_template()` and
  uses `plan.critical_facts()` for source-attributed input rather
  than the flat `must_include_facts: list[str]` shape.  Consumes the
  existing `PlannedFact` abstraction so sources / forbidden-claims /
  delivery_intent remain first-class.

Both paths converge on `_render_confirm_action(facts_dict)` so the
speech-formatting logic is one implementation, not two.

Behavior when active:
- Policy/plan says CONFIRM_ACTION with sufficient facts → synthesize
  deterministic reply text.  Return `(reply_text, True)` — brain
  skips the 2nd LLM.
- Anything else → return `(None, False)` — brain falls through to
  normal LLM path.
- Any exception → return `(None, False)` — safe fallback to LLM.

Not covered here (deferred to future iteration):
- Affect / style / urgency inference (reducer.py doesn't populate them
  yet — synthesizer uses safe defaults).
- Reducer wiring for accumulated ConversationDecisionState (state built
  fresh per call to `maybe_synthesize` from ambient runtime).
- Non-CONFIRM_ACTION deterministic synth (ASK_SLOT / TOOL_PREAMBLE etc.
  could also skip the LLM later — start with the biggest win, iterate).
- SlotProposalRenderer for `check_availability` — A2 lists this as a
  sibling renderer.  Deferred to the follow-up ticket after A1 lands.
"""
from __future__ import annotations

from typing import Optional

from packages.dialogue.next_action_policy import (
    ConversationAction,
    ConversationDecisionState,
    ConversationPhase,
    NextActionPolicy,
)
from packages.dialogue.plan import PlanOperation, SemanticPlan


def _facts_dict(must_include_facts: list[str]) -> dict[str, str]:
    """Parse the policy's `must_include_facts` list-of-'key: value'
    strings into a dict. Facts are emitted by the policy as
    `f"{k}: {v}"` where k ∈ {caller_name, service, date, time}.

    Returns empty dict on malformed input — synthesizer treats empty as
    "no facts, can't synthesize" and falls through to LLM.
    """
    out: dict[str, str] = {}
    for item in must_include_facts:
        if not isinstance(item, str) or ":" not in item:
            continue
        key, _, value = item.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def _render_confirm_action(facts: dict[str, str]) -> Optional[str]:
    """Render a natural booking-confirmation reply from parsed facts.

    Format target (from prompt.py § BOOKING CONFIRMATION RULE):
      "You're booked for a {service} on {date} at {time}. See you then!"

    If any critical fact is missing, returns None → LLM fallback.
    Never invents. Never uses field names in output text.
    """
    name = facts.get("caller_name") or ""
    service = facts.get("service") or ""
    date = facts.get("date") or ""
    time = facts.get("time") or ""

    # Minimum viable: need service + date + time to say a confirmation.
    # Without those, the caller wouldn't be able to verify what got booked.
    if not (service and date and time):
        return None

    # Format the date for natural speech. Input is likely ISO
    # (YYYY-MM-DD) from the booking tool; render as caller-friendly.
    date_spoken = _format_date_for_speech(date)
    time_spoken = _format_time_for_speech(time)

    name_prefix = f"{name}, " if name else ""

    return (
        f"{name_prefix}you're booked for a {service} on {date_spoken} "
        f"at {time_spoken}. See you then!"
    )


def _format_date_for_speech(iso_or_human: str) -> str:
    """YYYY-MM-DD → 'Tuesday, August 26th'. Passes through if already
    human-readable. Never raises — worst case returns input unchanged."""
    try:
        from datetime import date as _date
        parts = iso_or_human.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            weekday = d.strftime("%A")
            month = d.strftime("%B")
            day = d.day
            # Simple ordinal — 1st, 2nd, 3rd, 4th...
            if 10 <= day % 100 <= 20:
                suffix = "th"
            else:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            return f"{weekday}, {month} {day}{suffix}"
    except Exception:
        pass
    return iso_or_human


def _format_time_for_speech(iso_or_human: str) -> str:
    """HH:MM (24-hour) → 'two thirty p.m.'.  Passes through on
    non-parseable input."""
    try:
        s = iso_or_human.strip()
        # Strip trailing seconds/timezone if any: "14:30:00" → "14:30".
        if s.count(":") >= 1:
            hh, mm = s.split(":")[:2]
            if hh.isdigit() and mm.isdigit():
                h = int(hh)
                m = int(mm)
                if 0 <= h <= 23 and 0 <= m <= 59:
                    ampm = "a.m." if h < 12 else "p.m."
                    hour12 = h if h <= 12 else h - 12
                    if hour12 == 0:
                        hour12 = 12
                    hour_words = _number_to_words(hour12)
                    if m == 0:
                        return f"{hour_words} {ampm}"
                    minute_words = _minute_to_words(m)
                    return f"{hour_words} {minute_words} {ampm}"
    except Exception:
        pass
    return iso_or_human


_HOUR_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}


def _number_to_words(n: int) -> str:
    return _HOUR_WORDS.get(n, str(n))


_MINUTE_TENS = {
    2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
}


def _minute_to_words(m: int) -> str:
    """5 → 'oh five' / 15 → 'fifteen' / 30 → 'thirty' / 45 → 'forty-five'"""
    if m < 10:
        return f"oh {_HOUR_WORDS.get(m, str(m))}"
    if 10 <= m <= 12:
        base = {10: "ten", 11: "eleven", 12: "twelve"}[m]
        return base
    if 13 <= m <= 19:
        base = {13: "thirteen", 14: "fourteen", 15: "fifteen",
                16: "sixteen", 17: "seventeen", 18: "eighteen",
                19: "nineteen"}[m]
        return base
    tens = m // 10
    ones = m % 10
    if ones == 0:
        return _MINUTE_TENS[tens]
    return f"{_MINUTE_TENS[tens]}-{_HOUR_WORDS[ones]}"


def maybe_synthesize(
    tool_results: list[dict],
    known_slots: Optional[dict[str, str]] = None,
) -> tuple[Optional[str], bool]:
    """Try to skip the 2nd LLM by synthesizing a reply from policy +
    facts.

    Called from brain.py after tool_results come back but BEFORE the
    2nd LLM call fires.

    Args:
      tool_results: list of tool receipts accumulated this turn.
                    Each has {'name', 'ok', 'result'} or similar.
      known_slots: parsed slot values (name / phone / service / date /
                   time) from the ambient runtime state. If None, treats
                   as empty — will fall through to LLM.

    Returns:
      (reply_text, True)  → synthesizer produced text; skip the LLM.
      (None, False)       → not applicable or synth failed; run LLM.

    Never raises. All exceptions become (None, False).
    """
    try:
        # Only synthesize when a booking tool completed successfully
        # this turn — that's the CONFIRM_ACTION trigger.
        booking_tools = {"book_appointment", "book_reservation", "book_viewing"}
        booked_ok = any(
            (tr.get("name") in booking_tools) and (tr.get("ok") is True)
            for tr in (tool_results or [])
        )
        if not booked_ok:
            return None, False

        # Build decision state so the policy fires the CONFIRM_ACTION
        # branch (its own contract, tested in test_next_action_policy_scaffold).
        known = known_slots or {}
        state = ConversationDecisionState(
            conversation_phase=ConversationPhase.CONFIRMING,
            requires_confirmation=True,
            known=known,
        )
        decision = NextActionPolicy().decide(state)

        if decision.action != ConversationAction.CONFIRM_ACTION:
            return None, False

        # Parse facts + render the confirmation.
        facts = _facts_dict(decision.must_include_facts)
        rendered = _render_confirm_action(facts)
        if not rendered:
            return None, False
        return rendered, True
    except Exception:
        return None, False


def render_from_semantic_plan(
    plan: SemanticPlan,
    tool_receipts: Optional[dict] = None,
) -> Optional[str]:
    """Render a deterministic reply from a `SemanticPlan`.

    This is the planner-native entry point.  When the wider stack
    (planners/semantic_v2.py per the roadmap) produces a `SemanticPlan`
    for the turn, brain.py can call this directly instead of the
    ambient-runtime `maybe_synthesize()` path — the plan already
    carries the operation + sourced facts + forbidden-claims + delivery
    intent, so we don't have to re-derive them from raw runtime state.

    Contract:
      - Only renders when `plan.requires_deterministic_template()` is
        True (currently: `PlanOperation.CONFIRM_ACTION`).  Anything
        else returns None → LLM fallback via the realizer.
      - Uses `plan.critical_facts()` for the fact set.  Each
        `PlannedFact.claim` is expected to be `key: value` (same as
        `NextActionPolicy.must_include_facts` — kept aligned so the
        renderer sees one input shape).
      - Never invents facts. If critical facts are missing (no
        service / date / time), returns None.
      - Never raises. Any exception → None → LLM fallback.

    Returns:
      str  → deterministic reply text; caller should skip the 2nd LLM.
      None → caller should run the LLM realizer.

    tool_receipts is currently unused but reserved for future use
    (renderer may want to cite the booking confirmation number etc.
    from the receipt).
    """
    try:
        if not isinstance(plan, SemanticPlan):
            return None
        if not plan.requires_deterministic_template():
            return None
        # Convert PlannedFact list → flat dict.
        raw_claims = [f.claim for f in plan.critical_facts()
                      if isinstance(f.claim, str)]
        facts = _facts_dict(raw_claims)
        return _render_confirm_action(facts)
    except Exception:
        return None


def maybe_synthesize_availability(
    tool_results: list[dict],
) -> tuple[Optional[str], bool]:
    """Deterministic renderer for `check_availability` results.

    **Why this exists (2026-08-24):** live-call testing surfaced the
    LLM hallucinating times that were never in `check_availability`'s
    `open_slots` list.  User reports:

      > "the AI always says a random as hell date like sometimes its
      >  available at random times today sometimes tomorrow sometimes
      >  neither and the day after"

    Root cause: the LLM sees `open_slots=[10:00, 14:30, 15:00]` in the
    tool result but under load ignores it and phrases whatever times
    it thinks sound reasonable.  Prompt rules ("Never invent slot times.
    Never say 'not available' without check_availability THIS TURN") are
    advisory; the LLM overrides them.

    The proper fix, per roadmap A2's "SlotProposalRenderer", is a
    deterministic post-tool renderer that pulls the exact slot list from
    the receipt and phrases it — no LLM freeform, zero hallucination
    surface.

    Contract:
      - Fires only when `check_availability` returned successfully with a
        non-empty `open_slots` list this turn.
      - Skips (returns None, False) when: tool errored, no slots
        (fall through to LLM so it can say "we're full that day" using
        prompt rules + FAQ), date_ambiguous / date_unparseable
        (LLM must clarify), or the result is missing the expected keys.
      - Never invents times. Only speaks slots present in `open_slots`.
      - Presents at most 3 slots — a spoken list of 8 candidates is
        overwhelming to a caller and defeats humanness.  Prefer spread:
        pick first, middle, last from the list.
      - Never raises. Any exception → (None, False) → LLM fallback.

    Returns:
      (reply_text, True)  → skip the 2nd LLM.
      (None, False)       → run the LLM realizer.
    """
    try:
        for tr in (tool_results or []):
            if tr.get("name") != "check_availability":
                continue
            if tr.get("error") is not None:
                continue
            result = tr.get("result") or {}
            if not isinstance(result, dict):
                continue
            # Skip when the tool signalled an unresolvable / ambiguous
            # date — the LLM should clarify with the caller, not
            # propose fake slots.
            if any(result.get(k) for k in (
                "date_unparseable", "date_ambiguous",
                "blocked", "error",
            )):
                return None, False
            open_slots = result.get("open_slots")
            if not isinstance(open_slots, list):
                continue
            spoken = _render_slot_proposal(
                open_slots, date_iso=result.get("date"),
            )
            if spoken:
                return spoken, True
        return None, False
    except Exception:
        return None, False


def _pick_spread_slots(slots: list[str], k: int = 3) -> list[str]:
    """From a list of open-slot HH:MM strings, pick up to `k` slots
    that spread across the range (first, middle, last for k=3).

    Rationale: a caller offered 8 back-to-back slots hears noise.
    Offering morning / midday / afternoon gives them a real choice.
    If the list is already short (≤ k), return it verbatim in-order.
    """
    if not slots:
        return []
    if len(slots) <= k:
        return list(slots)
    # Even spacing across indices.  For k=3, len=8 → indices [0, 4, 7].
    step = (len(slots) - 1) / (k - 1) if k > 1 else 0
    picked_indices = sorted({int(round(step * i)) for i in range(k)})
    return [slots[i] for i in picked_indices if 0 <= i < len(slots)]


def _render_slot_proposal(
    open_slots: list[str],
    date_iso: Optional[str] = None,
) -> Optional[str]:
    """Turn `["10:00", "14:30", "15:00"]` → "I've got ten, two thirty,
    or three that day. Which works?"

    date_iso is optional — when provided we say "on Wednesday" for
    orientation; when missing we say "that day" so we never invent a
    date.

    Never emits a time that isn't in the input list.  Never emits a
    date the caller didn't ask about.
    """
    picked = _pick_spread_slots([s for s in open_slots if isinstance(s, str)])
    if not picked:
        return None

    spoken_times = [_format_time_for_speech(t) for t in picked]
    # Strip a.m./p.m. suffix when it's identical across all three —
    # "ten a.m., eleven a.m., twelve p.m." feels robotic; "ten,
    # eleven, or noon" is more human.  Only strip when the leading
    # meridiem block matches for all picks.
    #
    # Simple heuristic: if all three end with " a.m." or all three end
    # with " p.m.", drop the suffix from all but the last.
    if len(spoken_times) >= 2:
        suffixes = [s.rsplit(" ", 1)[-1] if " " in s else "" for s in spoken_times]
        if all(sfx in ("a.m.", "p.m.") for sfx in suffixes) and len(set(suffixes)) == 1:
            spoken_times = [
                s.rsplit(" ", 1)[0] if i < len(spoken_times) - 1 else s
                for i, s in enumerate(spoken_times)
            ]

    # Join the list naturally: "A, B, or C" for 3; "A or B" for 2;
    # "A" for 1.
    if len(spoken_times) == 1:
        list_str = spoken_times[0]
    elif len(spoken_times) == 2:
        list_str = f"{spoken_times[0]} or {spoken_times[1]}"
    else:
        list_str = ", ".join(spoken_times[:-1]) + f", or {spoken_times[-1]}"

    # Date orientation — only when we can render it faithfully.
    when = " that day"
    if date_iso and isinstance(date_iso, str):
        try:
            from datetime import date as _date
            parts = date_iso.split("-")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                weekday = d.strftime("%A")
                when = f" on {weekday}"
        except Exception:
            pass

    return f"I've got {list_str}{when}. Which works?"


__all__ = [
    "maybe_synthesize",
    "maybe_synthesize_availability",
    "render_from_semantic_plan",
]
