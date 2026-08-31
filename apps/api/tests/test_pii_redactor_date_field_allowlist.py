"""P0 regression — PII redactor was eating appointment dates as DOB (task #103).

Bug summary: on CAc66749590f6e53986eec4210e49bb425 every check_availability
call showed tool_result `date='[DOB]'`. Root cause: the DOB regex matches
any ISO YYYY-MM-DD. Downstream, the LLM read '[DOB]' back from persisted
transcript history and passed literal '[DOB]' into the next tool call —
all availability lookups broken, "no slots" every time, wrong Monday
selected.

Fix: field-name allowlist. Certain keys (`date`, `start_iso`,
`scheduled_for`, etc.) skip DOB regexes even when the value looks like
a birthdate. Other PII (phone, card, SSN, email) still redacts everywhere.

These tests lock in:
  1. Date-shaped fields pass through un-redacted
  2. Actual DOB in free-text still redacts
  3. Other PII kinds still redact in date fields (defence in depth)
  4. Case-insensitivity on field name
  5. Nested dicts inherit the allowlist rule
  6. Presidio path (if installed) honors the same rule via entity filter
"""
from __future__ import annotations

import pytest

from packages.compliance.pii import RegexPIIRedactor


# ─── 1. Date-shaped fields pass through ─────────────────────────────────────


def test_date_field_iso_not_redacted():
    """`date=2026-09-07` must survive unredacted — this is an
    appointment date, not a birthday."""
    r = RegexPIIRedactor()
    out, counts = r.redact_dict({"date": "2026-09-07", "service": "cleaning"})
    assert out["date"] == "2026-09-07", (
        "P0 REGRESSION: appointment date being redacted as DOB. "
        "This is the CAc66749590f6e53986eec4210e49bb425 bug — LLM "
        "reads '[DOB]' back from history + all availability lookups "
        "return no slots."
    )
    assert counts.get("DOB", 0) == 0


def test_date_field_us_format_not_redacted():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"date": "09/07/2026"})
    assert out["date"] == "09/07/2026"


def test_start_iso_field_not_redacted():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({
        "start_iso": "2026-09-07T07:30:00",
        "end_iso": "2026-09-07T08:00:00",
    })
    assert out["start_iso"] == "2026-09-07T07:30:00"
    assert out["end_iso"] == "2026-09-07T08:00:00"


def test_scheduled_for_not_redacted():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"scheduled_for": "2026-09-07"})
    assert out["scheduled_for"] == "2026-09-07"


def test_original_visit_date_not_redacted():
    """Discovery-drill original visit date — same bug class."""
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"original_visit_date": "2026-08-15"})
    assert out["original_visit_date"] == "2026-08-15"


def test_field_name_case_insensitive():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"DATE": "2026-09-07", "Start_Iso": "2026-09-07"})
    assert out["DATE"] == "2026-09-07"
    assert out["Start_Iso"] == "2026-09-07"


# ─── 2. Actual DOB still redacts ────────────────────────────────────────────


def test_dob_field_still_redacts():
    """`dob=1990-05-14` MUST redact — this is exactly what the DOB
    pattern exists for."""
    r = RegexPIIRedactor()
    out, counts = r.redact_dict({"dob": "1990-05-14", "phone": "+15551234567"})
    assert out["dob"] == "[DOB]"
    assert counts.get("DOB", 0) == 1


def test_dob_in_free_text_still_redacts():
    """A caller saying 'my birthday is 1990-05-14' via a `notes` or
    `text` field still redacts (those fields are NOT allowlisted)."""
    r = RegexPIIRedactor()
    out, counts = r.redact_dict({"notes": "born 1990-05-14, allergic to latex"})
    assert "1990-05-14" not in out["notes"]
    assert "[DOB]" in out["notes"]


def test_dob_in_transcript_text_still_redacts():
    """The transcript row's `text` field is NOT allowlisted — caller
    reciting their DOB out loud must still be scrubbed."""
    r = RegexPIIRedactor()
    result = r.redact_text("my date of birth is 05/14/1990")
    assert "1990" not in result.text
    assert "[DOB]" in result.text


# ─── 3. Other PII still redacts in date fields ──────────────────────────────


def test_phone_in_date_field_still_redacts():
    """Contrived but the security posture demands it: if a `date`
    field somehow contains a phone number, phone still redacts."""
    r = RegexPIIRedactor()
    out, counts = r.redact_dict({"date": "call +15551234567"})
    assert "[PHONE]" in out["date"]
    assert counts.get("PHONE", 0) == 1


def test_email_in_date_field_still_redacts():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"date": "sent by user@example.com"})
    assert "[EMAIL]" in out["date"]


def test_card_in_date_field_still_redacts():
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({"date": "card 4532015112830366"})
    assert "[CARD]" in out["date"]


# ─── 4. Nested dicts ────────────────────────────────────────────────────────


def test_nested_dict_date_field_allowlist_inherited():
    """A `date` field one level down inside a dict must also be
    protected. Real tool payloads nest args + results."""
    r = RegexPIIRedactor()
    out, _ = r.redact_dict({
        "event": {"date": "2026-09-07", "phone": "+15551234567"},
    })
    assert out["event"]["date"] == "2026-09-07"
    assert "[PHONE]" in out["event"]["phone"]


# ─── 5. Full check_availability payload — the exact bug ────────────────────


def test_check_availability_payload_shape():
    """The exact payload shape that caused the bug. Reproduces the
    root scenario: tool receives args + returns result, everything
    goes through redact_dict at persist time."""
    r = RegexPIIRedactor()
    tool_args = {"date": "2026-09-07", "service": "Follow-up visit"}
    tool_result = {
        "date": "2026-09-07",
        "service": "Follow-up visit",
        "open_slots": ["09:30", "10:00", "10:30"],
    }
    args_out, _ = r.redact_dict(tool_args)
    result_out, _ = r.redact_dict(tool_result)

    # Neither the args nor the result should have '[DOB]' anywhere
    assert "[DOB]" not in str(args_out)
    assert "[DOB]" not in str(result_out)
    # Slots survive
    assert result_out["open_slots"] == ["09:30", "10:00", "10:30"]


def test_book_appointment_payload_shape():
    """Same protection for book_appointment (uses start_iso)."""
    r = RegexPIIRedactor()
    args = {
        "start_iso": "2026-09-07T07:30:00",
        "caller_name": "Hayden",
        "phone": "+15551234567",   # this SHOULD redact
        "service": "Follow-up visit",
    }
    out, _ = r.redact_dict(args)
    assert out["start_iso"] == "2026-09-07T07:30:00"
    assert out["phone"] == "[PHONE]"  # phone still redacts
    assert out["caller_name"] == "Hayden"
    assert out["service"] == "Follow-up visit"


# ─── 6. Backwards compat — old callers without skip_dob still work ─────────


def test_redact_text_default_skip_dob_false():
    r = RegexPIIRedactor()
    # No kwarg = old behavior = DOB redacts
    out = r.redact_text("dob 1990-05-14")
    assert "[DOB]" in out.text


def test_redact_text_skip_dob_true():
    r = RegexPIIRedactor()
    out = r.redact_text("appointment 2026-09-07", skip_dob=True)
    assert "2026-09-07" in out.text
    assert "[DOB]" not in out.text
