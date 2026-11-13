"""Integration tests verifying Alembic migrations, extensions, 24 provinces, and quarterly partitions."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_geo import ProvinceModel, StatisticalRegionModel
from argenprecios.adapters.db.partition_manager import (
    compact_cold_partition_to_chain_city,
    ensure_partitions_window,
    get_database_size_bytes,
    seal_historical_partition,
)


def test_alembic_seed_extensions_regions_and_provinces(pg_session: Session) -> None:
    # Verify PostgreSQL extensions enabled by 0001_initial_warehouse.py
    exts = set(
        pg_session.scalars(
            text("SELECT extname FROM pg_extension WHERE extname IN ('unaccent', 'pg_trgm')")
        ).all()
    )
    assert exts == {"unaccent", "pg_trgm"}

    # Verify immutable accent-stripping SQL function works
    stripped = pg_session.scalar(text("SELECT argenprecios_unaccent('Azúcar Láctea Ñandú')"))
    assert stripped == "Azucar Lactea Nandu"

    # Verify 6 INDEC regions and 24 Argentine ISO provinces seeded
    regions_count = pg_session.scalar(select(func.count(StatisticalRegionModel.id)))
    provinces_count = pg_session.scalar(select(func.count(ProvinceModel.id)))
    assert regions_count == 6
    assert provinces_count == 24


def test_quarterly_partition_lifecycle_brin_and_cold_compaction(
    pg_engine: Engine,
    pg_session: Session,
) -> None:
    with pg_engine.begin() as conn:
        parts = ensure_partitions_window(
            conn,
            anchor_date=date(2026, 4, 8),
            quarters_back=1,
            quarters_ahead=2,
            fillfactor=85,
        )
    assert "price_spans_2026_q2" in parts
    assert "price_spans_2026_q3" in parts

    # Verify BRIN index exists on partition
    brin_exists = pg_session.scalar(
        text(
            """
            SELECT COUNT(*) FROM pg_indexes
            WHERE tablename = 'price_spans_2026_q2'
              AND indexname = 'brin_price_spans_2026_q2_dates'
            """
        )
    )
    assert int(brin_exists or 0) == 1

    sealed = seal_historical_partition(pg_engine, year=2026, quarter=1, run_vacuum_freeze=False)
    assert sealed == "price_spans_2026_q1"

    compacted = compact_cold_partition_to_chain_city(
        pg_session,
        year=2026,
        quarter=1,
        drop_branch_rows_after_compact=False,
    )
    pg_session.commit()
    assert compacted >= 0
    assert get_database_size_bytes(pg_session) > 0
