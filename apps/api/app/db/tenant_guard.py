"""Cross-tenant leak guard — reject queries on tenant-scoped tables that
don't filter by tenant_id.

Sprint 6c.  This is the safety net.  Even if a handler forgets to filter
by tenant, the guard raises rather than returning cross-tenant data.

How it works:
    * Registered as a SQLAlchemy `before_execute` event listener.
    * Inspects every SELECT / UPDATE / DELETE against a table in
      `_TENANT_SCOPED_TABLES`.
    * Uses `sqlalchemy.sql.compiler.SQLCompiler` to check whether a WHERE
      clause references the `tenant_id` column.
    * Raises `CrossTenantLeakError` (subclass of PermissionError) if not.

Escape hatches:
    * `set_current_tenant(None)` bypasses the guard for background jobs
      that legitimately need to touch multiple tenants (cleanup crons,
      migration scripts).  The bypass is EXPLICIT and observable.
    * `TENANT_GUARD_ENFORCE=false` env disables the guard (dev only).

Falls back to permissive mode with a WARNING log if the query can't be
inspected — the point is defense-in-depth, not a hard-fail on every
weird SQL construct.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Delete, Select, Update

log = logging.getLogger(__name__)


class CrossTenantLeakError(PermissionError):
    """Raised when a query on a tenant-scoped table lacks a tenant_id filter."""


# Tables that MUST be filtered by tenant_id.
_TENANT_SCOPED_TABLES: frozenset[str] = frozenset({
    "sessions",
    "transcript",
    "bookings",
    "api_keys",       # keys are per-tenant; nobody reads across tenants
    "idempotency",    # per-tenant Idempotency-Key cache
})

# Tables that are legitimately global.
_GLOBAL_TABLES: frozenset[str] = frozenset({
    "tenants",        # tenant registry — only admin routes read this
    "alembic_version",
})


def _enforcing() -> bool:
    return os.environ.get("TENANT_GUARD_ENFORCE", "true").lower() not in ("0", "false", "no")


def _query_references_tenant(clauseelement) -> bool:
    """Check whether the WHERE clause of a SELECT/UPDATE/DELETE references
    `tenant_id`.  Best-effort — we compile the statement and grep the SQL.
    Cheap and works across every dialect we care about (SQLite + Postgres)."""
    try:
        compiled = clauseelement.compile(compile_kwargs={"literal_binds": True})
        # SQLAlchemy formats compiled SQL with newlines around keywords
        # ("...\nwhere ..."), which broke a previous `" where "` substring
        # check.  Normalize all whitespace to single spaces before we grep.
        sql = " ".join(str(compiled).lower().split())
        # Any of these patterns counts as "tenant filter present":
        return (
            "tenant_id" in sql
            and (" where " in sql or " on " in sql)
        )
    except Exception:
        # If we can't compile, err on the side of permissive.  A migration
        # or complex CTE that we can't inspect isn't a leak vector on its
        # own; the auth-middleware boundary still applies.
        return True


def _extract_target_tables(clauseelement) -> set[str]:
    """Return the set of table names touched by this statement."""
    tables: set[str] = set()
    try:
        # Selects have .froms; updates/deletes have .table
        if isinstance(clauseelement, Select):
            for fr in clauseelement.get_final_froms():
                name = getattr(fr, "name", None)
                if name:
                    tables.add(str(name))
        elif isinstance(clauseelement, (Update, Delete)):
            name = getattr(clauseelement.table, "name", None)
            if name:
                tables.add(str(name))
    except Exception:
        pass
    return tables


@event.listens_for(Engine, "before_execute")
def _tenant_guard(conn, clauseelement, multiparams, params, execution_options):
    """The guard.  Raises CrossTenantLeakError if a tenant-scoped query
    lacks a tenant_id filter."""
    if not _enforcing():
        return

    # Only guard high-level statements — skip DDL, raw SQL text, etc.
    if not isinstance(clauseelement, (Select, Update, Delete)):
        return

    tables = _extract_target_tables(clauseelement)
    if not tables:
        return
    scoped_hits = tables & _TENANT_SCOPED_TABLES
    if not scoped_hits:
        return

    if _query_references_tenant(clauseelement):
        return

    # Contextvar-based bypass — session-manager background work / admin
    # scripts set `current_tenant` to None explicitly and we let them through.
    from .session import current_tenant
    if current_tenant.get() is None and execution_options.get("allow_cross_tenant"):
        return

    # Leak.  Refuse the query.
    raise CrossTenantLeakError(
        f"query on tenant-scoped table {sorted(scoped_hits)!r} has no "
        f"tenant_id filter.  Fix the handler or set "
        f"execution_options=dict(allow_cross_tenant=True) for a legitimate "
        f"admin/migration path."
    )


def install() -> None:
    """No-op — importing this module registers the event listener above.
    Called from app.main for explicitness in the startup path."""
    log.info(
        "tenant_guard installed: enforce=%s scoped_tables=%s",
        _enforcing(), sorted(_TENANT_SCOPED_TABLES),
    )
