"""Guard-LLM validator for write-side tool calls.

The brain's primary LLM fires tools directly. If it hallucinates a caller
name, phone, or scheduled time that never appeared in the transcript, we
end up with a bogus booking, a phantom Calendar event, and a real client's
data corrupted.

This module runs a CHEAP second LLM (or rule engine) on every write-side
tool call and verifies:

  - The `caller_name`, `phone`, `service`, and `start_iso` arguments were
    actually said by the caller (or reasonably paraphrased).
  - No obvious hallucinations (e.g. LLM invented "John Doe" when caller
    never gave a name).

Returns a GuardVerdict with .approved (bool) + .reason (str). Callers
(the brain) skip the tool call and ask the caller for confirmation on
reject.

Model: any cheap fast LLM. Groq `llama-3.1-8b-instant` or NVIDIA
`meta/llama-3.1-8b-instruct` are perfect — ~100ms, ~$0 per check.

Wired into: ReceptionistBrain.handle_user_turn — before every
book_appointment / book_reservation / book_viewing tool dispatch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


log = logging.getLogger(__name__)


BOOKING_TOOL_NAMES = frozenset({"book_appointment", "book_reservation", "book_viewing"})


@dataclass
class GuardVerdict:
    approved: bool
    reason: str          # short machine tag
    detail: str = ""     # human-readable, shown to caller if we reject


GUARD_PROMPT = """You are a validator that decides if it's safe to write a booking to a database.

You will see:
1. The tool the primary AI wants to call, with its arguments (name, phone, service, time).
2. The recent call transcript.

Your job: check that each REQUIRED argument was actually stated by the caller (USER role) in the transcript, or is a trivial normalization of what they said.

APPROVE if every required argument traces back to the caller's own words.
REJECT if the AI is HALLUCINATING (inventing a name, phone, or time the caller never gave), or if the caller only gave partial info and the AI is filling in blanks.

Output ONE line, no prose:
    APPROVE
        or
    REJECT: <short reason>

Examples of correct REJECTs:
  - Caller said "book me tomorrow at 10" but never gave name → REJECT: no name from caller
  - Caller said name and service but never gave phone → REJECT: no phone from caller
  - AI is booking for time caller never confirmed → REJECT: time not confirmed
Examples of correct APPROVEs:
  - Caller: "I'm John Carter, 555-1234" — AI books with name="John Carter" phone="5551234" → APPROVE
