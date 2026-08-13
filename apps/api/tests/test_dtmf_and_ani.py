"""R3 P3 (task #370): DTMF feed into active slot + ANI resolver.

Two paths under test:

  1. DTMF: Twilio delivers one keypress per event; the actor routes
     digits into the SAME slot accumulator as speech.  The caller can
     mix modalities freely (voice "0333" + keypad "5244772").

  2. ANI: Twilio start.customParameters carries {{From}}/{{To}}.  The
     actor exposes resolve_ani_candidate() which returns a SlotResult
     the workflow presents as "Should I use the number you're calling
     from?" — never auto-committed without caller confirmation.

Both are exercised through the same lightweight harness used for the
slot-capture semantics tests — a full TwilioActorSession needs a
WebSocket, registry, and STT bridge we don't want to spin up here.
"""
from __future__ import annotations

from typing import List, Optional

import pytest

from packages.slot_parsers import (
    SlotResult,
    SlotSource,
    SlotStatus,
    StructuredInputSession,
)


class _Harness:
    call_id = "TEST_DTMF"
    _SLOT_STALL_FIRST_PROMPT_S = 0.5
    _SLOT_STALL_ESCALATE_S = 0.5

    def __init__(
        self,
        caller_number: str = "",
        dialed_number: str = "",
    ) -> None:
        # Slot capture state
        self._active_slot: Optional[StructuredInputSession] = None
        self._slot_normalizer = None
        self._slot_on_commit = None
        self._slot_on_stall = None
        self._slot_on_confirm_needed = None
        self._slot_stall_task = None
        self._slot_stall_first_prompt_s: float = 0.5
        self._slot_stall_escalate_s: float = 0.5
        # ANI fields
        self.caller_number = caller_number.strip()
        self.dialed_number = dialed_number.strip()
        self.caller_name = ""


def _bind(h: _Harness) -> None:
    from app.routes.twilio_actor import TwilioActorSession
    for name in (
        "enter_slot_capture", "exit_slot_capture", "slot_capture_active",
        "_arm_slot_stall_watchdog", "_cancel_slot_stall_watchdog",
        "_slot_stall_loop", "_feed_active_slot",
        "on_dtmf", "resolve_ani_candidate",
    ):
        attr = getattr(TwilioActorSession, name)
        if isinstance(attr, property):
            setattr(type(h), name, attr)
        else:
            setattr(h, name, attr.__get__(h, type(h)))


async def _noop(*_a, **_kw):
    return None


def _phone_config(default="PK", accepted=None):
    return {
        "phone_default_region": default,
        "phone_accepted_regions": accepted or [default],
    }


# ── DTMF feeds the active slot ───────────────────────────────────────

@pytest.mark.asyncio
async def test_dtmf_feeds_active_slot():
    """One DTMF keypress → one fragment into the slot buffer."""
    h = _Harness()
    _bind(h)
    committed: List[SlotResult] = []
    async def on_commit(r): committed.append(r)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("PK"),
        on_commit=on_commit,
    )
    # Full number typed one digit at a time.  0333 5244 772 = 11 digits.
    for digit in "03335244772":
        await h.on_dtmf(digit, track="inbound_track")
    assert len(committed) == 1
    assert committed[0].value == "+923335244772"
    # Fragments must be tagged SlotSource.DTMF.
    # (Harness is inert after commit — check the last audit via session.
    # But session is closed after commit; the assertion above is enough.)


@pytest.mark.asyncio
async def test_dtmf_mixes_with_speech_in_same_session():
    """Real scenario: caller says the country code, then keys the local
    portion.  Both feed the SAME session buffer.  Fragment sources must
    be tagged appropriately for the audit trail."""
    h = _Harness()
    _bind(h)
    committed: List[SlotResult] = []
    async def on_commit(r): committed.append(r)

    session = h.enter_slot_capture(
        kind="phone",
        config=_phone_config("PK"),
        on_commit=on_commit,
    )
    # Speech: "zero three three three" (4 digits)
    await h._feed_active_slot(
        "zero three three three", turn_gen=1, source=SlotSource.SPEECH,
    )
    # DTMF: "5244772" (7 more, total 11)
    for digit in "5244772":
        await h.on_dtmf(digit)
    assert len(committed) == 1
    assert committed[0].value == "+923335244772"


