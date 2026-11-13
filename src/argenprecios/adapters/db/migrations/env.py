"""Alembic migration environment configured with SQLAlchemy 2.0 WarehouseBase and pathlib."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from argenprecios.adapters.db import WarehouseBase
from argenprecios.config import DEFAULT_DEV_DATABASE_URL

config = context.config

target_metadata = WarehouseBase.metadata


def _resolve_database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return os.getenv("ARGENPRECIOS_DATABASE_URL", DEFAULT_DEV_DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' SQL generation mode."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' connected engine mode."""
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        context.configure(connection=existing_connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = _resolve_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
