"""Pytest fixtures managing an isolated PostgreSQL 16 test database via Alembic migrations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from argenprecios.adapters.db import create_session_factory, create_warehouse_engine
from argenprecios.adapters.db.migrations.runner import upgrade_warehouse_head
from argenprecios.config import AppConfig, DatabaseSettings, load_config

DEFAULT_ADMIN_DB_URL = (
    "postgresql+psycopg://argenprecios:argenprecios_dev@127.0.0.1:5433/argenprecios"
)
DEFAULT_TEST_DB_URL = (
    "postgresql+psycopg://argenprecios:argenprecios_dev@127.0.0.1:5433/argenprecios_test"
)


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Provision a clean `argenprecios_test` PostgreSQL database and run `alembic upgrade head`."""
    admin_url = os.getenv("ARGENPRECIOS_TEST_ADMIN_URL", DEFAULT_ADMIN_DB_URL)
    target_url = os.getenv("ARGENPRECIOS_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = 'argenprecios_test' AND pid <> pg_backend_pid()
                    """
                )
            )
            conn.execute(text("DROP DATABASE IF EXISTS argenprecios_test"))
            conn.execute(text("CREATE DATABASE argenprecios_test"))
    finally:
        admin_engine.dispose()

    # Schema creation strictly goes through Alembic migrations (never metadata.create_all)
    upgrade_warehouse_head(database_url=target_url)
    yield target_url


@pytest.fixture(scope="session")
def pg_engine(test_database_url: str) -> Iterator[Engine]:
    """Session-scoped SQLAlchemy 2.0 Engine bound to `argenprecios_test`."""
    engine = create_warehouse_engine(
        test_database_url,
        DatabaseSettings(pool_size=5, max_overflow=5, statement_timeout_ms=60_000),
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine: Engine) -> sessionmaker[Session]:
    """Return a SQLAlchemy 2.0 sessionmaker bound to the test PostgreSQL engine."""
    return create_session_factory(pg_engine)


@pytest.fixture
def pg_session(pg_session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a clean database Session for integration tests."""
    with pg_session_factory() as session:
        yield session


@pytest.fixture
def isolated_app_config(
    tmp_path: Path, test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AppConfig:
    """Build an isolated AppConfig rooted in tmp_path and pointing to `argenprecios_test`."""
    workdir = (tmp_path / "argenprecios_workdir").resolve()
    monkeypatch.setenv("ARGENPRECIOS_DATABASE_URL", test_database_url)
    monkeypatch.setenv("ARGENPRECIOS_SECRET_KEY", "test-secret-key-at-least-32-characters-long")
    cfg = load_config(workdir=workdir)
    cfg.ensure_owned_directories()
    return cfg
