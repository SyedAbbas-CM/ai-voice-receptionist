"""Tests for BUG-CHR-03: service-name alias resolver.

2026-08-29: Christiaan said 'A follow-up' as his service.  Clinic
fixture had NO follow-up service — no rule in prompt for that class
of ambiguous input → LLM returned empty completion → dead air.

Two fixes shipped:
1. sample-data/clinic/business.json: added 'Follow-up visit' and
   'Adult recall exam' as real services.  Follow-up visits are a
   normal billed appointment at every real dental practice.
2. packages/integrations/service_aliases.py: resolver that maps
   caller-spoken variants to canonical tenant service names.  Returns
   MATCH_EXACT / MATCH_FUZZY / AMBIGUOUS / UNKNOWN so prompt has an
   explicit rule for each outcome.
"""
from __future__ import annotations

import pytest

from packages.integrations.service_aliases import (
    ServiceMatchKind,
    resolve_service,
)
from packages.schemas.business import ServiceOffering


def _svc(name, duration=30):
    return ServiceOffering(
        name=name, duration_minutes=duration, description="",
    )


# Realistic tenant service lists.

_CLINIC_SERVICES = [
    _svc("New patient exam with X-rays", 60),
    _svc("Adult cleaning", 45),
    _svc("Composite filling", 45),
    _svc("Zoom whitening", 90),
    _svc("Invisalign consultation", 45),
    _svc("Implant consultation", 60),
    _svc("Emergency exam", 30),
    _svc("Pediatric first visit", 45),
    _svc("Follow-up visit", 30),        # NEW 2026-08-29
    _svc("Adult recall exam", 30),      # NEW 2026-08-29
]

_REAL_ESTATE_SERVICES = [
    _svc("Property viewing", 45),
    _svc("Home valuation", 60),
    _svc("Virtual tour", 30),
    _svc("Rental enquiry", 15),
]


# ── the exact Christiaan trigger ─────────────────────────────────


def test_christiaan_a_follow_up_matches_exact():
    """'A follow-up' → Follow-up visit exactly.  The exact trigger
    that killed his call is now unambiguous."""
    r = resolve_service("A follow-up", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert r.canonical_name == "Follow-up visit"


def test_follow_up_variants_all_match():
    for spoken in ("follow up", "follow-up", "a follow up",
                    "A follow-up", "return visit", "recheck"):
        r = resolve_service(spoken, _CLINIC_SERVICES)
        assert r.kind == ServiceMatchKind.MATCH_EXACT, (
            f"{spoken!r} didn't resolve exact: {r}"
        )
        assert r.canonical_name == "Follow-up visit"


# ── common caller shorthand ──────────────────────────────────────


def test_cleaning_short_form():
    r = resolve_service("cleaning", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "cleaning" in r.canonical_name.lower()


def test_a_cleaning_maps_same():
    r = resolve_service("a cleaning", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT


def test_check_up_maps_to_new_patient_exam_or_recall():
    """'Check-up' is intentionally ambiguous — could be new patient
    OR recall.  Resolver returns AMBIGUOUS so the LLM asks."""
    r = resolve_service("check up", _CLINIC_SERVICES)
    # Ambiguous because both 'New patient exam' AND 'Adult recall exam'
    # have 'exam' as a keyword match.
    assert r.kind == ServiceMatchKind.AMBIGUOUS
    assert len(r.candidates) >= 2


def test_toothache_maps_to_emergency():
    r = resolve_service("toothache", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert r.canonical_name == "Emergency exam"


def test_filling_maps_direct():
    r = resolve_service("filling", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "filling" in r.canonical_name.lower()


def test_cavity_maps_to_filling():
    r = resolve_service("cavity", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "filling" in r.canonical_name.lower()


def test_whitening_maps_to_zoom_whitening():
    r = resolve_service("whitening", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "whitening" in r.canonical_name.lower()


def test_braces_maps_to_invisalign():
    r = resolve_service("braces", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "invisalign" in r.canonical_name.lower()


def test_kids_maps_to_pediatric():
    r = resolve_service("my kids", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert "pediatric" in r.canonical_name.lower()


# ── ambiguous cases ─────────────────────────────────────────────


def test_consultation_ambiguous_when_multiple_consults():
    """'Consultation' matches BOTH Invisalign consult AND Implant
    consult — resolver should return AMBIGUOUS."""
    r = resolve_service("consultation", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.AMBIGUOUS
    candidates = [c.lower() for c in r.candidates]
    assert any("invisalign" in c for c in candidates)
    assert any("implant" in c for c in candidates)


# ── unknown / no-match ──────────────────────────────────────────


def test_unknown_service_returns_unknown():
    """Random word that doesn't map to anything → UNKNOWN, not a
    wrong best-guess."""
    r = resolve_service("purple submarine", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.UNKNOWN


def test_empty_spoken_returns_unknown():
    r = resolve_service("", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.UNKNOWN


def test_empty_services_returns_unknown():
    r = resolve_service("cleaning", [])
    assert r.kind == ServiceMatchKind.UNKNOWN


# ── vertical portability ────────────────────────────────────────


def test_viewing_matches_real_estate_service():
    """Same resolver works on real-estate service list."""
    r = resolve_service("viewing", _REAL_ESTATE_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert r.canonical_name == "Property viewing"


def test_valuation_matches_real_estate():
    r = resolve_service("home valuation", _REAL_ESTATE_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT


def test_follow_up_falls_back_when_no_follow_up_service():
    """Tenant without 'Follow-up' as a fixture service — resolver
    falls through alias keyword to fuzzy match.  Since no service
    contains 'follow-up' or 'follow' at all, result is UNKNOWN so
    the LLM asks the caller for clarification instead of guessing.
    """
    real_estate_only = _REAL_ESTATE_SERVICES  # no dental follow-up
    r = resolve_service("follow-up", real_estate_only)
    # None of the real-estate services contain 'follow-up' keyword,
    # so resolver returns UNKNOWN (not a wrong fuzzy match).
    assert r.kind == ServiceMatchKind.UNKNOWN


# ── never raises ────────────────────────────────────────────────


def test_none_spoken_never_raises():
    r = resolve_service(None, _CLINIC_SERVICES)  # type: ignore[arg-type]
    assert r.kind == ServiceMatchKind.UNKNOWN


def test_none_services_never_raises():
    r = resolve_service("cleaning", None)  # type: ignore[arg-type]
    assert r.kind == ServiceMatchKind.UNKNOWN


def test_malformed_services_never_raises():
    """Services list with non-ServiceOffering entries shouldn't crash."""
    r = resolve_service("cleaning", ["not-a-service", 42, None])  # type: ignore[list-item]
    assert r.kind == ServiceMatchKind.UNKNOWN


# ── confidence scores ──────────────────────────────────────────


def test_exact_match_confidence_is_one():
    r = resolve_service("Adult cleaning", _CLINIC_SERVICES)
    assert r.kind == ServiceMatchKind.MATCH_EXACT
    assert r.confidence == 1.0


def test_alias_match_confidence_high():
    r = resolve_service("cleaning", _CLINIC_SERVICES)
    assert r.confidence >= 0.9


def test_unknown_confidence_low():
    r = resolve_service("purple submarine", _CLINIC_SERVICES)
    assert r.confidence < 0.6