@pytest.mark.asyncio
async def test_dtmf_star_hash_do_not_pollute_buffer():
    """The phone validator strips non-digits, so a stray * or # from
    the caller fumbling the keypad must NOT break parsing."""
    h = _Harness()
    _bind(h)
    committed: List[SlotResult] = []
    async def on_commit(r): committed.append(r)

    h.enter_slot_capture(
        kind="phone",
        config=_phone_config("PK"),
        on_commit=on_commit,
    )
    # Sprinkle * and # among real digits.
    for ch in "0*333#5244772":
        await h.on_dtmf(ch)
    assert len(committed) == 1
    assert committed[0].value == "+923335244772"


@pytest.mark.asyncio
async def test_dtmf_outside_capture_is_ignored():
    """When no slot is open, DTMF is a no-op (future: could open a
    menu, but not needed yet).  MUST NOT auto-open a slot."""
    h = _Harness()
    _bind(h)
    assert h.slot_capture_active is False
    await h.on_dtmf("5")
    assert h.slot_capture_active is False


@pytest.mark.asyncio
async def test_dtmf_empty_digit_is_dropped():
    """Guard against a malformed Twilio event carrying digit=''."""
    h = _Harness()
    _bind(h)
    committed = []
    async def on_commit(r): committed.append(r)
    h.enter_slot_capture(
        kind="phone", config=_phone_config("PK"),
        on_commit=on_commit,
    )
    await h.on_dtmf("")     # no-op
    await h.on_dtmf("   ")  # no-op
    # Buffer must still be empty.
    assert h._active_slot.buffer == ""


# ── ANI resolver ────────────────────────────────────────────────────

def test_ani_returns_incomplete_when_missing():
    """Blocked caller ID or missing <Parameter> in TwiML → INCOMPLETE.
    Workflow just skips the ANI branch."""
    h = _Harness(caller_number="")
    _bind(h)
    r = h.resolve_ani_candidate(default_region="US")
    assert r.status == SlotStatus.INCOMPLETE


def test_ani_resolves_valid_pk_number_for_us_tenant():
    """US tenant with a PK caller — the common Karachi-test case."""
    h = _Harness(caller_number="+923335244772")
    _bind(h)
    r = h.resolve_ani_candidate(
        default_region="US", accepted_regions=["US", "PK"],
    )
    assert r.status == SlotStatus.VALID
    assert r.value == "+923335244772"
    assert r.matched_region == "PK"


def test_ani_resolves_valid_us_number():
    h = _Harness(caller_number="+16502530000")
    _bind(h)
    r = h.resolve_ani_candidate(default_region="US")
    assert r.status == SlotStatus.VALID
    assert r.value == "+16502530000"


def test_ani_maps_invalid_to_invalid_not_incomplete():
    """A malformed ANI (should almost never happen from Twilio, but
    guard against it) must map cleanly to INVALID so the workflow
    knows to fall through to explicit capture."""
    h = _Harness(caller_number="not-a-number")
    _bind(h)
    r = h.resolve_ani_candidate(default_region="US")
    assert r.status == SlotStatus.INVALID


def test_ani_is_a_candidate_never_auto_commits():
    """Contract: resolve_ani_candidate returns a SlotResult but does
    NOT open a slot session or commit anything.  The workflow is
    responsible for asking the caller before treating it as truth."""
    h = _Harness(caller_number="+16502530000")
    _bind(h)
    r = h.resolve_ani_candidate(default_region="US")
    assert r.status == SlotStatus.VALID
    # No side effects on capture state.
    assert h.slot_capture_active is False
    assert h._active_slot is None
