"""Programmatic Alembic migration runner so schema creation always goes through Alembic."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

logger = logging.getLogger(__name__)


def build_alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic Config pointing to src/argenprecios/adapters/db/migrations."""
    migrations_dir = Path(__file__).resolve().parent
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(migrations_dir))
    if database_url is not None:
        alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    return alembic_cfg


def upgrade_warehouse_head(
    database_url: str | None = None,
    connection: Connection | None = None,
    revision: str = "head",
) -> None:
    """Run `alembic upgrade head` against database_url or an existing SQLAlchemy Connection."""
    alembic_cfg = build_alembic_config(database_url=database_url)
    if connection is not None:
        alembic_cfg.attributes["connection"] = connection
    command.upgrade(alembic_cfg, revision)
    logger.info("Alembic migration upgraded to revision '%s'", revision)


__all__ = [
    "build_alembic_config",
    "upgrade_warehouse_head",
]
