"""Bug #149 fix: known_slots['service'] populates from caller utterance.

Diagnosed 2026-08-30 from CAa8d209cff78e065909410a7ab76b5873. Test call #2
had policy fire ASK_SLOT(service) 14 times because _extract_known_slots
reads only from booking tool receipts. Caller said 'an exam' / 'follow-up'
never landed in known_slots['service'], so policy kept asking.

Fix: resolve_service_from_utterances runs the alias resolver on each
recent caller utterance and returns the canonical service name if any
match. Brain calls it to augment known_slots BEFORE compute_missing_slots.
"""
from __future__ import annotations

import pytest

from packages.core_agent.missing_slots import (
    resolve_service_from_utterances,
    compute_missing_slots,
)
from packages.schemas import ServiceOffering


class _Business:
    """Minimal shape resolve_service expects."""
    def __init__(self, services):
        self.services = services


_CLINIC = _Business([
    ServiceOffering(
        name="Adult cleaning", duration_minutes=45, description="",
    ),
    ServiceOffering(
        name="Emergency exam", duration_minutes=30, description="",
    ),
    ServiceOffering(
        name="Follow-up visit", duration_minutes=30, description="",
    ),
    ServiceOffering(
        name="New patient exam with X-rays",
        duration_minutes=60, description="",
    ),
    ServiceOffering(
        name="Invisalign consultation",
        duration_minutes=45, description="",
    ),
])


# ── direct alias matches ─────────────────────────────────────


def test_an_exam_alone_returns_none_ambiguous():
    """The exact Abbas test-call #2 trigger.  'an exam' matches
    multiple services (Emergency exam + New patient exam) — the
    resolver correctly reports AMBIGUOUS and this helper returns
    None so the policy stays in ASK_SLOT(service) and the agent
    asks 'what kind of exam?'.

    This IS the correct product behavior — audit call: 'an exam' is
    an ambiguous SERVICE that needs disambiguation.  (Distinct from
    'a follow-up' which the audit called an under-specified INTENT.)
    """
    r = resolve_service_from_utterances(["an exam"], _CLINIC)
    # Correctly ambiguous → None.  Not a bug.
    assert r is None


def test_a_follow_up_resolves():
    """Christiaan/Abbas 'follow-up' trigger."""
    r = resolve_service_from_utterances(
        ["I'd like a follow-up please"], _CLINIC,
    )
    assert r == "Follow-up visit"


def test_cleaning_resolves():
    r = resolve_service_from_utterances(["I need a cleaning"], _CLINIC)
    assert r is not None
    assert "cleaning" in r.lower()


def test_toothache_resolves_to_emergency():
    r = resolve_service_from_utterances(
        ["I have a bad toothache"], _CLINIC,
    )
    assert r == "Emergency exam"


def test_braces_resolves_to_invisalign():
    r = resolve_service_from_utterances(
        ["I want to look into braces"], _CLINIC,
    )
    assert r == "Invisalign consultation"


# ── multi-word inside longer utterance ────────────────────


def test_short_phrase_extracted_from_longer_utterance():
    """Real callers embed service names inside longer sentences.
    'I need a cleaning tomorrow' — the resolver finds 'cleaning'
    even when embedded in a longer utterance."""
    r = resolve_service_from_utterances(
        ["I need a cleaning tomorrow please"], _CLINIC,
    )
    # 'cleaning' matches Adult cleaning uniquely.
    assert r is not None
    assert "cleaning" in r.lower()


def test_ambiguous_word_inside_longer_utterance_returns_none():
    """When the short-phrase extract only finds an AMBIGUOUS word
    (like 'exam' which maps to multiple services), None is returned
    — service stays 'missing' → policy asks the caller to clarify.
    This is CORRECT product behavior."""
    r = resolve_service_from_utterances(
        ["I'd like to book an exam for tomorrow at nine"], _CLINIC,
    )
    # 'exam' matches Emergency exam AND New patient exam → AMBIGUOUS
    # → helper returns None → policy will ASK_SLOT(service) again
    # → agent asks 'what kind of exam?'.  Correct.
    assert r is None


def test_multiple_utterances_first_match_wins():
    """When multiple recent utterances contain services, first
    match wins (newest first)."""
    r = resolve_service_from_utterances(
        [
            "wait, actually a cleaning",   # newest
            "I'd like a follow-up",         # older
        ],
        _CLINIC,
    )
    # 'cleaning' is newer + resolves cleanly.
    assert r is not None
    assert "cleaning" in r.lower()


# ── negative + defensive ─────────────────────────────────


def test_no_service_mention_returns_none():
    r = resolve_service_from_utterances(
        ["what are your hours?"], _CLINIC,
    )
    assert r is None


def test_empty_utterances_returns_none():
    r = resolve_service_from_utterances([], _CLINIC)
    assert r is None


def test_no_business_returns_none():
    r = resolve_service_from_utterances(["a cleaning"], None)
    assert r is None


def test_business_with_no_services_returns_none():
    class _NoSvc:
        services = []
    r = resolve_service_from_utterances(["a cleaning"], _NoSvc())
    assert r is None


def test_malformed_utterances_never_raises():
    r = resolve_service_from_utterances(
        [None, 42, {}, "a cleaning"],  # type: ignore[list-item]
        _CLINIC,
    )
    # Non-string entries silently skipped; 'a cleaning' resolves.
    assert r is not None


# ── end-to-end: missing_slots respects utterance-derived service ──


def test_missing_slots_drops_service_when_utterance_resolves_it():
    """The full loop: caller said something that resolves; missing
    should NOT list service anymore."""
    known = {"service": "Follow-up visit"}   # populated by fix
    r = compute_missing_slots(
        known_slots=known,
        recent_caller_texts=["I'd like a follow-up"],
    )
    assert "service" not in r
    # phone should still be missing (unless we know it too).
    assert "phone" in r


def test_missing_slots_still_includes_service_when_none_resolved():
    """No utterance matched → service stays missing → policy asks."""
    r = compute_missing_slots(
        known_slots={},
        recent_caller_texts=["I want to book something"],
    )
    assert "service" in r
