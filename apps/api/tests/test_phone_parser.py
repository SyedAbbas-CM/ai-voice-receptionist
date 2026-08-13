"""R3 P0: structured-input engine — phone slot tests.

Three layers under test:
  Layer A — normalize_spoken_digits (region-agnostic)
  Layer B — parse_phone (libphonenumber wrapper)
  Engine  — StructuredInputSession (feeds A → B, accumulates fragments)

Every VALID case here is either a real caller pattern from actual
2026-08-13 call logs OR a plausible regional variation.  If any of
these fail, the engine is wrong — do NOT degrade to "close enough."
Digit accuracy is the whole point of R3.
"""
from __future__ import annotations

import pytest

from packages.slot_parsers import (
    PhoneStatus,
    SlotSource,
    SlotStatus,
    StructuredInputSession,
    get_slot_handlers,
    normalize_spoken_digits,
    parse_phone,
)


# ── Layer A: normalize_spoken_digits ─────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Raw digit forms
    ("0333",              "0333"),
    ("0-3-3-3",           "0333"),
    ("0.3.3.3",           "0333"),
    ("0333, 1310260",     "03331310260"),
    ("5244 7724",         "52447724"),
    ("+923335244772",     "+923335244772"),
    # Word-spelled digits
    ("zero three three three",          "0333"),
    ("oh three three three",            "0333"),
    ("naught three three three",        "0333"),
    ("one two three four",              "1234"),
    # Double/triple/quadruple
    ("double three",                    "33"),
    ("triple three",                    "333"),
    ("zero double three",               "033"),
    ("zero triple three",               "0333"),
    ("triple three, four four",         "33344"),
    ("quadruple seven",                 "7777"),
    # Mixed forms
    ("oh three, three three",           "0333"),
    ("zero-three-three-three",          "0333"),
    # E.164 with plus
    ("plus nine two three three three", "+92333"),
    ("+92 333",                         "+92333"),
    # Filler words stripped
    ("uh, my number is oh three three three", "0333"),
    ("okay, the number is 0333",              "0333"),
    ("it's zero double three",                "033"),
    # Empty / non-numeric
    ("",                                ""),
    ("hello there",                     ""),
    ("Alright.",                        ""),
])
def test_normalize_spoken_digits(text: str, expected: str):
    assert normalize_spoken_digits(text) == expected


# ── Layer B: parse_phone (libphonenumber wrapper) — PK ────────────────

@pytest.mark.parametrize("text,expected_status,expected_value", [
    # Hamzah's actual number in every form we might see
    ("03335244772",       PhoneStatus.COMPLETE, "+923335244772"),
    ("+923335244772",     PhoneStatus.COMPLETE, "+923335244772"),
    ("00923335244772",    PhoneStatus.COMPLETE, "+923335244772"),
    ("923335244772",      PhoneStatus.COMPLETE, "+923335244772"),
    # Partial
    ("033",               PhoneStatus.PARTIAL,  None),
    ("0333 5244",         PhoneStatus.PARTIAL,  None),
    # Too long
    ("03335244772" + "1234", PhoneStatus.TOO_LONG, None),
    # Empty
    ("",                  PhoneStatus.EMPTY,    None),
])
def test_parse_phone_pk(text: str, expected_status: PhoneStatus, expected_value):
    result = parse_phone(text, default_region="PK")
    assert result.status == expected_status, (
        f"got {result.status} for {text!r} ({result.reason})"
    )
    if expected_value is not None:
        assert result.value == expected_value


# ── Layer B — US (real allocated numbers, not 555) ────────────────────

@pytest.mark.parametrize("text,expected_status,expected_value", [
    # libphonenumber considers 555 valid (only 555-01XX is reserved
    # for fiction, per NANPA).  Full 10-digit numbers in NYC 212 area
    # come back COMPLETE.
    ("2125550000",        PhoneStatus.COMPLETE, "+12125550000"),
    ("+12125550000",      PhoneStatus.COMPLETE, "+12125550000"),
    # Google's HQ example number — canonically valid.
    ("6502530000",        PhoneStatus.COMPLETE, "+16502530000"),
    ("650-253-0000",      PhoneStatus.COMPLETE, "+16502530000"),
    # Partial (< min NANP length of 10)
    ("650",               PhoneStatus.PARTIAL,  None),
    # Empty
    ("",                  PhoneStatus.EMPTY,    None),
])
def test_parse_phone_us(text: str, expected_status: PhoneStatus, expected_value):
    result = parse_phone(text, default_region="US")
    assert result.status == expected_status, (
        f"got {result.status} for {text!r} ({result.reason})"
    )
    if expected_value is not None:
        assert result.value == expected_value


# ── Multi-region: US tenant that also accepts PK callers ──────────────

def test_parse_phone_us_tenant_accepts_pk_number():
    """Common case: US business, PK caller (like our Karachi tests)."""
    result = parse_phone(
        "03335244772",
        default_region="US",
        accepted_regions=["US", "PK"],
    )
    assert result.status == PhoneStatus.COMPLETE
    assert result.value == "+923335244772"
    assert result.matched_region == "PK"