"""


async def validate_write(
    llm: "LLMProvider",
    tool_name: str,
    tool_arguments: dict,
    transcript_lines: list[str],
) -> GuardVerdict:
    """Ask the guard LLM to approve or reject a booking tool call.

    Fails open (approves) on any exception — a broken guard shouldn't
    block real bookings during an outage. Logs the failure so we notice."""
    if tool_name not in BOOKING_TOOL_NAMES:
        return GuardVerdict(approved=True, reason="not_write_tool")

    # Pre-check: reject obvious hallucinations without hitting the LLM.
    # If caller_name looks like a common placeholder, reject.
    name = str(tool_arguments.get("caller_name") or "").strip()
    phone = str(tool_arguments.get("phone") or "").strip()
    if not name:
        return GuardVerdict(approved=False, reason="no_name", detail="I didn't catch your name — could you say it again?")
    if not phone or len(phone.replace("-", "").replace(" ", "").replace("+", "").replace("(", "").replace(")", "")) < 7:
        return GuardVerdict(approved=False, reason="no_phone", detail="I didn't catch a valid phone number — could you say it again?")

    lowered_name = name.lower()
    placeholder_names = {"john doe", "jane doe", "test user", "example", "n/a", "unknown", "customer", "caller"}
    if lowered_name in placeholder_names:
        return GuardVerdict(approved=False, reason="placeholder_name", detail="I didn't catch your real name — could you say it again?")

    # Owner-mode / fake-booking fast-path: caller declared this is a test call
    # OR asked us to book something they explicitly said shouldn't be real.
    # Harness owner-01 (2026-07-19) hit this: agent booked a "physical exam"
    # after caller said "make it seem like a real appointment so you don't
    # know I'm just testing your system." Real receptionist would refuse.
    #
    # Scanning transcript_lines because these declarations often happen in
    # earlier turns and the model forgets by turn 4.
    import re as _re_owner
    transcript_lower_full = " ".join(transcript_lines).lower()
    test_mode_patterns = [
        r"\b(?:just|only)\s+testing\b",
        r"\btesting\s+(?:the\s+system|the\s+agent|the\s+AI|you)\b",
        r"\b(?:don'?t|do\s+not)\s+(?:actually\s+)?book\b",
        r"\b(?:pretend|make\s+it\s+seem)\s+(?:like\s+)?(?:it'?s\s+)?real\b",
        r"\bfake\s+(?:booking|appointment|reservation)\b",
        r"\bdon'?t\s+create\s+a\s+(?:real|actual)\s+(?:booking|appointment)\b",
        r"\bthis\s+is\s+(?:a\s+)?test\s+(?:call|booking|appointment)?\b",
        r"\bfor\s+(?:demo|demonstration|test)\s+purpose",
        r"\bmark\s+(?:as|it)\s+(?:a\s+)?test\b",
    ]
    for pat in test_mode_patterns:
        if _re_owner.search(pat, transcript_lower_full):
            return GuardVerdict(
                approved=False,
                reason="test_mode_declared",
                detail=(
                    "You mentioned this is a test call — for safety I don't create real "
                    "bookings when I'm asked to pretend. If you'd like to make a real "
                    "appointment, just let me know and I'll take that."
                ),
            )

    # Anti-hallucination fast-path: if the booking has a specific date/time,
    # make sure something in the transcript referenced it. LLM sometimes
    # invents "2024-07-09" or "March 15" when caller said only "next Tuesday".
    # This was the #1 hard-fail category in the 2026-07-18 baseline.
    start_iso = str(tool_arguments.get("start_iso") or tool_arguments.get("preferred_date") or "").strip()
    if start_iso:
        # Extract year/month/day tokens from ISO string
        import re as _re
        year_match = _re.search(r"\b(\d{4})\b", start_iso)
        month_names = ["january", "february", "march", "april", "may", "june",
                        "july", "august", "september", "october", "november", "december"]
        month_short = ["jan", "feb", "mar", "apr", "may", "jun",
                        "jul", "aug", "sep", "oct", "nov", "dec"]
        day_words = ["today", "tomorrow", "next", "monday", "tuesday", "wednesday",
                        "thursday", "friday", "saturday", "sunday",
                        "week", "asap", "any time", "whenever"]
        transcript_lower = " ".join(transcript_lines).lower()
        # If a specific year was fabricated (like "2024" when caller said only
        # "next Tuesday"), and neither the year nor day-of-week nor a natural
        # date word appears in transcript, reject.
        year = year_match.group(1) if year_match else None
        date_referenced_by_caller = (
            (year and year in transcript_lower)
            or any(m in transcript_lower for m in month_names + month_short)
            or any(d in transcript_lower for d in day_words)
        )
        if not date_referenced_by_caller:
            return GuardVerdict(
                approved=False,
                reason="hallucinated_date",
                detail="Sorry — I want to make sure I have the right day. What day works for you?",
            )

    # Slow path: LLM check. Only for write tools that survived the fast path.
    transcript = "\n".join(transcript_lines[-20:])  # last 20 turns is plenty
    user_msg = (
        f"Tool: {tool_name}\n"
        f"Arguments: {tool_arguments}\n\n"
        f"Transcript:\n{transcript}\n\n"
        f"APPROVE or REJECT?"
    )

    try:
        resp = await llm.complete(
            messages=[
                {"role": "system", "content": GUARD_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            tools=None,
            temperature=0.0,
            max_tokens=32,
        )
    except Exception as e:
        log.warning("write_guard LLM check failed, failing open: %s", e)
        return GuardVerdict(approved=True, reason="guard_error", detail=str(e))

    raw = (resp.text or "").strip()
    upper = raw.upper()
    if upper.startswith("APPROVE"):
        return GuardVerdict(approved=True, reason="approved")
    if upper.startswith("REJECT"):
        # Reason is everything after "REJECT:" if present
        reason_tail = raw.split(":", 1)[1].strip() if ":" in raw else "hallucination_detected"
        return GuardVerdict(
            approved=False,
            reason="rejected",
            detail=f"I want to make sure I have that right — {reason_tail}. Could you confirm?"
        )
    # Malformed output: fail open with a log
    log.warning("write_guard got unparseable output %r; failing open", raw)
    return GuardVerdict(approved=True, reason="unparseable_response")
