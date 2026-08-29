"""P0.0 acceptance — TenantRuntimeContextResolver cross-tenant isolation.

FULL-CODEBASE-AUDIT-2026-08-26-CHATGPT.md flagged the biggest single
architectural bug: session_manager.py held three process-wide singletons
(BusinessProfile, calendar, sink) all loaded from ONE settings. Even
after P0.1 removes the `"default"` supertenant bypass, tenant B's call
still gets tenant A's persona / calendar / CRM sink because the objects
in memory are shared.

The resolver at packages/runtime/tenant_context.py is the fix. This test
suite is the acceptance gate. If ANY of these fails, B-P0.0 is not shipped —
the runtime is still leaking.

## What each test proves

  1. Different tenant_ids produce different BusinessProfile *objects*
     (not just equal — literally different Python instances). This is
     the direct singleton retirement check.
  2. Each business_profile carries the correct tenant-owned persona,
     FAQs, and services — cross-fire would silently return the wrong
     content, this catches that.
  3. A tenant with no config on disk raises TenantRuntimeContextError,
     never falls back to some other tenant's data.
  4. Cache invalidation works per-tenant AND globally.
  5. Concurrent resolution of two tenants returns two different contexts
     even when the resolves race.
  6. The legacy sentinel (`__legacy__`) explicitly opts into the global
     path; any real tenant_id that lacks config fails LOUD instead of
     silently borrowing legacy.
"""
from __future__ import annotations

import threading
import pytest

from packages.runtime.tenant_context import (
    LEGACY_SENTINEL,
    TenantRuntimeContext,
    TenantRuntimeContextError,
    TenantRuntimeContextResolver,
)


@pytest.fixture(autouse=True)
def _fresh_resolver():
    """Every test gets a fresh singleton — cache from a prior test must
    not affect this one. Cleaned up after."""
    TenantRuntimeContextResolver._reset_for_tests()
    yield
    TenantRuntimeContextResolver._reset_for_tests()


# ─── 1. Different tenants → different profile OBJECTS ───────────────────────


def test_clinic_and_real_estate_return_different_business_profiles():
    """The regression that used to be an audit finding. Two tenants,
    two DIFFERENT BusinessProfile instances. Not "different content" —
    literally not the same Python object."""
    r = TenantRuntimeContextResolver.get()
    clinic = r.resolve("clinic")
    real_estate = r.resolve("real-estate")

    assert clinic.business_profile is not real_estate.business_profile, (
        "P0.0 REGRESSION: two different tenants got the SAME BusinessProfile "
        "instance. The singleton pattern is back. This means every downstream "
        "consumer (brain, tool handler, sink) is shared across tenants — "
        "silent cross-tenant leak. Check session_manager.py for a re-added "
        "process-wide `_business_cache` global."
    )


def test_clinic_persona_is_not_real_estate_persona():
    """Content-level check. Even if the objects were different, this
    catches the case where both tenants ended up loading from the same
    fixture file by accident."""
    r = TenantRuntimeContextResolver.get()
    clinic = r.resolve("clinic")
    real_estate = r.resolve("real-estate")

    clinic_persona = clinic.business_profile.voice_persona or ""
    re_persona = real_estate.business_profile.voice_persona or ""

    assert clinic_persona != re_persona, (
        "P0.0 REGRESSION: two tenants got the SAME voice_persona. Even if the "
        "BusinessProfile objects are distinct, they're carrying the same "
        "content — check the resolver's _resolve_config_path chain."
    )
    # And the identifying words we expect in each
    assert "Sofia" in re_persona or "Ribeira" in re_persona, (
        f"real-estate persona doesn't mention Sofia or Ribeira: {re_persona[:80]!r}"
    )
    # Clinic is either Smile Dental / Alex-in-Plano depending on fixture version
    assert (
        "Smile" in clinic.business_profile.name
        or "Dental" in clinic.business_profile.name
        or "Alex" in clinic_persona
    ), (
        f"clinic profile doesn't look like Smile Dental: "
        f"name={clinic.business_profile.name!r}"
    )


# ─── 2. Non-existent tenant fails LOUD, never falls back ────────────────────


def test_nonexistent_tenant_raises_never_returns_default():
    """The exact behaviour we're paying for. A call for a tenant we don't
    have config for must fail with a clear error — NEVER silently return
    a default/global business. That silent fallback was the original bug."""
    r = TenantRuntimeContextResolver.get()
    with pytest.raises(TenantRuntimeContextError, match="no business config found"):
        r.resolve("acme-corp-not-provisioned")


def test_empty_tenant_id_raises():
    r = TenantRuntimeContextResolver.get()
    with pytest.raises(TenantRuntimeContextError, match="tenant_id is required"):
        r.resolve("")


# ─── 3. Legacy sentinel is the ONLY explicit legacy path ────────────────────


