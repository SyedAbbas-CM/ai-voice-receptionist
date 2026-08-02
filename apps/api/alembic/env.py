"""Alembic env — loads the DATABASE_URL from our app Settings so migrations
run against whatever DB the app runs against.

Usage from repo root:

    cd apps/api
    PYTHONPATH="../..:$(pwd)" alembic upgrade head
    PYTHONPATH="../..:$(pwd)" alembic revision --autogenerate -m "add tenant_id"
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo importable so `app.*` resolves the same way `main.py` does.
_APP_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _APP_DIR.parents[1]
for p in (str(_REPO_ROOT), str(_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.core.config import settings  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.db import models  # noqa: E402,F401  — registers tables on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the alembic.ini placeholder URL with our runtime setting
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite doesn't support ALTER on most things — batch mode transparently
        # rewrites as CREATE+COPY+DROP+RENAME.
        render_as_batch=settings.database_url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=settings.database_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
