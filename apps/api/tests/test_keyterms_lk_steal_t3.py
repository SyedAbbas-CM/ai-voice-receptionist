"""T3 acceptance — tenant-aware STT keyterm boosting.

Regression prevention: on CAa8d209cff78e065909410a7ab76b5873, "Syed Abbas"
transcribed as "Seth Muhammadabas" because the hardcoded _DENTAL_KEYTERMS
had no per-tenant boost. This test file locks in the per-tenant contract
so a future refactor can't silently revert to hardcoded terms.

Scope: unit-tests the compute_keyterms function + the provider signature
change. Live-STT integration is covered by manual test-call verification.
"""
from __future__ import annotations

import pytest

from packages.runtime.keyterms import compute_keyterms
from packages.schemas.business import BusinessProfile, ServiceOffering


# ─── fixtures ──────────────────────────────────────────────────────────────


def _clinic() -> BusinessProfile:
    """Smile Dental — the tenant serving prod calls today."""
    return BusinessProfile(
        id="clinic-main",
        name="Smile Dental Clinic",
        vertical="clinic",
        services=[
            ServiceOffering(name="Follow-up visit", duration_minutes=30),
            ServiceOffering(name="New patient exam", duration_minutes=60),
            ServiceOffering(name="Cleaning", duration_minutes=30),
            ServiceOffering(name="Root canal", duration_minutes=90),
        ],
    )


def _real_estate() -> BusinessProfile:
    """Ribeira Prime — Portuguese real-estate tenant fixture."""
    return BusinessProfile(
        id="ribeira-prime",
        name="Ribeira Prime",
        vertical="real-estate",
        services=[
            ServiceOffering(name="Villa viewing", duration_minutes=60),
            ServiceOffering(name="Studio tour", duration_minutes=30),
        ],
    )


def _empty() -> BusinessProfile:
    """Edge case: tenant with no services + generic vertical."""
    return BusinessProfile(id="minimal", name="Test", vertical="clinic")


# ─── 1. Business name variants ────────────────────────────────────────────


def test_business_name_boosted_verbatim():
    """The bug that caused 'Smile Dental' → 'Seth Muhammadabas' on
    CAa8d209. Full business name MUST be in the keyterm list."""
    terms = compute_keyterms(_clinic())
    assert "Smile Dental Clinic" in terms, (
        "REGRESSION: business name missing from keyterms — this is exactly "
        "the bug that caused 'Smile Dental' to transcribe as 'Seth "
        "Muhammadabas' on call CAa8d209cff78e065909410a7ab76b5873."
    )


def test_business_name_first_word_also_boosted():
    """Callers often say just the first word ('Thanks for calling Smile...').
    Boosting the first word alone catches this pattern too."""
    terms = compute_keyterms(_clinic())
    assert "Smile" in terms


def test_business_name_ordered_first():
    """Higher-priority terms should come first (Deepgram roughly weights
    early terms more when the total list is large)."""
    terms = compute_keyterms(_clinic())
    # Full business name must appear before any vertical fallback term.
    biz_idx = terms.index("Smile Dental Clinic")
    vertical_idx = next(
        (i for i, t in enumerate(terms) if t.lower() in ("implant", "cleaning", "crown")),
        len(terms),
    )
    assert biz_idx < vertical_idx


def test_short_first_word_not_boosted_alone():
    """Skip too-short first words ('A', 'The', 'El') to avoid wasting
    keyterm budget on stopwords."""
    biz = BusinessProfile(id="x", name="A Dental Practice", vertical="clinic")
    terms = compute_keyterms(biz)
    # Full name kept
    assert "A Dental Practice" in terms
    # But standalone "A" dropped
    assert "A" not in terms


# ─── 2. Service names ─────────────────────────────────────────────────────


def test_service_names_verbatim_boosted():
    terms = compute_keyterms(_clinic())
    assert "Follow-up visit" in terms
    assert "New patient exam" in terms
    assert "Root canal" in terms


def test_service_names_tokenized():
    """Callers rarely say full service name. 'follow-up' alone should
    also boost, not just 'Follow-up visit'."""
    terms = compute_keyterms(_clinic())
    # From "Follow-up visit" → tokens "Follow" (5 chars, kept), "up"
    # (2 chars, dropped), "visit" (stopword, dropped by our list)
    assert any(t.lower() == "follow" for t in terms)


def test_service_names_deduped():
    """Same term repeated across services (or between service + vertical
    fallback) should appear ONCE."""
    biz = BusinessProfile(
        id="x", name="Test", vertical="clinic",
        services=[
            ServiceOffering(name="Cleaning"),
            ServiceOffering(name="Cleaning"),  # dupe by user error
            ServiceOffering(name="Deep cleaning"),  # would tokenize to "cleaning" too
        ],
    )
    terms = compute_keyterms(biz)
    # Case-insensitive dedup — "Cleaning" and "cleaning" from vertical
    # fallback should collapse.
    lower = [t.lower() for t in terms]
    assert lower.count("cleaning") == 1


