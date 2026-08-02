# 36. Multi-Tenancy Implementation — voiceops-ai-agent

**Date:** 2026-08-01
**Author:** research + implementation guide
**Status:** design doc, ready to implement
**Repo state at time of writing:**
- SQLAlchemy 2.x sync engine (`apps/api/app/db/session.py`)
- Models: `SessionRow`, `TranscriptRow`, `BookingRow` (no `tenant_id` column yet)
- `ConsentRow` planned, not yet defined
- SQLite default (`data/voiceops.db`); `DATABASE_URL` overrides
- `apps/api/app/middleware/auth.py` sets `request.state.tenant_id` from `{key: tenant_id}` map; `get_tenant_id(request)` dependency exists
- Current tenant boundary is a leaky helper `_tenant_owns_business` in `apps/api/app/routes/sessions.py` that trusts a `business_id` column

Target scale: ~5 concurrent calls today, ~50 tenants in 12 months, headroom to 500 without re-architecture.

---

## TL;DR — the ONE architecture for our stack

**Shared Postgres, shared schema, `tenant_id` column on every row, enforced by Postgres RLS (`SET LOCAL app.tenant_id` per request), auto-injected by a SQLAlchemy `before_flush` event listener, auto-filtered by a `do_orm_execute` listener that appends `with_loader_criteria` on every SELECT.**

