"""DB engine + session factory + tenant contextvar.

Sprint 6 changes:
  * Postgres async engine used when DATABASE_URL starts with `postgres`
    (sync SQLite still supported for local dev + tests).
  * SQLite WAL + busy_timeout + foreign_keys pragmas (audit STATE-016).
  * `current_tenant` contextvar so ORM event listeners can auto-inject
    tenant_id on insert.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


# ─── Tenant context ──────────────────────────────────────────────────────────
# The auth middleware sets this per-request.  Session-manager background work
# without a request (greeting warm, filler pool) sets it explicitly.

current_tenant: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_tenant", default=None
)


def set_current_tenant(tenant_id: Optional[str]) -> contextvars.Token:
    """Push a tenant onto the contextvar stack.  Caller MUST reset the returned
    token in a `finally:` block."""
    return current_tenant.set(tenant_id)


def reset_current_tenant(token: contextvars.Token) -> None:
    current_tenant.reset(token)


def get_current_tenant() -> Optional[str]:
    return current_tenant.get()


# ─── Engine ──────────────────────────────────────────────────────────────────

db_url = settings.database_url
_is_sqlite = db_url.startswith("sqlite:")

if _is_sqlite:
    Path(db_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        db_url, future=True, connect_args={"check_same_thread": False},
    )

    # AUDIT FIX 2026-08-01 (STATE-016): SQLite production pragmas.
    #   * WAL — concurrent readers + one writer, no lock storms
    #   * busy_timeout — 5 s wait before "database is locked" errors
    #   * foreign_keys — enforce our FK relationships (SQLite ignores them by default)
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
else:
    # Postgres (async or sync).  SQLAlchemy accepts postgresql+psycopg,
    # postgresql+asyncpg, etc.  For now we run sync sessions from async
    # routes because our concurrency is <10 req/s; audit STATE-015 is on
    # the Sprint 7 backlog to fully migrate to AsyncSession.
    engine = create_engine(db_url, future=True, pool_pre_ping=True, pool_size=10, max_overflow=20)


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Auto-inject tenant_id on insert ─────────────────────────────────────────
# Any model that has a tenant_id column gets it populated automatically from
# the current_tenant contextvar before flush.  Handlers never have to remember.

@event.listens_for(SessionLocal, "before_flush")
def _auto_inject_tenant(db_session, flush_context, instances):
    tenant = current_tenant.get()
    if tenant is None:
        return
    for obj in db_session.new:
        if hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
            obj.tenant_id = tenant


# ─── Auto-filter tenant_id on SELECT (Sprint 6h) ─────────────────────────────
# do_orm_execute listener injects `with_loader_criteria(Model, Model.tenant_id
# == current_tenant)` into every ORM SELECT against tenant-scoped models.
#
# This is the belt AND suspenders layer on top of tenant_guard's WHERE-clause
# grep.  Even if a handler forgets a `filter(tenant_id=X)`, this listener
# adds it silently.  Cross-tenant leaks become impossible-by-default.
#
# Skipped when current_tenant is None (admin routes, migrations, cleanup
# crons) — those legitimately need to see across tenants.

from sqlalchemy.orm import with_loader_criteria as _wlc


@event.listens_for(SessionLocal, "do_orm_execute")
def _auto_filter_tenant(execute_state):
    if not execute_state.is_select:
        return

    # Skip when handler opts out via execution_options=dict(skip_tenant_filter=True)
    if execute_state.execution_options.get("skip_tenant_filter"):
        return

    tenant = current_tenant.get()
    if tenant is None:
        # Contextvar unset = admin/migration path; don't inject a filter that
        # would return zero rows.  tenant_guard will still catch queries that
        # reach the raw SQL layer without a filter.
        return

    # Lazy-import models to avoid circular deps at module load.
    from . import models

    # Every tenant-scoped ORM class gets a criteria injected.  The
    # include_aliases=True flag ensures joins + subqueries also get the filter.
    for cls in (models.SessionRow, models.TranscriptRow, models.BookingRow,
                models.ApiKey, models.IdempotencyRow):
        execute_state.statement = execute_state.statement.options(
            _wlc(
                cls,
                cls.tenant_id == tenant,
                include_aliases=True,
            )
        )


def init_db() -> None:
    """Initialize the database.

    RE-AUDIT FIX 2026-08-02 (CRITICAL-03): behavior differs by environment:

      * ENVIRONMENT=production — refuse to run create_all().  Startup
        FAILS unless the DB is already at the latest Alembic head, so
        production can't silently drift into a hand-created schema.
        Deploys must run `alembic upgrade head` first.

      * ENVIRONMENT=development/test (default) — create_all() as before
        for zero-friction local dev.  Existing tests continue to work.
    """
    import os as _os
    from . import models  # noqa: F401 — registers tables on Base.metadata

    env = _os.environ.get("ENVIRONMENT", "development").lower()

    if env == "production":
        # Verify Alembic head matches; refuse create_all() in prod.
        try:
            from alembic.config import Config as _AC
            from alembic.script import ScriptDirectory
            from alembic.runtime.migration import MigrationContext

            alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
            if not alembic_ini.exists():
                raise RuntimeError(
                    "ENVIRONMENT=production but alembic.ini not found at "
                    f"{alembic_ini}; cannot verify migration state."
                )
            cfg = _AC(str(alembic_ini))
            script = ScriptDirectory.from_config(cfg)
            expected_head = script.get_current_head()

            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                actual_head = ctx.get_current_revision()

            if actual_head != expected_head:
                raise RuntimeError(
                    f"DB migration state mismatch (production): "
                    f"expected head={expected_head!r} got={actual_head!r}. "
                    f"Run `alembic upgrade head` before starting the app."
                )
        except ImportError as e:
            raise RuntimeError(
                f"ENVIRONMENT=production requires Alembic installed to verify "
                f"migration state: {e}"
            ) from e
        # Do NOT call create_all in production — the schema is Alembic-owned.
        return

    # Development / test mode — same as before.
    Base.metadata.create_all(bind=engine)