# ─── 3. Vertical fallback ─────────────────────────────────────────────────


def test_clinic_vertical_fallback_present():
    terms_lower = {t.lower() for t in compute_keyterms(_clinic())}
    # A sample of dental/medical vocab that MUST survive.
    for expected in ("implant", "crown", "cavity", "hygienist", "appointment"):
        assert expected in terms_lower, f"missing vertical fallback: {expected}"


def test_real_estate_vertical_uses_correct_fallback():
    terms_lower = {t.lower() for t in compute_keyterms(_real_estate())}
    # Real estate should have viewing/villa NOT dental vocab
    assert "viewing" in terms_lower
    assert "villa" in terms_lower
    assert "implant" not in terms_lower, (
        "cross-vertical leak: real-estate tenant got dental keyterms"
    )


def test_vertical_fallback_can_be_disabled():
    terms = compute_keyterms(_clinic(), include_vertical_fallback=False)
    lower = {t.lower() for t in terms}
    # Business name + services still there
    assert "smile dental clinic" in lower
    assert "follow-up visit" in lower
    # But vertical vocab absent
    assert "implant" not in lower
    assert "hygienist" not in lower


# ─── 4. Extras (staff, campaign terms) ────────────────────────────────────


def test_extras_included():
    terms = compute_keyterms(_clinic(), extras=["Dr. Chen", "Dr. Patel"])
    assert "Dr. Chen" in terms
    assert "Dr. Patel" in terms


def test_extras_deduped_with_business_terms():
    terms = compute_keyterms(_clinic(), extras=["cleaning", "Cleaning"])
    lower = [t.lower() for t in terms]
    assert lower.count("cleaning") == 1


# ─── 5. Defensive edge cases ──────────────────────────────────────────────


def test_none_business_does_not_crash():
    """The bridge might see actor.business=None during a bootstrap
    window; must not crash. Returns whatever it can (empty or vertical
    default depending on how we treat missing vertical)."""
    # Passing None as business — should not raise
    terms = compute_keyterms(None)  # type: ignore[arg-type]
    assert isinstance(terms, list)


def test_empty_business_still_returns_vertical_fallback():
    terms = compute_keyterms(_empty())
    lower = {t.lower() for t in terms}
    # No custom services, but vertical fallback should fire
    assert "appointment" in lower


def test_max_length_terms_dropped():
    """Very long service names ('Full-mouth periodontal deep-scaling
    with root-planing under sedation') should be dropped rather than
    truncated — a partial term is worse than none."""
    biz = BusinessProfile(
        id="x", name="Test", vertical="clinic",
        services=[ServiceOffering(name="A" * 60)],  # 60-char single-token
    )
    terms = compute_keyterms(biz)
    # The long term itself dropped
    assert "A" * 60 not in terms


def test_max_total_terms_capped():
    """Even a tenant with 200 services should not send 200 keyterms —
    dilutes each term's weight. Cap should engage."""
    biz = BusinessProfile(
        id="x", name="Test", vertical="clinic",
        services=[ServiceOffering(name=f"Service{i}") for i in range(200)],
    )
    terms = compute_keyterms(biz)
    assert len(terms) <= 128  # matches _MAX_TERMS_PER_REQUEST


def test_empty_extras_does_not_crash():
    terms = compute_keyterms(_clinic(), extras=[])
    assert isinstance(terms, list)
    assert len(terms) > 0


def test_ordering_deterministic():
    """Same input → same output ordering (needed for cache-key stability
    downstream if anyone caches on the keyterm set)."""
    t1 = compute_keyterms(_clinic())
    t2 = compute_keyterms(_clinic())
    assert t1 == t2


# ─── 6. Provider integration signature ────────────────────────────────────


def test_deepgram_flux_signature_accepts_keyterms():
    """Contract check: the provider MUST accept the keyterms kwarg
    without erroring, even if the connection itself doesn't actually
    execute (we're just poking the signature)."""
    from app.providers.stt.deepgram_flux_stt import DeepgramFluxSTT
    import inspect
    sig = inspect.signature(DeepgramFluxSTT.transcribe_stream)
    assert "keyterms" in sig.parameters, (
        "REGRESSION: deepgram_flux_stt.transcribe_stream lost the keyterms "
        "kwarg. T3 wire is broken."
    )


def test_deepgram_nova3_signature_accepts_keyterms():
    from app.providers.stt.deepgram_stt import DeepgramSTT
    import inspect
    sig = inspect.signature(DeepgramSTT.transcribe_stream)
    assert "keyterms" in sig.parameters


def test_base_signature_accepts_keyterms():
    """Base class contract — every STT provider must at least ACCEPT
    the kwarg, even if it ignores it."""
    from app.providers.base import STTProvider
    import inspect
    sig = inspect.signature(STTProvider.transcribe_stream)
    assert "keyterms" in sig.parameters
