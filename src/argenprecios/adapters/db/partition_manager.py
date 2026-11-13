"""PostgreSQL quarterly partition lifecycle, BRIN indexing, and cold storage compaction."""

from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_PARTITION_NAME_RE = re.compile(r"^price_spans_\d{4}_q[1-4]$")


def date_to_year_quarter(target: date) -> tuple[int, int]:
    """Return (year, quarter) with quarter in 1..4 for any calendar date."""
    quarter = ((target.month - 1) // 3) + 1
    return (target.year, quarter)


def advance_quarter(year: int, quarter: int, steps: int = 1) -> tuple[int, int]:
    """Advance (year, quarter) forward or backward by `steps` quarters."""
    total_quarters = (year * 4) + (quarter - 1) + steps
    new_year, rem = divmod(total_quarters, 4)
    return (new_year, rem + 1)


def quarter_date_bounds(year: int, quarter: int) -> tuple[date, date]:
    """Return inclusive start date and exclusive end date [start, end) for (year, quarter)."""
    if quarter not in {1, 2, 3, 4}:
        raise ValueError(f"Invalid quarter: {quarter}")
    start_month = ((quarter - 1) * 3) + 1
    start = date(year, start_month, 1)
    next_year, next_quarter = advance_quarter(year, quarter, 1)
    end_month = ((next_quarter - 1) * 3) + 1
    end = date(next_year, end_month, 1)
    return (start, end)


def partition_table_name(year: int, quarter: int) -> str:
    """Format validated PostgreSQL partition table identifier `price_spans_YYYY_qN`."""
    name = f"price_spans_{year:04d}_q{quarter}"
    if not _PARTITION_NAME_RE.match(name):
        raise ValueError(f"Unsafe partition identifier rejected: {name}")
    return name


def ensure_quarterly_partition(
    conn: Connection,
    year: int,
    quarter: int,
    fillfactor: int = 85,
) -> str:
    """Create quarterly partition of price_spans with HOT-update fillfactor and BRIN index."""
    if not 50 <= fillfactor <= 100:
        raise ValueError(f"fillfactor out of range [50, 100]: {fillfactor}")
    part_name = partition_table_name(year, quarter)
    start_dt, end_dt = quarter_date_bounds(year, quarter)
    brin_idx = f"brin_{part_name}_dates"

    conn.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {part_name} "
            f"PARTITION OF price_spans "
            f"FOR VALUES FROM ('{start_dt.isoformat()}') TO ('{end_dt.isoformat()}') "
            f"WITH (fillfactor = {fillfactor})"
        )
    )
    conn.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {brin_idx} "
            f"ON {part_name} USING brin (valid_from, last_seen)"
        )
    )
    return part_name


def ensure_partitions_window(
    conn: Connection,
    anchor_date: date,
    quarters_back: int = 1,
    quarters_ahead: int = 2,
    fillfactor: int = 85,
) -> list[str]:
    """Ensure contiguous quarterly partitions exist around anchor_date."""
    base_year, base_quarter = date_to_year_quarter(anchor_date)
    created: list[str] = []
    for offset in range(-quarters_back, quarters_ahead + 1):
        yr, qtr = advance_quarter(base_year, base_quarter, offset)
        created.append(ensure_quarterly_partition(conn, yr, qtr, fillfactor=fillfactor))
    return created


def seal_historical_partition(
    engine: Engine,
    year: int,
    quarter: int,
    run_vacuum_freeze: bool = False,
) -> str:
    """Set fillfactor=100 on a closed historical partition and optionally VACUUM FREEZE it."""
    part_name = partition_table_name(year, quarter)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {part_name} SET (fillfactor = 100)"))

    if run_vacuum_freeze:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as autocommit_conn:
            autocommit_conn.execute(text(f"VACUUM (FREEZE, ANALYZE) {part_name}"))

    logger.info("Sealed historical partition %s (vacuum_freeze=%s)", part_name, run_vacuum_freeze)
    return part_name


def compact_cold_partition_to_chain_city(
    session: Session,
    year: int,
    quarter: int,
    drop_branch_rows_after_compact: bool = False,
) -> int:
    """Safety valve: compact branch-level cold partition spans into chain_city_spans."""
    part_name = partition_table_name(year, quarter)
    start_dt, end_dt = quarter_date_bounds(year, quarter)

    insert_sql = text(
        """
        INSERT INTO chain_city_spans (
            article_id,
            chain_id,
            city_id,
            pos_channel,
            source_id,
            valid_from,
            last_seen,
            observed_days,
            branch_count,
            list_price_cents,
            promo_price_cents,
            in_stock
        )
        SELECT
            ps.article_id,
            pos.chain_id,
            pos.city_id,
            pos.pos_channel,
            ps.source_id,
            ps.valid_from,
            MAX(ps.last_seen) AS last_seen,
            MAX(ps.observed_days) AS observed_days,
            COUNT(*) AS branch_count,
            PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ps.list_price_cents) AS list_price_cents,
            PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ps.promo_price_cents) AS promo_price_cents,
            BOOL_OR(ps.in_stock) AS in_stock
        FROM price_spans ps
        JOIN points_of_sale pos ON pos.id = ps.pos_id
        WHERE ps.valid_from >= :start_dt AND ps.valid_from < :end_dt
        GROUP BY
            ps.article_id,
            pos.chain_id,
            pos.city_id,
            pos.pos_channel,
            ps.source_id,
            ps.valid_from
        ON CONFLICT (article_id, chain_id, city_id, pos_channel, source_id, valid_from)
        DO UPDATE SET
            last_seen = GREATEST(chain_city_spans.last_seen, EXCLUDED.last_seen),
            observed_days = GREATEST(chain_city_spans.observed_days, EXCLUDED.observed_days),
            branch_count = EXCLUDED.branch_count
        """
    )
    result = session.execute(insert_sql, {"start_dt": start_dt, "end_dt": end_dt})
    compacted_count = int(getattr(result, "rowcount", 0) or 0)

    if drop_branch_rows_after_compact:
        session.execute(text(f"TRUNCATE TABLE {part_name}"))

    logger.info(
        "Compacted cold partition %s into %d chain_city_spans rows (truncated=%s)",
        part_name,
        compacted_count,
        drop_branch_rows_after_compact,
    )
    return compacted_count


def get_database_size_bytes(session: Session) -> int:
    """Return current PostgreSQL database disk footprint in bytes."""
    val = session.scalar(text("SELECT pg_database_size(current_database())"))
    return int(val or 0)


__all__ = [
    "advance_quarter",
    "compact_cold_partition_to_chain_city",
    "date_to_year_quarter",
    "ensure_partitions_window",
    "ensure_quarterly_partition",
    "get_database_size_bytes",
    "partition_table_name",
    "quarter_date_bounds",
    "seal_historical_partition",
]