- **Storage model:** shared-DB row-level. Schema-per-tenant is the wrong pick until we sell a HIPAA-covered clinic that demands physical separation; database-per-tenant only if a single 6-figure enterprise contract requires it. Both cost 2-3x more CPU/RAM per tenant and multiply the migration blast radius by N. ([dev.to 2026](https://dev.to/young_gao/multi-tenant-architecture-database-per-tenant-vs-shared-schema-1n2e), [PlanetScale 2026](https://planetscale.com/blog/approaches-to-tenancy-in-postgres), [ClickHouse SaaS-on-Postgres 2026](https://clickhouse.com/resources/engineering/multi-tenant-saas-postgres-architecture))
- **Belt and suspenders:** app-level filter (event listener) + database-level RLS. Belt catches missing filters in dev; suspenders catch the belt failing in prod. Two independent enforcement paths — this is what Stripe, Notion, and every serious B2B SaaS ships. ([Crunchy Data 2025](https://www.crunchydata.com/blog/row-level-security-for-tenants-in-postgres), [AWS RLS pattern](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/))
- **Database:** Neon serverless Postgres. `$0.106/CU-hour`, scale-to-zero, branching for CI, RLS fully supported, pg_vector shipped. Cheaper than Supabase at our tenant count and 3-6x cheaper than Fly.io Managed Postgres at the same compute. ([Neon pricing 2026](https://neon.com/pricing), [Supabase pricing 2026](https://makerkit.dev/blog/saas/supabase-pricing), [Fly.io Managed Postgres pricing 2026](https://kuberns.com/blogs/flyio-pricing/))
- **Driver:** `asyncpg` behind SQLAlchemy 2.x async. 2-3x faster than psycopg3 in micro-benchmarks and the SQLAlchemy async default. ([blog.rajpoot.dev 2026](https://blog.rajpoot.dev/posts/python/python-async-database-drivers-2026/), [Gold Lapel asyncpg vs psycopg3 2026](https://goldlapel.com/grounds/django-python/asyncpg-vs-psycopg3-fastapi))
- **Migrations:** stay on Alembic. Atlas is compelling but Alembic is native to SQLAlchemy, we have zero Go infra, and Atlas's diff-based flow is worse for the expand/backfill/contract we need here. ([Atlas vs Alembic 2026 comparison](https://atlasgo.io/guides/atlas-vs-alembic))
- **Idempotency:** ship Brandur's Stripe-style Postgres table (`idempotency_keys` scoped by `(tenant_id, idempotency_key)`) as a FastAPI decorator on `POST /bookings`. Webhook dedup uses a separate `webhook_events` table keyed on `(provider, event_id)` — Twilio `CallSid`, Vapi `call.id`. ([Brandur 2017, still the reference](https://brandur.org/idempotency-keys), [Twilio at-least-once delivery](https://www.twilio.com/docs/segment/guides/duplicate-data))
- **Testing:** transactional-rollback pytest fixture, one Postgres testcontainer per session, a `tenant()` factory fixture, plus a pytest plugin that fails any test whose `SELECT` executed without RLS context set (raw `psql` connection reads `pg_stat_statements`).
- **SQLite:** drop for prod immediately; keep only for CLI utilities that don't touch tenant data. `check_same_thread=False` + tenant RLS + Alembic batch mode = pain we don't need.

---

## 1. Ranked comparison: shared-DB row-level vs schema-per-tenant vs DB-per-tenant

| Dimension                        | Shared DB + RLS                              | Schema-per-tenant                             | DB-per-tenant                                    |
|----------------------------------|----------------------------------------------|-----------------------------------------------|--------------------------------------------------|
| Operational cost @ 50 tenants    | 1 DB, 1 migration run                        | 1 DB, N schemas, N migration runs             | N DBs, N migration runs, N monitoring targets    |
| Marginal cost per tenant         | ~0                                           | small (schema + role)                         | full DB (Neon min $0 idle / Supabase $10/proj)   |
| Migration blast radius           | 1 DB                                         | N schemas (fan-out or race)                   | N DBs (fan-out orchestration required)           |
| Cross-tenant analytics           | trivial `GROUP BY tenant_id`                 | painful (`UNION ALL` per schema)              | needs a warehouse pipeline                       |
| Noisy-neighbor risk              | real; needs pgBouncer + query timeouts       | shared connections still                      | isolated                                         |
| Physical isolation for HIPAA/SOC2 BAA-lite | not physical                        | not physical                                  | yes (per-BAA)                                    |
| CPU/RAM per tenant (benchmark)   | baseline                                     | +30-50%                                       | +200% RAM, +300% CPU                             |
| Postgres system-catalog pressure | none                                         | linear in tenants — starts hurting > 500 schemas | none per-DB, but pool count balloons          |
| Break point                      | 5,000-10,000 tenants (Citus / partitioning)  | 500-1,000 schemas (pg_catalog thrash)         | 100-300 DBs (per-DB pool overhead)               |
| When to pick                     | **default for B2B SaaS 2026**                | mid-market with per-tenant Postgres extensions | enterprise contract mandates BAA-per-tenant     |

Sources: [dev.to 2026 benchmark](https://dev.to/young_gao/multi-tenant-architecture-database-per-tenant-vs-shared-schema-1n2e) (99% isolation efficiency at 300% CPU cost for DB-per-tenant), [PlanetScale 2026](https://planetscale.com/blog/approaches-to-tenancy-in-postgres), [ClickHouse SaaS-on-Postgres 2026](https://clickhouse.com/resources/engineering/multi-tenant-saas-postgres-architecture), [Ajit Singh B2B isolation guide 2026](https://singhajit.com/multi-tenant-database-isolation/), [Ali Asghar RLS-vs-schema-vs-DB 2026](https://aliasghar.me/blog/multi-tenant-saas-data-isolation), [Aditya Agrawal — Building SaaS with Postgres 2026](https://www.adiagr.com/blog/07-saas-postgres-multitenancy-patterns/), [dasroot patterns comparison 2026](https://dasroot.net/posts/2026/01/multi-tenancy-database-patterns-schema-database-row-level/).

### What actually breaks past 500 tenants

- **Schema-per-tenant** hits pg_catalog scaling: each schema creates rows in `pg_class`, `pg_attribute`, `pg_index`; the planner slows and `psql \d` becomes unusable. Postgres bloat guides consistently flag 500-1000 schemas as the informal ceiling. ([Ricardo Fritzsche RLS deep dive](https://ricofritzsche.me/mastering-postgresql-row-level-security-rls-for-rock-solid-multi-tenancy/))
- **DB-per-tenant** hits connection-pool fan-out: each DB needs its own pgBouncer pool (or a shared pool with per-DB accounting); Neon and Supabase both cap projects per org (Supabase Pro is $10/project — 500 tenants = $5000/mo before compute). ([Supabase pricing sprawl warning 2026](https://makerkit.dev/blog/saas/supabase-pricing))
- **Shared DB + RLS** hits `tenant_id` cardinality on indexes — mitigated by (a) partitioning by `tenant_id` on the largest tables (`transcripts` for us) at ~5000 tenants, (b) BRIN indexes on time-series columns, or (c) moving to Citus for logical sharding. ([Crunchy Data on RLS scale 2025](https://www.crunchydata.com/blog/row-level-security-for-tenants-in-postgres))

### Recent post-mortem-adjacent evidence

- **CVE-2024-10976** — Postgres RLS policies below subqueries could disregard user-id changes; patched in 16.5 / 17.1. If we're on Neon (which stays current), we inherit the fix. Lesson: pin `USING (tenant_id = current_setting(...))` at the outermost level; don't nest inside subqueries. ([InstaTunnel RLS failures blog](https://medium.com/@instatunnel/multi-tenant-leakage-when-row-level-security-fails-in-saas-da25f40c788c))
- **CVE-2025-8713** — optimizer statistics leaked sampled RLS-protected rows via `EXPLAIN`. Mitigation: revoke `SELECT` on `pg_statistic` from tenant roles. ([InstaTunnel](https://instatunnel.my/blog/multi-tenant-leakage-when-row-level-security-fails-in-saas))
- **General 2024-25 pattern** — the two dominant cross-tenant breach classes are (a) app forgot the `WHERE tenant_id=?` (Notion-style filter bugs), and (b) connection-pool checkout leaked a previous `SET` (per Kuldeep Pisda's fastapi-rls postmortem). The belt+suspenders architecture below defends both. ([AppOmni 2024 SaaS breach analysis](https://appomni.com/blog/saas-security-predictions-2025/), [Valence Security 2024 lessons](https://www.valencesecurity.com/resources/blogs/2024-saas-security-breaches-lessons-learned), [Bugstrix cross-tenant testing playbook](https://bugstrix.com/blogs/multi-tenant-saas-security-testing-how-to-prevent-cross-tenant-data-leaks/))
- No public **Neon / Supabase / Turso / Nile** multi-tenant-scaling post-mortems as of Aug 2026 — the closest is Neon's Databricks-acquisition pricing shift Dec 2025 (storage $1.75 → $0.35/GB-month, compute -15-25%) which materially favors the shared-DB pick for us. ([Neon 2026 review](https://checkthat.ai/brands/neon/pricing))

---

## 2. SQLAlchemy 2.x auto-inject tenant_id pattern (copy-pasteable)

### 2.1. Design

Three enforcement layers, ordered from cheapest to most expensive:

1. **`ContextVar` holds the current `tenant_id`** — set by FastAPI dependency, read by every listener.
2. **`before_flush`** event listener — auto-stamps `tenant_id` on new `TenantScoped` rows so we never write "tenant-orphan" rows.
3. **`do_orm_execute`** event listener — auto-appends `with_loader_criteria(TenantScoped, TenantScoped.tenant_id == current_tenant())` to every SELECT/UPDATE/DELETE.
4. **Postgres RLS as backstop** — `SET LOCAL app.tenant_id = :tid` per request, `CREATE POLICY ... USING (tenant_id = current_setting('app.tenant_id')::text)`. Even if the ORM bypasses (raw SQL, script, migration), the DB refuses to return other tenants' rows.

Sources: [SQLAlchemy `with_loader_criteria` 2.0 docs](https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html), [SQLAlchemy `SessionEvents.do_orm_execute` docs](https://docs.sqlalchemy.org/en/20/orm/session_events.html?highlight=object+lifecycle+events), [Telemaco019/sqlalchemy-tenants library 2026](https://github.com/Telemaco019/sqlalchemy-tenants), [Kuldeep Pisda fastapi-rls 2026](https://kdpisda.in/fastapi-rls-postgres-row-level-security-fastapi/), [Adriano Vieira RLS+SQLAlchemy+Alembic guide](https://www.adrianovieira.eng.br/en/posts/architecture/row-level-security-sqlachemy-alembic-guide/), [Rico Fritzsche mastering RLS 2026](https://ricofritzsche.me/mastering-postgresql-row-level-security-rls-for-rock-solid-multi-tenancy/).

### 2.2. Complete implementation

**`apps/api/app/db/tenant.py`** (new file):

```python
"""Tenant context + SQLAlchemy enforcement layer.

Three enforcement paths, all wired here:
  1. ContextVar `current_tenant_id` — the source of truth per request.
  2. `before_flush` — stamps new TenantScoped rows with tenant_id.
  3. `do_orm_execute` — filters every SELECT/UPDATE/DELETE by tenant_id.

Postgres RLS is a separate backstop wired in session.py's connect handler.

DO NOT bypass this by using `session.execute(text(...))` on a TenantScoped
table without explicitly including `tenant_id` in the WHERE clause. RLS will
still refuse the query but the code review will flag it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

# ---------------------------------------------------------------------------
# Request-scoped tenant context
# ---------------------------------------------------------------------------
_current_tenant: ContextVar[Optional[str]] = ContextVar(
    "current_tenant_id", default=None
)

# Sentinel tenant used by system tasks (migrations, admin scripts). Requires
# explicit opt-in via `with system_tenant():` — never set by request path.
SYSTEM_TENANT = "__system__"


def current_tenant() -> str:
    tid = _current_tenant.get()
    if tid is None:
        raise RuntimeError(
            "no tenant in context — request path must set current_tenant "
            "before touching the ORM"
        )
    return tid


def set_current_tenant(tenant_id: str) -> object:
    """Set the tenant for this async task / thread. Returns a Token to reset."""
    return _current_tenant.set(tenant_id)


def reset_current_tenant(token: object) -> None:
    _current_tenant.reset(token)


@contextmanager
def system_tenant() -> Iterator[None]:
    """Escape hatch for background jobs / migrations.

    RLS is still enforced at the DB layer — this only silences the ORM
    listeners. If you use this, you MUST write your own tenant_id filter.
    """
    tok = _current_tenant.set(SYSTEM_TENANT)
    try:
        yield
    finally:
        _current_tenant.reset(tok)


# ---------------------------------------------------------------------------
# TenantScoped mixin — every tenant table inherits this
# ---------------------------------------------------------------------------
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy import Index, String


class TenantScoped:
    """Mixin every tenant-owned model MUST inherit.

    Adds `tenant_id TEXT NOT NULL` + a composite index on (tenant_id, id).
    All auto-filtering and RLS policies target rows of this type.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(String(64), nullable=False, index=True)

    @declared_attr
    def __table_args__(cls):
        return (
            Index(f"ix_{cls.__tablename__}_tenant_id", "tenant_id"),
        )


# ---------------------------------------------------------------------------
# Listener 1: stamp tenant_id on new rows before flush
# ---------------------------------------------------------------------------
def _install_before_flush(session_cls) -> None:
    @event.listens_for(session_cls, "before_flush")
    def _before_flush(session: Session, flush_context, instances) -> None:
        tid = _current_tenant.get()
        if tid is None or tid == SYSTEM_TENANT:
            return
        for obj in session.new:
            if isinstance(obj, TenantScoped):
                current = getattr(obj, "tenant_id", None)
                if current is None:
                    obj.tenant_id = tid
                elif current != tid:
                    # explicit cross-tenant write attempt — fail hard
                    raise PermissionError(
                        f"cross-tenant write blocked: object tenant_id="
                        f"{current!r} but request tenant={tid!r}"
                    )


# ---------------------------------------------------------------------------
# Listener 2: filter every ORM query by tenant_id
# ---------------------------------------------------------------------------
def _install_do_orm_execute(session_cls) -> None:
    @event.listens_for(session_cls, "do_orm_execute")
    def _do_orm_execute(execute_state) -> None:
        tid = _current_tenant.get()
        if tid is None or tid == SYSTEM_TENANT:
            return
        # Skip if caller opted out (bulk internal queries, admin panels)
        if execute_state.execution_options.get("skip_tenant_filter"):
            return
        # include_aliases=True makes the filter apply to relationship loads too
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )


def install_tenant_listeners(session_cls) -> None:
    """Wire both listeners onto the given Session class. Call once at startup."""
    _install_before_flush(session_cls)
    _install_do_orm_execute(session_cls)
```

**`apps/api/app/db/session.py`** (replace):

```python
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.db.tenant import (
    _current_tenant,
    SYSTEM_TENANT,
    install_tenant_listeners,
)


class Base(DeclarativeBase):
    pass


db_url = settings.database_url
is_sqlite = db_url.startswith("sqlite")
if is_sqlite:
    Path(db_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    db_url,
    future=True,
    connect_args={"check_same_thread": False} if is_sqlite else {},
    pool_pre_ping=True,
    pool_size=10 if not is_sqlite else 5,
    max_overflow=20 if not is_sqlite else 0,
)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, future=True
)

# Install the two ORM-level listeners (before_flush + do_orm_execute)
install_tenant_listeners(SessionLocal)


# ---------------------------------------------------------------------------
# Postgres RLS: set app.tenant_id at the start of every transaction.
# SET LOCAL is scoped to the transaction, so we never leak across pool checkouts.
# Skipped for SQLite (RLS is Postgres-only).
# ---------------------------------------------------------------------------
if not is_sqlite:
    @event.listens_for(engine, "begin")
    def _rls_set_tenant(conn):
        tid = _current_tenant.get()
        if tid is None or tid == SYSTEM_TENANT:
            # No tenant → no rows will match any RLS policy. Prevents cold
            # queries from ever seeing anything without context.
            conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            return
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": tid},
        )


def get_session() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
```

**Why `set_config(name, value, true)` instead of `SET LOCAL`:**

- `SET` cannot take bind parameters — you'd have to string-interpolate `tenant_id` into SQL and open an injection hole. `set_config()` is a function call and accepts binds. ([pgedge on session variables](https://www.pgedge.com/blog/it-depends-using-session-variables-in-postgres), [SQLAlchemy discussion #13020](https://github.com/sqlalchemy/sqlalchemy/discussions/13020))
- The third arg `true` = "is_local" — scoped to the current transaction, auto-cleared on COMMIT/ROLLBACK. This is the single most important safety property; forgetting it leaks tenant identity across pool checkouts (per Kuldeep Pisda's FastAPI-rls postmortem). ([fastapi-rls guide](https://kdpisda.in/fastapi-rls-postgres-row-level-security-fastapi/))

**`apps/api/app/middleware/auth.py`** (add — extends existing middleware):

```python
# after existing code that sets request.state.tenant_id:
from app.db.tenant import set_current_tenant, reset_current_tenant

# in dispatch() after resolving tenant_id:
token = set_current_tenant(tenant)
try:
    response = await call_next(request)
finally:
    reset_current_tenant(token)
return response
```

**`apps/api/app/routes/sessions.py`** (delete `_tenant_owns_business`, becomes):

```python
# No more manual filters — the ORM listener handles it.
@router.get("/sessions/{session_id}")
def get_session_endpoint(session_id: str, db: Session = Depends(get_session)):
    row = db.get(SessionRow, session_id)
    if row is None:
        raise HTTPException(404, "not found")  # 404, not 403 — don't leak existence
    return row
```

Note the 404-vs-403 point: `with_loader_criteria` makes cross-tenant reads return `None`, so we get a "not found" for free. Returning 403 would leak the fact that the ID exists for a different tenant. ([InstaTunnel cross-tenant enumeration writeup](https://instatunnel.my/blog/multi-tenant-leakage-when-row-level-security-fails-in-saas))

---

## 3. Alembic migration playbook (expand → backfill → contract)

### 3.1. Why three migrations, not one

The **expand/backfill/contract** pattern is the 2026 industry standard for zero-downtime column additions to populated tables. Each phase deploys separately so old and new app code can run simultaneously during rollout. ([that.guru zero-downtime Alembic 2025](https://that.guru/blog/zero-downtime-upgrades-with-alembic-and-sqlalchemy/), [Alembic Cookbook 1.18](https://alembic.sqlalchemy.org/en/latest/cookbook.html), [OneUptime Alembic guide 2025](https://oneuptime.com/blog/post/2025-07-02-python-alembic-migrations/view))

For our specific case with SQLite as the current dev DB, `op.batch_alter_table()` is required because SQLite can't `ALTER COLUMN`. Alembic emulates it via move-and-copy: reflect → create new table → copy data → drop old → rename. ([Alembic Batch docs](https://alembic.sqlalchemy.org/en/latest/batch.html))

### 3.2. Migration 1 — expand: add nullable `tenant_id` + backfill

`apps/api/alembic/versions/0002_add_tenant_id_nullable.py`:

```python
"""Add tenant_id (nullable) + backfill from business_id.

Revision ID: 0002_add_tenant_id_nullable
Revises: 0001_initial
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_add_tenant_id_nullable"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TENANT_TABLES = ("sessions", "bookings", "transcript", "consents")


def upgrade() -> None:
    for tbl in TENANT_TABLES:
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.add_column(sa.Column("tenant_id", sa.String(64), nullable=True))
            batch_op.create_index(f"ix_{tbl}_tenant_id", ["tenant_id"])

    # Backfill: existing rows have business_id → derive tenant from it.
    # For rows without business_id (transcripts), inherit from parent session.
    op.execute("""
        UPDATE sessions SET tenant_id = COALESCE(business_id, 'default')
        WHERE tenant_id IS NULL
    """)
    op.execute("""
        UPDATE bookings SET tenant_id = COALESCE(business_id, 'default')
        WHERE tenant_id IS NULL
    """)
    op.execute("""
        UPDATE transcript
        SET tenant_id = (
            SELECT s.tenant_id FROM sessions s WHERE s.id = transcript.session_id
        )
        WHERE tenant_id IS NULL
    """)
    op.execute("""
        UPDATE consents SET tenant_id = COALESCE(business_id, 'default')
        WHERE tenant_id IS NULL
    """)


def downgrade() -> None:
    for tbl in TENANT_TABLES:
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.drop_index(f"ix_{tbl}_tenant_id")
            batch_op.drop_column("tenant_id")
```

**Deploy checkpoint after 0002:** app must know to *write* `tenant_id` (event listener enabled) but still read without filtering. Verify `SELECT count(*) FROM sessions WHERE tenant_id IS NULL` returns 0 in every environment before proceeding.

### 3.3. Migration 2 — contract: `NOT NULL` + composite unique + FK

`apps/api/alembic/versions/0003_tenant_id_not_null.py`:

```python
"""Enforce NOT NULL + composite (tenant_id, id) uniqueness on tenant tables."""
from alembic import op
import sqlalchemy as sa

revision = "0003_tenant_id_not_null"
down_revision = "0002_add_tenant_id_nullable"

TENANT_TABLES = ("sessions", "bookings", "transcript", "consents")


def upgrade() -> None:
    # Safety guard — the deploy will fail loudly if anything skipped 0002.
    for tbl in TENANT_TABLES:
        row = op.get_bind().execute(
            sa.text(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id IS NULL")
        ).scalar()
        if row:
            raise RuntimeError(
                f"{tbl} still has {row} rows with NULL tenant_id — "
                "did 0002 backfill run? Re-run before continuing."
            )

    for tbl in TENANT_TABLES:
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.alter_column("tenant_id", nullable=False)

    # Composite uniqueness ensures a business_id (or any natural key) is
    # unique WITHIN a tenant, not globally. This is the schema-level defense
    # against tenant-A creating a booking with the same UUID as tenant-B.
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.create_unique_constraint(
            "uq_bookings_tenant_business", ["tenant_id", "business_id", "id"]
        )


def downgrade() -> None:
    for tbl in TENANT_TABLES:
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.alter_column("tenant_id", nullable=True)
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.drop_constraint("uq_bookings_tenant_business", type_="unique")
```

### 3.4. Migration 3 — Postgres RLS policies (Postgres only, guarded)

`apps/api/alembic/versions/0004_enable_rls.py`:

```python
"""Enable Postgres RLS + create tenant_isolation policies.

Skipped on SQLite. Requires Postgres 15+ (for `FORCE ROW LEVEL SECURITY`).
"""
from alembic import op

revision = "0004_enable_rls"
down_revision = "0003_tenant_id_not_null"

TENANT_TABLES = ("sessions", "bookings", "transcript", "consents")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for tbl in TENANT_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")  # applies to owner too
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {tbl}
              USING (tenant_id = current_setting('app.tenant_id', true))
              WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for tbl in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
```

**Two RLS gotchas — do not skip:**

1. `FORCE ROW LEVEL SECURITY` — without this, the table *owner* (which is usually the app's DB role) bypasses RLS. Bug from InstaTunnel writeup: teams enable RLS but forget FORCE, and their app connects as the owner. ([InstaTunnel 2025 RLS failures](https://medium.com/@instatunnel/multi-tenant-leakage-when-row-level-security-fails-in-saas-da25f40c788c))
2. `current_setting('app.tenant_id', true)` — the `true` = "missing_ok"; returns NULL instead of erroring if unset. Combined with `tenant_id = NULL` returning UNKNOWN (not false), this means a query with no tenant context returns zero rows. Good default. ([Crunchy Data 2025](https://www.crunchydata.com/blog/row-level-security-for-tenants-in-postgres))

### 3.5. Zero-downtime deploy checklist

For each of the three migrations:

1. Deploy code compatible with both old and new schema
2. Run migration in maintenance window (or use `pg_repack` for the batch-copy on very large tables)
3. Wait for replica lag to clear (< 1s)
4. Verify: `SELECT COUNT(*) WHERE tenant_id IS NULL = 0` (after backfill)
5. Deploy code that requires the new column
6. Only then deploy the next migration

For our current scale (< 10 MB DB, ~5 concurrent calls) this is overkill — we can run all three migrations back-to-back in a single deploy window with < 30s downtime. But adopt the pattern now while volume is small; it's much cheaper than retrofitting.

### 3.6. Alembic vs Atlas / dbmate / sqlx (2026 landscape)

| Tool     | Language     | Migration style      | Verdict for us                                    |
|----------|--------------|----------------------|---------------------------------------------------|
| Alembic  | Python       | imperative (upgrade/downgrade)   | **stay here** — native to SQLAlchemy      |
| Atlas    | Go binary    | declarative + versioned        | overkill; wins on multi-DB, safety analyzer, but adds Go tooling |
| dbmate   | Go binary    | raw .sql up/down     | attractive if we drop the ORM; we won't          |
| sqlx     | Rust         | Rust-only            | N/A for Python codebase                          |
| Prisma   | Node         | declarative schema.prisma | N/A                                         |

Atlas's semantic safety analyzer (would-flag: "adding NOT NULL to populated column is dangerous") is a real feature Alembic lacks — we can adopt Atlas *alongside* Alembic later just for CI safety linting if we outgrow manual review. ([Atlas vs Alembic 2026](https://atlasgo.io/guides/atlas-vs-alembic), [Toolradar migration tools ranked 2026](https://toolradar.com/blog/best-database-migration-tools))

---

## 4. SQLite → Postgres migration path

### 4.1. Async everywhere, no dual-DB in prod

Recommendation: **drop SQLite from production immediately. Keep it only for the CLI/simulator apps that don't touch tenant tables.** Reasons:

- SQLite has no RLS. Belt+suspenders architecture becomes belt-only.
- SQLite requires `batch_alter_table` for every schema change — extra migration complexity forever.
- `check_same_thread=False` + our new event listeners = subtle concurrency bugs waiting to happen.
- Neon free tier ($0/mo up to 0.5 GB) covers a dev-per-developer scenario without SQLite.

### 4.2. Driver pick: asyncpg

| Driver     | Speed vs baseline | API                          | SQLAlchemy async default | Verdict            |
|------------|-------------------|------------------------------|--------------------------|--------------------|
| asyncpg    | 2-3x faster       | native async, binary protocol| yes                      | **pick this**      |
| psycopg3   | baseline          | shared sync+async surface    | supported                | fallback if we need COPY / server cursors |
| psycopg2   | slower, sync only | legacy                       | n/a                      | remove entirely    |

Sources: [rajpoot.dev 2026 async drivers](https://blog.rajpoot.dev/posts/python/python-async-database-drivers-2026/), [Gold Lapel asyncpg vs psycopg3 2026 evidence](https://goldlapel.com/grounds/django-python/asyncpg-vs-psycopg3-fastapi), [Fernando Arteaga psycopg3 vs asyncpg](https://fernandoarteaga.dev/blog/psycopg-vs-asyncpg/), [dasroot SQLAlchemy vs asyncpg 2026](https://dasroot.net/posts/2026/02/python-postgresql-sqlalchemy-asyncpg-performance-comparison/).

### 4.3. Connection pool sizing (2026 SQLAlchemy async guidance)

For our ~5 concurrent → ~50 concurrent trajectory:

```python
# apps/api/app/db/session.py — async version
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine(
    "postgresql+asyncpg://...",
    pool_size=10,       # persistent connections
    max_overflow=20,    # burst headroom
    pool_pre_ping=True, # detect stale conns (Neon idles them aggressively)
    pool_recycle=1800,  # recycle every 30 min
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
```

Rule of thumb from FastAPI discussion #13732: `pool_size + max_overflow >= expected concurrent DB-bound requests`. At 50 concurrent voice calls, each holding a session for maybe 100 ms across 8-10 flushes, we're looking at peak ~5 open connections. `pool_size=10, max_overflow=20` is generous. ([FastAPI discussion #13732 benchmark](https://github.com/fastapi/fastapi/discussions/13732))

If we deploy behind pgBouncer (Neon includes it), use **transaction-mode pooling** and disable prepared statements: `create_async_engine(url, connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0})`. Session-mode pooling defeats the point of pgBouncer at scale.

### 4.4. Testing pattern

**Prod = async Postgres, tests = async Postgres in testcontainer.** Do NOT test against sync SQLite and deploy async Postgres — the event-listener bugs surface only in the async engine.

```python
# apps/api/tests/conftest.py
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest_asyncio.fixture
async def db_session(postgres_container):
    url = postgres_container.get_connection_url().replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        # Wrap the test in a transaction we roll back at the end
        await s.begin()
        yield s
        await s.rollback()
    await engine.dispose()
```

Sources: [core27.co transactional async SQLAlchemy tests](https://www.core27.co/post/transactional-unit-tests-with-pytest-and-async-sqlalchemy), [pytest-async-sqlalchemy PyPI](https://pypi.org/project/pytest-async-sqlalchemy/), [OneUptime testcontainers guide 2025](https://oneuptime.com/blog/post/2025-01-06-python-testcontainers-integration/view).

---

## 5. Managed Postgres provider recommendation

### 5.1. Comparison matrix (Aug 2026 pricing)

| Provider          | Compute pricing           | Storage           | RLS | Scale-to-zero | Branching | pgBouncer | pgvector |
|-------------------|---------------------------|-------------------|-----|---------------|-----------|-----------|----------|
| **Neon**          | $0.106/CU-hr (Launch), $0.222/CU-hr (Scale) | $0.35/GB-mo | yes | **yes**      | **yes**   | yes (built-in) | yes    |
| Supabase          | $25/mo Pro minimum + $10/extra project      | $0.125/GB-mo | yes | no (Pro)     | yes (branch DB) | yes  | yes    |
| Turso             | $8.25/mo hobby, $29+/mo scaler              | libSQL, per-DB     | no (libSQL, not Postgres) | new users: no | yes (embedded replicas) | n/a | no |
| Railway           | $5-20/mo hobby, $250+/mo pro compute        | included in compute | yes | no          | no        | manual    | yes    |
| Fly.io Managed PG | $38/mo Basic → $1922/mo Performance         | $0.28/GB-mo (Volumes $0.15/GB) | yes | no | no        | manual    | yes    |
| RDS Serverless v2 | $0.12/ACU-hr (min 0.5 ACU = $43/mo) + I/O    | $0.115/GB-mo GP3   | yes | v2.1 yes    | no        | RDS Proxy $ | yes  |
| Nile              | tenant-aware; usage-based                    | included           | native (tenant is a first-class DB concept) | yes | per-tenant | yes | yes |

Sources: [Neon pricing 2026](https://neon.com/pricing), [Neon CU-hr breakdown](https://swyftstack.com/blog/neon-pricing-explained), [Supabase pricing 2026](https://makerkit.dev/blog/saas/supabase-pricing), [Supabase real costs](https://www.metacto.com/blogs/the-true-cost-of-supabase-a-comprehensive-guide-to-pricing-integration-and-maintenance), [Turso vs Neon vs Supabase 2026](https://devtoolpicks.com/blog/turso-vs-neon-vs-supabase-indie-hackers-2026), [Fly.io Managed Postgres 2026](https://kuberns.com/blogs/flyio-pricing/), [Fly.io alternatives 2026](https://expresstech.io/7-fly-io-alternatives-in-2026-real-pricing-after-the-free-tier-died/), [Railway vs Fly.io 2026](https://northflank.com/blog/railway-vs-flyio), [Nile architecture](https://www.thenile.dev/docs/getting-started/architecture), [Northflank Postgres alternatives 2026](https://northflank.com/blog/neon-planetscale-postgres-alternatives).

### 5.2. Cost projection for voiceops-ai-agent

Assumptions per tenant: ~1 GB storage after 12 months, ~0.05 CU-hours/day average compute (idle-heavy voice traffic), ~2 GB egress/mo.

| Tenants | Neon Launch monthly | Supabase Pro monthly | Fly.io Managed PG (Basic pool) | RDS Serverless v2 (shared) |
|---------|---------------------|----------------------|--------------------------------|-----------------------------|
| 10      | ~$5-15              | $25-35 (1 project)   | $38                            | $43 base + I/O              |
| 100     | ~$60-100            | $25-45 (1 project)   | $38-150 (pool grows)           | $80-150 base + I/O          |
| 1000    | ~$400-700           | $200-400             | $400-1000+                     | $300-600                    |

**Neon wins at every scale for our profile** because voice traffic is spiky: tenants are idle 20+ hours/day, and Neon's scale-to-zero means we pay ~$0 for idle. Supabase's flat $25/project floor loses at 10 tenants; Fly.io's $38 Basic minimum plus manual pool sizing hurts at 100+. Nile is interesting for the tenant-native architecture but is still smaller-scale in 2026 — we should re-evaluate at 500 tenants.

### 5.3. Recommendation

**Neon Launch tier, one project (`voiceops-prod`), one branch per env (main + preview branches per PR).** Enable pooled connections, wire our RLS policies, set compute autoscaling min=0.25 CU / max=2 CU.

Rationale beyond price:

- **Branching for CI:** every PR gets a copy-on-write branch that runs the full migration + test suite against real Postgres. This is the killer feature Supabase can't match cheaply. ([Neon 2026 review — makerkit](https://makerkit.dev/blog/tutorials/best-database-software-startups))
- **Databricks acquisition (May 2025):** stable ownership, pricing has trended down not up. No repricing risk in the 12-month horizon. ([tech-insider Neon vs Supabase 2026](https://tech-insider.org/neon-vs-supabase-2026/))
- **Scale-to-zero fits voice-agent load shape exactly.** Tenants are silent 90% of the time; a call spikes CPU for 3-8 minutes.

Migration off if: (a) we sign a HIPAA BAA that requires physical DB isolation (move to Neon Business tier with dedicated compute or drop to RDS with per-tenant DBs), (b) we hit 5000+ tenants and Citus / logical sharding becomes cheaper.

---

## 6. Idempotency layer implementation

### 6.1. Two separate tables — user idempotency ≠ webhook dedup

Do NOT overload one table:

- **`idempotency_keys`** — for client-initiated writes (`POST /bookings`, `POST /tenants`). Client generates a UUID, sends via `Idempotency-Key: <uuid>` header, we cache the response for 24h. Follows Stripe's [documented behavior](https://stripe.com/docs/api/idempotent_requests).
- **`webhook_events`** — for provider callbacks (Twilio call status, Vapi call end, WhatsApp inbound). We control the key: `(provider, provider_event_id)`. TTL 30 days.

### 6.2. `idempotency_keys` table (Brandur-style, tenant-scoped)

```python
# apps/api/app/db/models.py — append

from datetime import timedelta
from sqlalchemy import CheckConstraint, UniqueConstraint

class IdempotencyKeyRow(Base, TenantScoped):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(String(500), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of body
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    recovery_point: Mapped[str] = mapped_column(String(50), default="started", nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.utcnow() + timedelta(hours=24)
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idem_tenant_key"),
        CheckConstraint("recovery_point IN ('started', 'finished')"),
    )
```

Schema follows [Brandur's reference](https://brandur.org/idempotency-keys) with `(tenant_id, idempotency_key)` composite uniqueness so tenants can pick colliding UUIDs. `recovery_point` state machine: `started → finished`. `locked_at` handles the concurrent-retry race.

### 6.3. FastAPI decorator (handler-explicit, opt-in)

```python
# apps/api/app/middleware/idempotency.py
import hashlib, json
from functools import wraps
from typing import Callable

from fastapi import Request, Response, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyKeyRow
from app.db.tenant import current_tenant


def idempotent(func: Callable) -> Callable:
    """Wrap a FastAPI handler with Stripe-style idempotency.

    Client passes `Idempotency-Key: <uuid>` header. We return the cached
    response on retry. Different body with same key → 409 Conflict.
    """
    @wraps(func)
    async def wrapper(request: Request, db: Session, *args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            # No key = not idempotent, just execute
            return await func(request=request, db=db, *args, **kwargs)

        body = await request.body()
        req_hash = hashlib.sha256(body).hexdigest()
        tenant = current_tenant()

        # Try to claim the key. UNIQUE(tenant_id, idempotency_key) does the work.
        row = IdempotencyKeyRow(
            tenant_id=tenant,
            idempotency_key=key,
            request_method=request.method,
            request_path=str(request.url.path),
            request_hash=req_hash,
            locked_at=datetime.utcnow(),
        )
        try:
            db.add(row)
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.query(IdempotencyKeyRow).filter_by(
                idempotency_key=key
            ).one()  # tenant filter auto-applied by RLS + listener
            if existing.request_hash != req_hash:
                raise HTTPException(409, "Idempotency-Key reused with different payload")
            if existing.recovery_point == "finished":
                return Response(
                    content=json.dumps(existing.response_body),
                    status_code=existing.response_status,
                    media_type="application/json",
                )
            # In-flight — client is retrying too fast
            raise HTTPException(409, "Request in progress, retry shortly")

        # First-time execution
        response = await func(request=request, db=db, *args, **kwargs)
        row.response_status = response.status_code
        row.response_body = json.loads(response.body) if response.body else None
        row.recovery_point = "finished"
        db.commit()
        return response
    return wrapper
```

Usage:

```python
@router.post("/bookings")
@idempotent
async def create_booking(...):
    ...
```

**Why decorator, not middleware:**

- Middleware runs for every route → we'd need per-route opt-in flags anyway.
- Decorator makes idempotency visible at the handler definition (grep-able).
- Middleware doesn't have clean access to `db: Session` without extra plumbing.

### 6.4. Webhook dedup — separate table, provider-owned keys

```python
class WebhookEventRow(Base):
    """NOT TenantScoped — webhooks arrive before we know the tenant."""
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # twilio|vapi|elevenlabs|whatsapp
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_provider_event"),
    )
```

Dedup helper:

```python
# apps/api/app/webhooks/dedup.py
def is_duplicate(db: Session, provider: str, event_id: str, payload_hash: str) -> bool:
    row = WebhookEventRow(
        provider=provider,
        provider_event_id=event_id,
        payload_hash=payload_hash,
    )
    try:
        db.add(row)
        db.commit()
        return False
    except IntegrityError:
        db.rollback()
        return True  # already processed
```

Provider event-id sources: Twilio uses `CallSid` (call-level) and `MessageSid` (per SMS/status); Vapi provides `call.id` in every webhook payload; WhatsApp Business uses `messages[].id`. Twilio explicitly warns [at-least-once delivery, dedupe required](https://www.twilio.com/docs/segment/guides/duplicate-data). Vapi's [call handling docs](https://docs.vapi.ai/calls/call-handling-with-vapi-and-twilio) recommend the same pattern.

### 6.5. Library options — reviewed and rejected

- `fastapi-idempotency` (2024) — abandoned, last commit > 12 months old.
- `svix` (webhook infrastructure) — good for *sending* webhooks; overkill for our receive path.
- `standardwebhooks` — spec, not a library; useful for signing, not dedup.

Verdict: 60 lines of code + one Postgres table is the right amount of infrastructure here. Reach for a library when we need retry orchestration and dead-letter queues (that's Svix territory, but we're 6 months from needing it).

---

## 7. Testing strategy

### 7.1. `tenant()` factory fixture

```python
# apps/api/tests/conftest.py
import pytest
from app.db.tenant import set_current_tenant, reset_current_tenant


@pytest.fixture
def as_tenant(db_session):
    """Auto-scope the test to a tenant; restores context on teardown.

    Usage:
        def test_foo(as_tenant, db_session):
            t = as_tenant("acme-corp")
            db_session.add(SessionRow(id="s1", business_id="b1"))
            db_session.commit()  # tenant_id="acme-corp" auto-stamped
    """
    tokens = []
    def _use(tenant_id: str) -> str:
        tokens.append(set_current_tenant(tenant_id))
        return tenant_id
    yield _use
    for t in reversed(tokens):
        reset_current_tenant(t)
```

### 7.2. Cross-tenant leak detection (the important one)

```python
# apps/api/tests/test_tenant_isolation.py
import pytest
from hypothesis import given, strategies as st

from app.db.models import SessionRow, BookingRow, TranscriptRow


TENANT_TABLES = [SessionRow, BookingRow, TranscriptRow]


@given(
    tenants=st.lists(st.text(min_size=3, max_size=20), min_size=2, max_size=5, unique=True),
    rows_per_tenant=st.integers(min_value=1, max_value=10),
)
def test_no_cross_tenant_reads(as_tenant, db_session, tenants, rows_per_tenant):
    # Seed rows for each tenant
    seeded = {}
    for tid in tenants:
        as_tenant(tid)
        ids = []
        for i in range(rows_per_tenant):
            r = SessionRow(id=f"{tid}-{i}", business_id=tid, status="active")
            db_session.add(r)
            ids.append(r.id)
        db_session.commit()
        seeded[tid] = ids

    # For each tenant, verify they see ONLY their rows
    for tid, ids in seeded.items():
        as_tenant(tid)
        visible = {r.id for r in db_session.query(SessionRow).all()}
        assert visible == set(ids), f"tenant {tid} saw {visible} but owned {set(ids)}"


def test_cross_tenant_write_blocked(as_tenant, db_session):
    as_tenant("tenant-a")
    row = SessionRow(id="x", business_id="b", tenant_id="tenant-b", status="active")
    db_session.add(row)
    with pytest.raises(PermissionError, match="cross-tenant write blocked"):
        db_session.flush()


def test_missing_tenant_context_returns_empty(db_session):
    # Do NOT call as_tenant — simulates handler that forgot the dependency
    with pytest.raises(RuntimeError, match="no tenant in context"):
        db_session.query(SessionRow).all()
```

### 7.3. RLS-bypass detection at the DB layer

Ships as a pytest plugin that inspects `pg_stat_statements` after each test and fails if any statement touched a tenant table without an `app.tenant_id` setting. Rough sketch:

```python
@pytest.fixture(autouse=True)
def _assert_rls_context(db_session):
    yield
    # After the test, query pg_stat_statements for our session's PID
    # and verify no query on a TenantScoped table ran with empty app.tenant_id
    # (Practical: log the assertion, don't fail hard in unit tests)
```

### 7.4. 2026 linting tools reviewed

- **sqlfluff** — SQL linter, no built-in tenant rule but supports custom rules. We'd write a rule that flags any raw SQL touching `sessions|bookings|transcript|consents` without `tenant_id` in the WHERE clause.
- **semgrep** — better fit. One YAML rule: `pattern: session.query($TABLE).filter(...)` with `pattern-not: tenant_id ==` catches the same class of bug at review time.
- **mypy custom plugin** — investigated; too much implementation cost for the value at our scale. Revisit at 20+ engineers.

The real test-strategy safety net is the belt+suspenders architecture (listeners + RLS), not the linters.

Sources: [pytest fixtures scope 2026](https://qaskills.sh/blog/pytest-fixtures-scope-complete-guide), [pytest-xdist parallel 2026](https://qaskills.sh/blog/pytest-xdist-parallel-testing-guide), [Bugstrix multi-tenant testing 2026](https://bugstrix.com/blogs/multi-tenant-saas-security-testing-how-to-prevent-cross-tenant-data-leaks/), [Mindful Chase pytest fixture leaks](https://www.mindfulchase.com/explore/troubleshooting-tips/testing-frameworks/troubleshooting-pytest-fixture-leaks,-flaky-tests,-and-performance-bottlenecks-in-enterprise-ci-cd.html).

---

## 8. Onboarding UX — the 3-endpoint flow

### 8.1. Design principles (borrowed from competitor teardowns)

- **Retell** does not publish a tenant-provisioning API; their console-only signup implies a manual/internal provision step. Not a model to copy.
- **Vapi** has an explicit org → workspace → assistant hierarchy exposed via API; workspaces are the tenant boundary. ([Retell vs Vapi 2026 architecture comparison](https://blog.anyreach.ai/retell-ai-alternatives-2026/), [Vapi vs Retell BPO analysis](https://www.jahanzaib.ai/blog/retell-ai-vs-vapi))
- **Hostie / Slang / Loman** — no public API docs; UI-driven signup with backend provisioning invisible to the customer.

Our design: **API-first**, three endpoints, all atomic within a transaction, ergonomic enough that the marketing site can call them from a signup form or a Stripe webhook.

### 8.2. Endpoint contracts

```
POST /v1/tenants
  Auth: bootstrap API key OR Stripe webhook signature
  Body: {name, contact_email, plan, phone_number_hint?}
  Response 201:
    { tenant_id, api_key, business_id, phone_number, dashboard_url }
  Behavior (all-or-nothing in one DB transaction + one Twilio API call):
    1. INSERT tenants row
    2. INSERT default business row with tenant_id
    3. Generate API key (32 bytes urlsafe), INSERT api_keys row (hashed)
    4. Provision Twilio phone number (external — see rollback)
    5. INSERT phone_numbers row linking tenant + Twilio SID
    6. INSERT stripe_customer row + create Stripe Customer object
  Rollback strategy:
    - Steps 1-3, 5, 6: single DB transaction, ROLLBACK on any failure
    - Step 4 (Twilio): released via saga pattern — on any downstream
      failure, hit Twilio DELETE /IncomingPhoneNumbers/{sid} in a
      background job that retries for 24h. This is the ONE non-atomic
      step; it's OK because Twilio numbers are cheap.

POST /v1/tenants/{tenant_id}/api-keys
  Auth: existing tenant API key with admin scope
  Body: {name, scopes[]}
  Response 201: { api_key_id, api_key, name, scopes, created_at }
  Notes: api_key returned ONLY here — never fetch-able again.

POST /v1/tenants/{tenant_id}/businesses
  Auth: tenant API key
  Body: {name, vertical, hours, services, seed_from_template?}
  Response 201: full business object
  Notes: creates SessionRow-facing config; can seed from a
    vertical template (restaurant, clinic, spa).
```

### 8.3. Reference implementation sketch

```python
# apps/api/app/routes/tenants.py
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.tenant import system_tenant, set_current_tenant

router = APIRouter(prefix="/v1/tenants")


@router.post("", status_code=201)
def create_tenant(payload: CreateTenantIn, db: Session = Depends(get_session)):
    # Bootstrap flow runs as system_tenant so listener stays quiet
    with system_tenant():
        tenant_id = f"t_{secrets.token_urlsafe(8)}"
        api_key = f"vk_live_{secrets.token_urlsafe(24)}"
        business_id = f"b_{secrets.token_urlsafe(8)}"

        db.add(TenantRow(id=tenant_id, name=payload.name, plan=payload.plan))
        db.add(ApiKeyRow(
            tenant_id=tenant_id,
            key_hash=hashlib.sha256(api_key.encode()).hexdigest(),
            name="default",
            scopes=["*"],
        ))
        db.add(BusinessRow(
            id=business_id, tenant_id=tenant_id,
            vertical=payload.vertical or "generic",
        ))
        db.flush()  # get all IDs before external calls

        try:
            phone = twilio_client.provision_number(
                area_code=payload.phone_number_hint,
                webhook_url=f"{settings.public_url}/webhooks/twilio/{tenant_id}",
            )
            db.add(PhoneNumberRow(
                tenant_id=tenant_id, number=phone.number,
                twilio_sid=phone.sid,
            ))
        except TwilioError as e:
            db.rollback()
            raise HTTPException(502, f"phone provisioning failed: {e}")

        db.commit()

    return {
        "tenant_id": tenant_id,
        "api_key": api_key,  # returned exactly once
        "business_id": business_id,
        "phone_number": phone.number,
        "dashboard_url": f"{settings.dashboard_url}/{tenant_id}",
    }
```

### 8.4. Should each tenant get its own Postgres schema?

**No.** We picked shared-DB + RLS in §1. Onboarding does zero DDL — just row inserts. This is the whole point.

### 8.5. Stripe metered billing hookup

- Store `stripe_customer_id` on `TenantRow`.
- Emit usage events (`stripe.SubscriptionItem.create_usage_record`) from a background job that counts minutes-of-call per tenant per day.
- Idempotency key on the usage record = `{tenant_id}:{yyyy-mm-dd}` to prevent double-charging on retries.

---

## 9. Migration plan for THIS repo (ordered file list)

Steps in the order they should be committed. Each step is a self-contained PR / commit — never mix them.

**Sprint 6.1 — foundation (no user-visible change)**

1. `apps/api/app/db/tenant.py` — **create** (ContextVar, `TenantScoped` mixin, both event listeners, `install_tenant_listeners`).
2. `apps/api/app/db/session.py` — **modify** to call `install_tenant_listeners(SessionLocal)` at import time and add the `begin` engine event for `set_config('app.tenant_id', ..., true)`.
3. `apps/api/app/middleware/auth.py` — **modify** dispatch to call `set_current_tenant(tenant)` in a try/finally so ContextVar is set for the whole request.
4. `apps/api/tests/conftest.py` — **modify** to add `as_tenant` fixture; convert existing tests to use it.
5. Ship. Zero behavior change (listener is a no-op until we add `tenant_id` to models).

**Sprint 6.2 — schema (expand)**

6. `apps/api/app/db/models.py` — **add** `ConsentRow` (new); add `TenantScoped` mixin to `SessionRow`, `BookingRow`, `TranscriptRow`, `ConsentRow`.
7. `apps/api/alembic/versions/0002_add_tenant_id_nullable.py` — **create** (expand migration from §3.2).
8. Deploy migration 0002. Verify `SELECT COUNT(*) WHERE tenant_id IS NULL = 0` in staging + prod.

**Sprint 6.3 — enforcement (contract)**

9. `apps/api/alembic/versions/0003_tenant_id_not_null.py` — **create** (§3.3).
10. `apps/api/alembic/versions/0004_enable_rls.py` — **create** (§3.4). Only takes effect on Postgres.
11. `apps/api/app/routes/sessions.py` — **delete** `_tenant_owns_business` helper, replace body with the ORM-listener-driven pattern (§2.2). Update all sessions/bookings/consents routes similarly.
12. Ship migrations 0003 + 0004 + route cleanup as one deploy.

**Sprint 6.4 — SQLite → Postgres**

13. `apps/api/app/db/session.py` — **modify** to `create_async_engine` with `postgresql+asyncpg://`.
14. `apps/api/pyproject.toml` — **add** `asyncpg`, `psycopg[binary]` (fallback), `testcontainers[postgres]`.
15. `apps/api/tests/conftest.py` — **replace** the SQLite in-memory fixture with the Postgres testcontainer fixture (§4.4).
16. All handlers in `apps/api/app/routes/` — **modify** to `async def` and `AsyncSession`.
17. CI: add Neon branch-per-PR step so integration tests run against real Postgres.

**Sprint 6.5 — idempotency + onboarding**

18. `apps/api/app/db/models.py` — **add** `IdempotencyKeyRow`, `WebhookEventRow`, `TenantRow`, `ApiKeyRow`, `BusinessRow`, `PhoneNumberRow`.
19. `apps/api/alembic/versions/0005_add_idempotency_and_provisioning.py` — **create**.
20. `apps/api/app/middleware/idempotency.py` — **create** (§6.3).
21. `apps/api/app/routes/bookings.py` — **add** `@idempotent` decorator.
22. `apps/api/app/routes/tenants.py` — **create** (§8.3).
23. `apps/api/app/webhooks/dedup.py` — **create** (§6.4). Wire into existing Twilio + Vapi webhook handlers.
24. `apps/api/tests/test_tenant_isolation.py` — **create** (§7.2), running against Postgres testcontainer.

**Ready-to-ship checkpoints (block deploy if failing):**

- After step 5: existing test suite green, no user-visible change.
- After step 8: `SELECT COUNT(*) FROM sessions WHERE tenant_id IS NULL` = 0 in prod.
- After step 12: cross-tenant read attempt (simulated with two API keys) returns 404, not the other tenant's row. This is the most important test we will ever run.
- After step 17: full test suite runs green against Postgres in CI.
- After step 24: fuzzer from §7.2 runs 500 iterations without failure.

---

## Sources (all Aug 2026 unless noted)

**Architecture pattern**
- [SQLAlchemy `with_loader_criteria` docs, 2.0](https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html)
- [SQLAlchemy `SessionEvents.do_orm_execute`](https://docs.sqlalchemy.org/en/20/orm/session_events.html)
- [SQLAlchemy ORM Events, 2.1](https://docs.sqlalchemy.org/en/21/orm/events.html)
- [Telemaco019/sqlalchemy-tenants library](https://github.com/Telemaco019/sqlalchemy-tenants)
- [fastapi-extensions/fastapi-tenancy](https://github.com/fastapi-extensions/fastapi-tenancy)
- [Kuldeep Pisda — FastAPI RLS 2026](https://kdpisda.in/fastapi-rls-postgres-row-level-security-fastapi/)
- [Adriano Vieira — RLS with SQLAlchemy and Alembic](https://www.adrianovieira.eng.br/en/posts/architecture/row-level-security-sqlachemy-alembic-guide/)
- [Rico Fritzsche — Mastering PostgreSQL RLS 2026](https://ricofritzsche.me/mastering-postgresql-row-level-security-rls-for-rock-solid-multi-tenancy/)
- [Crunchy Data — Row Level Security for Tenants in Postgres](https://www.crunchydata.com/blog/row-level-security-for-tenants-in-postgres)
- [AWS — Multi-tenant data isolation with PostgreSQL RLS](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/)

**Multi-tenant DB comparison**
- [PlanetScale — Approaches to tenancy in Postgres 2026](https://planetscale.com/blog/approaches-to-tenancy-in-postgres)
- [ClickHouse — How to architect multi-tenant SaaS on Postgres 2026](https://clickhouse.com/resources/engineering/multi-tenant-saas-postgres-architecture)
- [DEV — Multi-Tenant Architecture 2026 benchmark](https://dev.to/young_gao/multi-tenant-architecture-database-per-tenant-vs-shared-schema-1n2e)
- [Ajit Singh — Designing DB Isolation for B2B Multi-Tenant SaaS](https://singhajit.com/multi-tenant-database-isolation/)
- [Ali Asghar — RLS vs schema vs DB 2026](https://aliasghar.me/blog/multi-tenant-saas-data-isolation)
- [Aditya Agrawal — Building SaaS with Postgres 2026](https://www.adiagr.com/blog/07-saas-postgres-multitenancy-patterns/)
- [dasroot — Multi-Tenancy Database Patterns 2026](https://dasroot.net/posts/2026/01/multi-tenancy-database-patterns-schema-database-row-level/)
- [Nile Postgres architecture](https://www.thenile.dev/docs/getting-started/architecture)

**Alembic migrations**
- [Alembic Batch migrations for SQLite 1.18](https://alembic.sqlalchemy.org/en/latest/batch.html)
- [Alembic Cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)
- [that.guru — Zero-downtime upgrades with Alembic](https://that.guru/blog/zero-downtime-upgrades-with-alembic-and-sqlalchemy/)
- [OneUptime — Alembic guide 2025](https://oneuptime.com/blog/post/2025-07-02-python-alembic-migrations/view)
- [Atlas vs Alembic 2026](https://atlasgo.io/guides/atlas-vs-alembic)
- [Toolradar — 10 best DB migration tools 2026](https://toolradar.com/blog/best-database-migration-tools)

**Postgres providers pricing**
- [Neon pricing](https://neon.com/pricing)
- [Neon pricing calculator 2026 (makerkit)](https://makerkit.dev/pricing-calculator/neon)
- [Neon pricing explained (swyftstack)](https://swyftstack.com/blog/neon-pricing-explained)
- [Supabase pricing 2026 (makerkit)](https://makerkit.dev/blog/saas/supabase-pricing)
- [Supabase real costs (metacto)](https://www.metacto.com/blogs/the-true-cost-of-supabase-a-comprehensive-guide-to-pricing-integration-and-maintenance)
- [Fly.io pricing 2026 (kuberns)](https://kuberns.com/blogs/flyio-pricing/)
- [Fly.io alternatives 2026 (expresstech)](https://expresstech.io/7-fly-io-alternatives-in-2026-real-pricing-after-the-free-tier-died/)
- [Railway vs Fly.io 2026 (northflank)](https://northflank.com/blog/railway-vs-flyio)
- [Turso vs Neon vs Supabase 2026 (devtoolpicks)](https://devtoolpicks.com/blog/turso-vs-neon-vs-supabase-indie-hackers-2026)
- [Neon vs Supabase 2026 (tech-insider)](https://tech-insider.org/neon-vs-supabase-2026/)
- [Best DB software for startups 2026 (makerkit)](https://makerkit.dev/blog/tutorials/best-database-software-startups)
- [Northflank — Postgres alternatives 2026](https://northflank.com/blog/neon-planetscale-postgres-alternatives)

**Drivers + async**
- [rajpoot.dev — Async Python DB drivers in 2026](https://blog.rajpoot.dev/posts/python/python-async-database-drivers-2026/)
- [FastAPI discussion #13732 — async DB benchmarking](https://github.com/fastapi/fastapi/discussions/13732)
- [Gold Lapel — asyncpg vs psycopg3 in FastAPI](https://goldlapel.com/grounds/django-python/asyncpg-vs-psycopg3-fastapi)
- [Fernando Arteaga — Psycopg 3 vs Asyncpg](https://fernandoarteaga.dev/blog/psycopg-vs-asyncpg/)
- [dasroot — SQLAlchemy vs asyncpg 2026](https://dasroot.net/posts/2026/02/python-postgresql-sqlalchemy-asyncpg-performance-comparison/)

**Idempotency**
- [Brandur — Implementing Stripe-like Idempotency Keys in Postgres](https://brandur.org/idempotency-keys)
- [Brandur — Simple internal idempotency by ID](https://brandur.org/fragments/simple-internal-idempotency)
- [Nerd Level Tech — Idempotency Keys with Node + Postgres 2026](https://nerdleveltech.com/idempotency-keys-nodejs-postgres-api-tutorial)
- [Simplico — Idempotency in Payment APIs 2026](https://simplico.net/2026/04/04/idempotency-in-payment-apis-prevent-double-charges-with-stripe-omise-and-2c2p/)
- [Luca Palmieri — In-Depth Introduction to Idempotency](https://lpalmieri.com/posts/idempotency/)
- [WebhookAgent — Idempotent Webhooks pattern 2026](https://webhookagent.com/automation-pattern-2026-04-28-idempotent-webhooks)
- [Twilio — Handling Duplicate Data](https://www.twilio.com/docs/segment/guides/duplicate-data)
- [Vapi — Call Handling with Vapi and Twilio](https://docs.vapi.ai/calls/call-handling-with-vapi-and-twilio)

**Testing**
- [pytest-async-sqlalchemy on PyPI](https://pypi.org/project/pytest-async-sqlalchemy/)
- [core27 — Transactional Unit Tests with Pytest and Async SQLAlchemy](https://www.core27.co/post/transactional-unit-tests-with-pytest-and-async-sqlalchemy)
- [OneUptime — Testcontainers guide 2025](https://oneuptime.com/blog/post/2025-01-06-python-testcontainers-integration/view)
- [QASkills — Pytest fixtures scope guide 2026](https://qaskills.sh/blog/pytest-fixtures-scope-complete-guide)
- [Bugstrix — Multi-Tenant SaaS Security Testing](https://bugstrix.com/blogs/multi-tenant-saas-security-testing-how-to-prevent-cross-tenant-data-leaks/)

**Security post-mortems**
- [InstaTunnel — Multi-Tenant Leakage: When RLS Fails](https://medium.com/@instatunnel/multi-tenant-leakage-when-row-level-security-fails-in-saas-da25f40c788c)
- [InstaTunnel blog mirror](https://instatunnel.my/blog/multi-tenant-leakage-when-row-level-security-fails-in-saas)
- [AppOmni — 2024 SaaS Breaches predictions 2025](https://appomni.com/blog/saas-security-predictions-2025/)
- [Valence Security — 2024 SaaS Breach Lessons](https://www.valencesecurity.com/resources/blogs/2024-saas-security-breaches-lessons-learned)
- [Pentesttesting — 30-Day Multi-Tenant SaaS Breach Containment Blueprint](https://www.pentesttesting.com/multi-tenant-saas-breach-containment/)

**Competitor architecture**
- [AnyReach — Retell AI alternatives 2026](https://blog.anyreach.ai/retell-ai-alternatives-2026/)
- [Jahanzaib — Retell AI vs VAPI 2026](https://www.jahanzaib.ai/blog/retell-ai-vs-vapi)
- [Sympana — Retell vs Vapi for GHL Agencies 2026](https://www.sympana.com/blog/retell-ai-vs-vapi-for-gohighlevel-agencies-2026)
- [Layer3Labs — Vapi vs Retell 2026](https://www.layer3labs.io/comparisons/vapi-vs-retell-ai)

**Session variables + set_config**
- [pgedge — Using Session Variables in Postgres](https://www.pgedge.com/blog/it-depends-using-session-variables-in-postgres)
- [SQLAlchemy Discussion #13020 — GUC reset on commit](https://github.com/sqlalchemy/sqlalchemy/discussions/13020)
- [Tenant Isolation with Postgres RLS and SQLAlchemy](https://personal-web-9c834.web.app/blog/pg-tenant-isolation/)
- [DEV — PostgreSQL RLS for Multi-Tenant SaaS](https://dev.to/software_mvp-factory/postgresql-row-level-security-for-multi-tenant-saas-1lgp)
- [QueryPlane — Postgres RLS in Practice](https://queryplane.com/blog/postgres-row-level-security-in-practice/)
