"""SQLAlchemy 2.0 DeclarativeBase, engine builder, and session factory for PostgreSQL."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from argenprecios.config import DatabaseSettings

logger = logging.getLogger(__name__)

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class WarehouseBase(DeclarativeBase):
    """Canonical SQLAlchemy 2.0 declarative base for the ArgenPrecios PostgreSQL warehouse."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_warehouse_engine(
    database_url: str,
    settings: DatabaseSettings | None = None,
) -> Engine:
    """Create a pooled SQLAlchemy 2.0 Engine using psycopg v3."""
    cfg = settings or DatabaseSettings()
    connect_args: dict[str, Any] = {}
    if database_url.startswith("postgresql"):
        connect_args["options"] = f"-c statement_timeout={cfg.statement_timeout_ms}"

    return create_engine(
        database_url,
        echo=cfg.echo_sql,
        pool_pre_ping=True,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a SQLAlchemy 2.0 session factory with expire_on_commit=False."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


__all__ = [
    "NAMING_CONVENTION",
    "WarehouseBase",
    "create_session_factory",
    "create_warehouse_engine",
]
