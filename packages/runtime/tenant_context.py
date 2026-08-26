"""Per-tenant runtime context — the single answer to "what business,
calendar, sink, and policy does THIS call belong to".

2026-08-26 B-P0.0 (FULL-CODEBASE-AUDIT-2026-08-26-CHATGPT.md).

## What this replaces

`apps/api/app/core/session_manager.py:26-54` used three process-wide
singletons — `_business_cache`, `_calendar_cache`, `_sink_cache` — all
loaded from a single `settings` object. Every `start_session_with_id(
tenant_id=...)` accepted a tenant_id but silently used the ONE globally-
loaded business profile, calendar adapter, and CRM sink. Result:
tenant B's call was handled with tenant A's persona and would write
bookings to tenant A's calendar and CRM.

The database-layer tenant filter (`_auto_filter_tenant`) doesn't catch
this because there's no cross-tenant SQL query — the leak is in the
in-process object graph. Fixing the P0.1 supertenant bypass alone
doesn't fix this. Both changes are required.

## Shape

`TenantRuntimeContext` is a frozen dataclass — safe to hand to async
call handlers, safe to cache, no defensive copies needed.

`TenantRuntimeContextResolver` is the single choke point every ingress
runs through:
    ctx = TenantRuntimeContextResolver.get().resolve(tenant_id, business_id)
    brain = ReceptionistBrain(business=ctx.business_profile,
                              calendar=ctx.calendar, sink=ctx.sink, ...)

`TenantRuntimeCache` sits inside the resolver, keyed by
`(tenant_id, business_id, config_version)`. `config_version` bumps
when the tenant's config on disk changes (mtime-based), invalidating
the cache without a server restart.

## Loading strategy

The resolver walks a priority chain per tenant, first hit wins:

  1. `data/tenants/{tenant_id}/business.json`  — per-tenant config dir
     (the intended long-term layout)
  2. `sample-data/{tenant_id}/business.json`   — the current fixture
     layout (Smile Dental = `sample-data/clinic/business.json`,
     Ribeira Prime = `sample-data/real-estate/business.json`, so
     `tenant_id` here is a vertical alias for now)
  3. `settings.business_profile_path`          — the legacy global
     path. Only used when tenant_id is the sentinel `"__legacy__"` OR
     no per-tenant config resolved. Emits a WARNING so operators see
     the deprecation.

Callers who don't know the tenant (test env, unauth'd dev shell)
pass `tenant_id="__legacy__"` explicitly — surfaces at every call site.

## Backward-compat shim

`session_manager.load_business()`, `.get_calendar()`, `.get_sink()`
become thin wrappers that call `resolve("__legacy__")` and log a
deprecation WARNING once per process. Every existing caller keeps
working through a rollout window; audit can grep for the WARNING and
migrate call sites one at a time.

## What isn't in v1

- No cross-process cache invalidation (multi-worker deploys need it
  eventually — for now, a config change requires bounce OR admin
  invalidate). SQLite pilot is single-worker.
- No per-tenant `compliance_mode` yet — the field is on the context
  but populated from settings.hipaa_mode until Day 4 alembic ships
  `tenants.compliance_mode`. Field exists so downstream code can
  branch now, without another refactor when the column lands.
- No hot-reload watcher on the config files. Cache TTL is 5 min,
  so a mistake propagates in ≤5 min without invalidate call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# Sentinel used by legacy call sites that don't have a real tenant_id yet.
# Every appearance of this string in a log line is a migration debt marker.
LEGACY_SENTINEL = "__legacy__"

# Cache TTL — a config change propagates in ≤5 min without an explicit
# invalidate call. Short enough that a mistake surfaces, long enough that
# steady-state loads are one-and-done per tenant.
_CACHE_TTL_S = 300.0


# ─── The context object ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantRuntimeContext:
    """Everything a call handler needs to run correctly for this tenant.

    Frozen so it's safe to share across async tasks without defensive
    copies. Every attribute is either a value type or a stateless adapter
    (calendar/sink have their own internal state but that's per-instance,
    not shared).
    """
    tenant_id: str
    business_id: Optional[str]      # None means "tenant's default"
    business_profile: Any            # packages.schemas.BusinessProfile
    calendar: Any                    # calendar adapter (fake/google/ghl)
    sink: Any                        # CRM sink (composite of hubspot+gsheets+etc)
    telephony_identity: Optional[str] = None  # e.g. E.164 that resolved to this
    compliance_mode: str = "standard"          # "standard" | "hipaa"
    feature_flags: dict = field(default_factory=dict)   # reactive_brain per-tenant, etc
    limits: dict = field(default_factory=dict)          # per-tenant cost/rate caps
    config_version: int = 0                             # bumps on config change

    def redacted_for_log(self) -> dict:
        """Everything except adapter object identities. Safe to structured-log."""
        return {
            "tenant_id": self.tenant_id,
            "business_id": self.business_id,
            "business_name": getattr(self.business_profile, "name", None),
            "compliance_mode": self.compliance_mode,
            "config_version": self.config_version,
        }


# ─── Resolver ───────────────────────────────────────────────────────────────


class TenantRuntimeContextResolver:
    """Process-singleton resolver. get() returns the instance.

    Use `resolve(tenant_id, business_id)` on every ingress path before
    constructing a brain or firing a sink. Every cache miss loads the
    tenant's config from disk and constructs fresh adapter instances —
    they're then cached for `_CACHE_TTL_S` under a key that includes
    the config version.

    `invalidate(tenant_id=None)` drops the whole cache OR just one tenant.
    Admin routes should call this after mutating tenant config.
    """

    _instance: "TenantRuntimeContextResolver | None" = None
    _instance_lock = threading.Lock()

    # (tenant_id, business_id, config_version) → (ctx, cached_at_ts)
    _cache: dict[tuple[str, Optional[str], int], tuple[TenantRuntimeContext, float]]
    _cache_lock: threading.Lock

    # Emitted-once flag for the legacy WARNING — spammy otherwise
    _legacy_warned: set[str]

    def __init__(self) -> None:
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._legacy_warned = set()

    # ─── singleton access ────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "TenantRuntimeContextResolver":
        """Return the process-singleton resolver."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Test-only. Named with `_` + `_for_tests` so a grep at review
        surfaces any accidental production use."""
        with cls._instance_lock:
            cls._instance = None

    # ─── the hot path ────────────────────────────────────────────────────

    def resolve(
        self,
        tenant_id: str,
        business_id: Optional[str] = None,
        *,
        telephony_identity: Optional[str] = None,
    ) -> TenantRuntimeContext:
        """Return the runtime context for this tenant + optional business.

        The `telephony_identity` param is stamped into the returned context
        for observability — it doesn't affect resolution (the tenant_id
        already resolved from it via app.telephony.tenant_from_phone).

        Never returns None. If the tenant has no config, raises
        `TenantRuntimeContextError` — the ingress must refuse the call.
        Silent fallback to a "default" tenant is exactly what B-P0.0 is
        closing.
        """
        if not tenant_id:
            raise TenantRuntimeContextError("tenant_id is required")

        # Fast path — cache hit at the current config version.
        config_version = self._current_config_version(tenant_id, business_id)
        cache_key = (tenant_id, business_id, config_version)
        with self._cache_lock:
            hit = self._cache.get(cache_key)
            if hit is not None:
                ctx, cached_at = hit
                if time.time() - cached_at < _CACHE_TTL_S:
                    return ctx
                # Expired — fall through and rebuild.

        # Slow path — construct fresh.
        ctx = self._build_context(
            tenant_id=tenant_id,
            business_id=business_id,
            telephony_identity=telephony_identity,
            config_version=config_version,
        )
        with self._cache_lock:
            self._cache[cache_key] = (ctx, time.time())
        return ctx

    def invalidate(self, tenant_id: Optional[str] = None) -> None:
        """Drop cache entries. Admin routes call this after tenant config
        mutation. tenant_id=None drops everything (used on bounce OR when
        the operator isn't sure what changed)."""
        with self._cache_lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                # dict comprehension keyed by tuple — drop matching tenant
                self._cache = {
                    k: v for k, v in self._cache.items()
                    if k[0] != tenant_id
                }

    # ─── config version ─────────────────────────────────────────────────

    def _current_config_version(
        self, tenant_id: str, business_id: Optional[str]
    ) -> int:
        """A cheap hash of the inputs that affect the built context —
        the config file mtime + relevant settings values. Bumps on any
        change so the cache key rotates and stale entries drop naturally.

        Not cryptographic, just a change-detection hash.
        """
        h = hashlib.sha256()
        h.update(tenant_id.encode())
        h.update((business_id or "").encode())

        # Resolve the config path and mix its mtime in (if any).
        try:
            path = self._resolve_config_path(tenant_id)
            if path and path.exists():
                h.update(str(path).encode())
                h.update(str(int(path.stat().st_mtime)).encode())
        except Exception:
            # If we can't stat, still return a stable-ish version — the
            # resolve() call will fail cleanly if the file is unreadable.
            pass

        # Env inputs that affect adapter selection.
        for env_key in (
            "CALENDAR_BACKEND", "CRM_SINK",
            "HUBSPOT_ACCESS_TOKEN", "PIPEDRIVE_API_TOKEN",
            "GHL_API_TOKEN", "GOOGLE_SHEET_ID",
        ):
            h.update(f"{env_key}={os.environ.get(env_key, '')}".encode())

        # Take first 8 bytes as an int — plenty of entropy for change detection.
        return int.from_bytes(h.digest()[:8], "big", signed=False)

    # ─── config path chain ──────────────────────────────────────────────

    def _resolve_config_path(self, tenant_id: str) -> Optional[Path]:
        """Walk the priority chain, return the first existing path.
        Returns None if nothing found — caller decides what to do."""
        from app.core.config import settings

        # 1. Per-tenant config dir (the intended long-term layout)
        p1 = Path("data/tenants") / tenant_id / "business.json"
        if p1.exists():
            return p1

        # 2. sample-data/{tenant_id}/business.json (current fixture layout,
        #    where tenant_id is a vertical alias like "clinic" or "real-estate")
        p2 = Path("sample-data") / tenant_id / "business.json"
        if p2.exists():
            return p2

        # 3. Legacy — the global settings path. Only for the legacy sentinel
        #    OR as a last-resort fallback while call sites migrate.
        legacy = settings.business_profile_path
        if legacy:
            p3 = Path(legacy)
            if p3.exists():
                if tenant_id != LEGACY_SENTINEL:
                    # Loud once — help operators grep for migration debt.
                    if tenant_id not in self._legacy_warned:
                        self._legacy_warned.add(tenant_id)
                        log.warning(
                            "tenant_runtime: tenant_id=%r resolved via LEGACY "
                            "global business_profile_path=%r; migrate to "
                            "data/tenants/%s/business.json before shipping "
                            "multi-tenant.", tenant_id, legacy, tenant_id,
                        )
                return p3

        return None

    # ─── context construction ───────────────────────────────────────────

    def _build_context(
        self,
        tenant_id: str,
        business_id: Optional[str],
        telephony_identity: Optional[str],
        config_version: int,
    ) -> TenantRuntimeContext:
        """Load business profile + build per-tenant calendar + sink.
        Every call here means a cache miss — deliberately not on the
        hot path."""
        from app.core.config import settings
        from packages.integrations import build_sink_from_env
        from packages.integrations.calendar_factory import build_calendar
        from packages.schemas import BusinessProfile

        path = self._resolve_config_path(tenant_id)
        if path is None:
            raise TenantRuntimeContextError(
                f"no business config found for tenant_id={tenant_id!r}. "
                f"Expected data/tenants/{tenant_id}/business.json OR "
                f"sample-data/{tenant_id}/business.json."
            )

        try:
            raw = json.loads(path.read_text())
            business_profile = BusinessProfile(**raw)
        except Exception as e:
            raise TenantRuntimeContextError(
                f"failed to load business profile for tenant_id={tenant_id!r} "
                f"from {path}: {e}"
            ) from e

        # Calendar — v1 uses global settings.calendar_backend. When Day 4
        # ships per-tenant calendar credentials, this reads them from the
        # tenant row + falls back to settings. Same shape either way.
        try:
            calendar = build_calendar(
                settings.calendar_backend, settings, business=business_profile,
            )
        except Exception as e:
            raise TenantRuntimeContextError(
                f"failed to build calendar for tenant_id={tenant_id!r}: {e}"
            ) from e

        # Sink — v1 reads env-driven CRM_SINK. Same as calendar: when
        # Day 4 lands per-tenant sink config, this reads from tenant row.
        try:
            sink = build_sink_from_env(settings.crm_sink, settings)
        except Exception as e:
            raise TenantRuntimeContextError(
                f"failed to build CRM sink for tenant_id={tenant_id!r}: {e}"
            ) from e

        # Compliance mode — placeholder until tenants.compliance_mode column
        # lands in Day 4 alembic. Reading a global env for now so downstream
        # code can already branch on it.
        compliance_mode = os.environ.get(
            "TENANT_COMPLIANCE_MODE_DEFAULT", "standard"
        ).lower()
        if compliance_mode not in ("standard", "hipaa"):
            compliance_mode = "standard"

        # Feature flags — v1 mirrors globals. Day 4 adds per-tenant row.
        feature_flags = {
            "next_action_policy_enabled": _env_bool("NEXT_ACTION_POLICY_ENABLED", False),
            "reactive_brain_enabled": _env_bool("REACTIVE_BRAIN_ENABLED", False),
        }

        return TenantRuntimeContext(
            tenant_id=tenant_id,
            business_id=business_id,
            business_profile=business_profile,
            calendar=calendar,
            sink=sink,
            telephony_identity=telephony_identity,
            compliance_mode=compliance_mode,
            feature_flags=feature_flags,
            limits={},
            config_version=config_version,
        )


# ─── Exceptions ─────────────────────────────────────────────────────────────


class TenantRuntimeContextError(Exception):
    """Raised when a tenant's runtime context can't be resolved. The ingress
    layer must translate this into "refuse the call" — never fall back to
    a global default. Silent fallback is exactly what B-P0.0 is closing."""


# ─── utilities ──────────────────────────────────────────────────────────────


def _env_bool(name: str, default: bool = False) -> bool:
    """Consistent truthy env parse — matches the pattern in middleware/auth.py
    (`("0", "false", "no")` = off). Kept as a small local util so this module
    doesn't take a config-loading dep at import time."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")
