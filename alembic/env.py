"""Alembic environment, adapted for an async SQLAlchemy engine.

The database URL is NOT read from `alembic.ini` — it comes from
`app.config.get_settings().pg_dsn`, so application config remains the single
source of truth. Both offline mode (`--sql`, no DB connection required) and
online mode (real async engine via `run_sync`) are supported.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import get_settings
from app.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata

# Single source of truth for the DB URL: application settings, not alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().pg_dsn)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL, emitting SQL to the script
    output without requiring a live DB connection or DBAPI driver.
    """
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
    """Run migrations in 'online' mode using an async engine via `run_sync`."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