def test_legacy_sentinel_returns_the_global_configured_business():
    """`__legacy__` explicitly opts into the settings.business_profile_path.
    Callers who don't have a real tenant_id yet pass this — it surfaces at
    every call site as migration debt."""
    r = TenantRuntimeContextResolver.get()
    ctx = r.resolve(LEGACY_SENTINEL)
    # Doesn't matter which fixture is set globally; just that it resolves.
    assert isinstance(ctx, TenantRuntimeContext)
    assert ctx.tenant_id == LEGACY_SENTINEL
    assert ctx.business_profile is not None


# ─── 4. Cache invalidation ─────────────────────────────────────────────────


def test_repeated_resolve_returns_cached_object_within_ttl():
    r = TenantRuntimeContextResolver.get()
    ctx1 = r.resolve("clinic")
    ctx2 = r.resolve("clinic")
    # Same object → cache hit
    assert ctx1 is ctx2


def test_invalidate_one_tenant_leaves_others_alone():
    r = TenantRuntimeContextResolver.get()
    ctx_clinic_1 = r.resolve("clinic")
    ctx_re_1 = r.resolve("real-estate")

    # Invalidate ONLY clinic
    r.invalidate("clinic")

    ctx_clinic_2 = r.resolve("clinic")
    ctx_re_2 = r.resolve("real-estate")

    # Clinic got a fresh build
    assert ctx_clinic_1 is not ctx_clinic_2, (
        "invalidate('clinic') didn't drop the clinic cache entry"
    )
    # Real-estate untouched
    assert ctx_re_1 is ctx_re_2, (
        "invalidate('clinic') incorrectly dropped real-estate too — "
        "cache scope leaked"
    )


def test_invalidate_all_drops_everything():
    r = TenantRuntimeContextResolver.get()
    ctx_clinic_1 = r.resolve("clinic")
    ctx_re_1 = r.resolve("real-estate")

    r.invalidate()  # None → drop all

    ctx_clinic_2 = r.resolve("clinic")
    ctx_re_2 = r.resolve("real-estate")

    assert ctx_clinic_1 is not ctx_clinic_2
    assert ctx_re_1 is not ctx_re_2


# ─── 5. Concurrent two-tenant resolution ───────────────────────────────────


def test_concurrent_two_tenant_resolution_stays_isolated():
    """The scenario ChatGPT audit called out: two calls in flight for
    two different tenants. Under the old singleton pattern this would
    return the same objects to both. Under the resolver, each gets its
    own context and neither is corrupted."""
    r = TenantRuntimeContextResolver.get()
    results: dict[str, TenantRuntimeContext] = {}
    errors: list[Exception] = []

    def _resolve(tenant_id: str) -> None:
        try:
            results[tenant_id] = r.resolve(tenant_id)
        except Exception as e:  # pragma: no cover — collected below
            errors.append(e)

    threads = [
        threading.Thread(target=_resolve, args=("clinic",)),
        threading.Thread(target=_resolve, args=("real-estate",)),
        threading.Thread(target=_resolve, args=("clinic",)),
        threading.Thread(target=_resolve, args=("real-estate",)),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)

    assert not errors, f"concurrent resolve raised: {errors}"
    assert set(results.keys()) == {"clinic", "real-estate"}
    clinic = results["clinic"]
    real_estate = results["real-estate"]
    assert clinic.business_profile is not real_estate.business_profile
    assert clinic.business_profile.name != real_estate.business_profile.name


# ─── 6. Cache key includes business_id so the same tenant with different
#       business_ids gets distinct contexts ────────────────────────────────


def test_same_tenant_different_business_ids_distinct_contexts():
    """When a tenant runs multiple businesses (multi-location clinic,
    franchise group), each `business_id` gets its own context even
    though tenant_id is the same. Prevents "wrong location's calendar"
    bugs when the number-mapping table routes both to the same tenant."""
    r = TenantRuntimeContextResolver.get()
    ctx_a = r.resolve("clinic", business_id="plano-main")
    ctx_b = r.resolve("clinic", business_id="frisco-annex")
    # Even though both currently load the same fixture (only one clinic
    # config exists), they must be distinct contexts so a future per-
    # business config change is picked up cleanly.
    assert ctx_a.business_id == "plano-main"
    assert ctx_b.business_id == "frisco-annex"
    assert ctx_a is not ctx_b


# ─── 7. TenantRuntimeContext is safely loggable ────────────────────────────


def test_context_redacted_for_log_omits_adapter_objects():
    """`.redacted_for_log()` returns only value fields — adapter object
    identities never enter a log line. Guards against pydantic/repr
    accidentally emitting api keys or client state."""
    r = TenantRuntimeContextResolver.get()
    ctx = r.resolve("clinic")
    log_shape = ctx.redacted_for_log()
    # No calendar / sink / adapter objects
    assert "calendar" not in log_shape
    assert "sink" not in log_shape
    # Basic identifying info present
    assert log_shape["tenant_id"] == "clinic"
    assert "business_name" in log_shape
    assert "config_version" in log_shape