def test_parse_phone_multi_region_prefers_valid():
    """If a number is COMPLETE in ANY accepted region, return that."""
    result = parse_phone(
        "6502530000",
        default_region="PK",
        accepted_regions=["PK", "US"],
    )
    assert result.status == PhoneStatus.COMPLETE
    assert result.value == "+16502530000"
    assert result.matched_region == "US"


# ── StructuredInputSession — end-to-end for phone slot ────────────────

def _phone_session(default_region: str = "US", accepted: list[str] = None):
    normalizer, validator = get_slot_handlers("phone")
    session = StructuredInputSession(
        slot_type="phone",
        validator=validator,
        config={
            "phone_default_region": default_region,
            "phone_accepted_regions": accepted or [default_region],
        },
    )
    return session, normalizer


def test_session_pk_hamzah_scenario():
    """Hamzah's actual STT sequence (2026-08-13 19:07:51-19:08:01):
      "Zero, double three,"       -> INCOMPLETE (3 digits)
      "52447724."                 -> VALID (11 digits total)
    """
    session, normalizer = _phone_session("PK", ["PK", "US"])

    r1 = session.feed("Zero, double three,", normalize=normalizer)
    assert r1.status == SlotStatus.INCOMPLETE, (
        f"expected INCOMPLETE after 3 digits, got {r1.status}: {r1.reason}"
    )

    r2 = session.feed("52447724.", normalize=normalizer)
    assert r2.status == SlotStatus.VALID, (
        f"expected VALID after full number, got {r2.status}: {r2.reason}"
    )
    assert r2.value == "+923352447724"
    assert r2.matched_region == "PK"


def test_session_pk_word_spelled_across_three_turns():
    """Hamzah's real number 03335244772 = 11 digits, spoken across 3 turns.
    zero triple three (0333) + five two four four (5244) + seven seven two (772)
    = 03335244772 = 11 digits = valid PK mobile.
    """
    session, normalizer = _phone_session("PK")
    session.feed("zero triple three", normalize=normalizer)
    session.feed("five two four four", normalize=normalizer)
    r = session.feed("seven seven two", normalize=normalizer)
    assert r.status == SlotStatus.VALID, f"got {r.status}: {r.reason} buf={session.buffer!r}"
    assert r.value == "+923335244772"


def test_session_us_split_across_turns():
    session, normalizer = _phone_session("US")
    session.feed("six five zero", normalize=normalizer)
    r = session.feed("253 0000", normalize=normalizer)
    assert r.status == SlotStatus.VALID
    assert r.value == "+16502530000"


def test_session_reset_on_correction():
    """Caller: '0333 5244 772 ... no wait, 0300 1234 567' (both 11-digit PK)."""
    session, normalizer = _phone_session("PK")
    session.feed("0333 5244 772", normalize=normalizer)  # 11 digits
    assert session.result().status == SlotStatus.VALID, (
        f"{session.result().status}: {session.result().reason} buf={session.buffer!r}"
    )

    session.reset(reason="caller correction")
    r = session.feed("0300 1234 567", normalize=normalizer)  # 11 digits
    assert r.status == SlotStatus.VALID, f"{r.status}: {r.reason} buf={session.buffer!r}"
    assert r.value == "+923001234567"


def test_session_ignores_pure_filler():
    session, normalizer = _phone_session("US")
    r1 = session.feed("uh", normalize=normalizer)
    assert r1.status == SlotStatus.INCOMPLETE
    assert session.buffer == ""
    r2 = session.feed("hello there", normalize=normalizer)
    assert r2.status == SlotStatus.INCOMPLETE
    assert session.buffer == ""


def test_session_commit_makes_it_inert():
    session, normalizer = _phone_session("PK")
    session.feed("0333 5244 7724", normalize=normalizer)
    session.commit("+923352447724")

    # After commit, further feeds are no-ops.
    r = session.feed("some other stuff", normalize=normalizer)
    assert r.status == SlotStatus.VALID
    assert r.value == "+923352447724"


def test_session_dtmf_and_speech_merge():
    """DTMF and speech feed the SAME accumulator.  This is a spec
    requirement — Twilio Media Streams sends dtmf events, we treat
    them as another SlotSource going into the same session.

    0333 (speech) + 5244 (dtmf) + 772 (dtmf) = 03335244772 = 11 digits.
    """
    session, normalizer = _phone_session("PK")
    session.feed("zero three three three", normalize=normalizer,
                 source=SlotSource.SPEECH)
    session.feed("5244", normalize=normalizer, source=SlotSource.DTMF)
    r = session.feed("772", normalize=normalizer, source=SlotSource.DTMF)
    assert r.status == SlotStatus.VALID, f"{r.status}: {r.reason} buf={session.buffer!r}"
    assert r.value == "+923335244772"
    sources = [f["source"] for f in session.audit_trail()]
    assert sources == ["speech", "dtmf", "dtmf"]


def test_session_us_tenant_pk_caller():
    """Real production scenario: US-based Smile Dental, PK caller.
    Config: default=US, accepted=[US, PK].  Uses Hamzah's real 11-digit
    number 03335244772."""
    session, normalizer = _phone_session("US", ["US", "PK"])
    r = session.feed("zero triple three, five two four four, seven seven two",
                     normalize=normalizer)
    assert r.status == SlotStatus.VALID, f"{r.status}: {r.reason} buf={session.buffer!r}"
    assert r.value == "+923335244772"
    assert r.matched_region == "PK"
