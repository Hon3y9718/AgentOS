"""Alembic migration environment.

Role: wires alembic to app.config (DB URL) and app.db.base (target metadata).
No migrations are hand-authored here — `make migrate` drives autogenerate.
Called by: the `alembic` CLI, via `make migrate`. Calls app.config, app.db.base.
Gotcha: our only driver is asyncpg, so migrations run through an async engine
via asyncio.run + run_sync — the sync `engine_from_config` alembic generates
by default does not work with an asyncpg URL.
See: docs/ARCHITECTURE.md (db/: "migration entry point")
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import settings
from app.db.base import Base

# Importing these registers their tables on Base.metadata before autogenerate
# runs below. A model module that's never imported here is invisible to
# `alembic revision --autogenerate`, even though it inherits Base.
from app.models import conversation, idempotency_key, message, user  # noqa: F401

# WHY set here rather than in alembic.ini: the URL comes from app.config (the
# one place config is allowed to live), not a second copy hardcoded in an ini
# file that would drift from it.
config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a live DB connection (`alembic upgrade --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live DB via the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
