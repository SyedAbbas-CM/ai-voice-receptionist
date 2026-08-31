"""CALL-BUG-06 regression: stopword tokens must never award fuzzy
service-match bonus.

Real trace: CAbd671430f1297c1bbe0640a977060f1f
  Turn 03 caller: "Hello. am I talking with"
  Turn 04 service_resolution event:
    spoken="Hello. am I talking with"
    canonical_name="New patient exam with X-rays"
    confidence=0.0 (from event payload; actual match was 0.60)
    kind=match_fuzzy

The word "with" (4+ chars, matched _similarity token-bonus) is in
"New patient exam with X-rays" → wrong service persisted for the
rest of the call → agent asked wrong follow-up questions on turns
06/08/10/12 instead of just booking.

Fix: exclude filler / preposition / greeting tokens from the fuzzy
overlap bonus in packages/integrations/service_aliases.py.
"""
from __future__ import annotations

import pytest

from packages.integrations.service_aliases import (
    ServiceMatchKind,
    resolve_service,
)


class _Svc:
    def __init__(self, name): self.name = name


@pytest.fixture
def clinic_services():
    return [_Svc(n) for n in [
        "New patient exam with X-rays",
        "Adult cleaning",
        "Composite filling",
        "Zoom whitening",
        "Invisalign consultation",
        "Implant consultation",
        "Emergency exam",
        "Pediatric first visit",
        "Follow-up visit",
        "Adult recall exam",
    ]]


# ─── The exact bug: hearing-check utterance must NOT match a service ────


def test_hearing_check_returns_unknown(clinic_services):
    """The exact utterance from the failing call. Must not match any
    service just because 'with' appears in a service name."""
    m = resolve_service("Hello. am I talking with", clinic_services)
    assert m.kind == ServiceMatchKind.UNKNOWN, (
        f"hearing-check utterance leaked as {m.kind.value} → {m.canonical_name!r}"
    )


def test_just_with_word_returns_unknown(clinic_services):
    m = resolve_service("with", clinic_services)
    assert m.kind == ServiceMatchKind.UNKNOWN


def test_am_i_talking_with_returns_unknown(clinic_services):
    m = resolve_service("am i talking with", clinic_services)
    assert m.kind == ServiceMatchKind.UNKNOWN


@pytest.mark.parametrize("utt", [
    "hello",
    "hey there",
    "yeah",
    "okay just calling to check",
    "sorry can you hear me",
    "would you have time",
    "well I want to book something",
])
def test_common_fillers_do_not_leak_service_match(utt, clinic_services):
    m = resolve_service(utt, clinic_services)
    assert m.kind == ServiceMatchKind.UNKNOWN, (
        f"filler {utt!r} matched {m.canonical_name!r} at {m.confidence:.2f}"
    )


# ─── Regression guard: real service words STILL match ─────────────────


def test_follow_up_still_matches_exact(clinic_services):
    m = resolve_service("follow-up", clinic_services)
    assert m.kind == ServiceMatchKind.MATCH_EXACT
    assert m.canonical_name == "Follow-up visit"


def test_cleaning_still_matches_exact(clinic_services):
    m = resolve_service("cleaning", clinic_services)
    assert m.kind == ServiceMatchKind.MATCH_EXACT
    assert m.canonical_name == "Adult cleaning"


def test_implant_consultation_still_matches_exact(clinic_services):
    m = resolve_service("implant consultation", clinic_services)
    assert m.kind == ServiceMatchKind.MATCH_EXACT
    assert m.canonical_name == "Implant consultation"


def test_i_want_a_follow_up_still_resolves(clinic_services):
    """The realistic caller utterance from turn 09 of the failing call."""
    m = resolve_service("I want to book a follow-up", clinic_services)
    assert m.kind in (ServiceMatchKind.MATCH_EXACT, ServiceMatchKind.MATCH_FUZZY)
    assert m.canonical_name == "Follow-up visit"


def test_follow_up_with_that_still_resolves(clinic_services):
    """Turn 09 verbatim — 'with' is a stopword but 'follow-up' still
    triggers the alias-keyword path (not the fuzzy overlap path)."""
    m = resolve_service("yeah I wanna do a follow-up with that", clinic_services)
    assert m.canonical_name == "Follow-up visit"
