"""Batched PostgreSQL repository for partitioned price_spans (HOT updates + bulk upserts)."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_facts import PriceSpanModel
from argenprecios.domain.models import (
    IncomingPricePoint,
    OpenSpanState,
    SpanKey,
    SpanTransitionBatch,
)
from argenprecios.domain.span_compressor import rebuild_timeline_with_backfill

logger = logging.getLogger(__name__)


class PostgresSpanRepository:
    """High-throughput SQLAlchemy 2.0 adapter for quarterly range-partitioned price_spans."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def fetch_latest_spans(
        self,
        keys: Iterable[SpanKey],
        query_chunk_size: int = 2_000,
    ) -> dict[SpanKey, OpenSpanState]:
        """Fetch the single latest span (highest valid_from) for each (article_id, pos_id, source_id)."""
        unique_keys = list({(k.article_id, k.pos_id, k.source_id): k for k in keys}.values())
        if not unique_keys:
            return {}

        result: dict[SpanKey, OpenSpanState] = {}
        for i in range(0, len(unique_keys), query_chunk_size):
            chunk = unique_keys[i : i + query_chunk_size]
            triples = [(k.article_id, k.pos_id, k.source_id) for k in chunk]
            stmt = (
                select(PriceSpanModel)
                .distinct(
                    PriceSpanModel.article_id,
                    PriceSpanModel.pos_id,
                    PriceSpanModel.source_id,
                )
                .where(
                    tuple_(
                        PriceSpanModel.article_id,
                        PriceSpanModel.pos_id,
                        PriceSpanModel.source_id,
                    ).in_(triples)
                )
                .order_by(
                    PriceSpanModel.article_id,
                    PriceSpanModel.pos_id,
                    PriceSpanModel.source_id,
                    PriceSpanModel.valid_from.desc(),
                )
            )
            for row in self._session.scalars(stmt):
                sk = SpanKey(
                    article_id=row.article_id,
                    pos_id=row.pos_id,
                    source_id=row.source_id,
                )
                result[sk] = OpenSpanState(
                    article_id=row.article_id,
                    pos_id=row.pos_id,
                    source_id=row.source_id,
                    valid_from=row.valid_from,
                    last_seen=row.last_seen,
                    observed_days=row.observed_days,
                    list_price_cents=row.list_price_cents,
                    promo_price_cents=row.promo_price_cents,
                    promo_rule_id=row.promo_rule_id,
                    in_stock=row.in_stock,
                )
        return result

    def fetch_all_spans_for_keys(
        self,
        keys: Iterable[SpanKey],
        query_chunk_size: int = 1_000,
    ) -> list[OpenSpanState]:
        """Fetch complete span histories for keys impacted by an out-of-order backfill."""
        unique_keys = list({(k.article_id, k.pos_id, k.source_id): k for k in keys}.values())
        if not unique_keys:
            return []

        spans: list[OpenSpanState] = []
        for i in range(0, len(unique_keys), query_chunk_size):
            chunk = unique_keys[i : i + query_chunk_size]
            triples = [(k.article_id, k.pos_id, k.source_id) for k in chunk]
            stmt = (
                select(PriceSpanModel)
                .where(
                    tuple_(
                        PriceSpanModel.article_id,
                        PriceSpanModel.pos_id,
                        PriceSpanModel.source_id,
                    ).in_(triples)
                )
                .order_by(PriceSpanModel.valid_from.asc())
            )
            for row in self._session.scalars(stmt):
                spans.append(
                    OpenSpanState(
                        article_id=row.article_id,
                        pos_id=row.pos_id,
                        source_id=row.source_id,
                        valid_from=row.valid_from,
                        last_seen=row.last_seen,
                        observed_days=row.observed_days,
                        list_price_cents=row.list_price_cents,
                        promo_price_cents=row.promo_price_cents,
                        promo_rule_id=row.promo_rule_id,
                        in_stock=row.in_stock,
                    )
                )
        return spans

    def apply_transition_batch(
        self,
        batch: SpanTransitionBatch,
        chunk_size: int = 10_000,
    ) -> tuple[int, int]:
        """Apply deletes, HOT-friendly extensions, and bulk inserts; return (extended, inserted)."""
        # 1. Deletes (only triggered on same-day price revisions or historical backfill splits)
        if batch.deletes:
            del_sql = text(
                """
                DELETE FROM price_spans
                WHERE article_id = :article_id
                  AND pos_id = :pos_id
                  AND source_id = :source_id
                  AND valid_from = :valid_from
                """
            )
            for i in range(0, len(batch.deletes), chunk_size):
                del_chunk = [
                    {
                        "article_id": d.article_id,
                        "pos_id": d.pos_id,
                        "source_id": d.source_id,
                        "valid_from": d.valid_from,
                    }
                    for d in batch.deletes[i : i + chunk_size]
                ]
                self._session.execute(del_sql, del_chunk)

        # 2. Extensions: only update non-indexed columns (last_seen_date/observed_days; note
        #    last_seen has only BRIN on child partition + pos_last_seen B-tree when modified,
        #    keeping tuple modifications minimal on fillfactor=85 pages)
        extended_count = 0
        if batch.extensions:
            upd_sql = text(
                """
                UPDATE price_spans
                SET last_seen = :new_last_seen,
                    observed_days = :new_observed_days
                WHERE article_id = :article_id
                  AND pos_id = :pos_id
                  AND source_id = :source_id
                  AND valid_from = :valid_from
                """
            )
            for i in range(0, len(batch.extensions), chunk_size):
                ext_chunk = [
                    {
                        "article_id": e.article_id,
                        "pos_id": e.pos_id,
                        "source_id": e.source_id,
                        "valid_from": e.valid_from,
                        "new_last_seen": e.new_last_seen,
                        "new_observed_days": e.new_observed_days,
                    }
                    for e in batch.extensions[i : i + chunk_size]
                ]
                self._session.execute(upd_sql, ext_chunk)
                extended_count += len(ext_chunk)

        # 3. Bulk Inserts
        inserted_count = 0
        if batch.inserts:
            for i in range(0, len(batch.inserts), chunk_size):
                ins_chunk = [
                    {
                        "article_id": r.article_id,
                        "pos_id": r.pos_id,
                        "source_id": r.source_id,
                        "valid_from": r.valid_from,
                        "last_seen": r.last_seen,
                        "observed_days": r.observed_days,
                        "list_price_cents": r.list_price_cents,
                        "promo_price_cents": r.promo_price_cents,
                        "promo_rule_id": r.promo_rule_id,
                        "in_stock": r.in_stock,
                    }
                    for r in batch.inserts[i : i + chunk_size]
                ]
                ins_stmt = pg_insert(PriceSpanModel).values(ins_chunk)
                upsert_stmt = ins_stmt.on_conflict_do_update(
                    index_elements=["article_id", "pos_id", "source_id", "valid_from"],
                    set_={
                        "last_seen": ins_stmt.excluded.last_seen,
                        "observed_days": ins_stmt.excluded.observed_days,
                        "list_price_cents": ins_stmt.excluded.list_price_cents,
                        "promo_price_cents": ins_stmt.excluded.promo_price_cents,
                        "promo_rule_id": ins_stmt.excluded.promo_rule_id,
                        "in_stock": ins_stmt.excluded.in_stock,
                    },
                )
                self._session.execute(upsert_stmt)
                inserted_count += len(ins_chunk)

        return (extended_count, inserted_count)

    def upsert_historical_points(
        self,
        points: Iterable[IncomingPricePoint],
        max_bridge_gap_days: int = 2,
        chunk_size: int = 10_000,
    ) -> tuple[int, int]:
        """Rebuild affected timelines losslessly for out-of-order historical backfills."""
        pts_list = list(points)
        if not pts_list:
            return (0, 0)
        keys = [
            SpanKey(article_id=p.article_id, pos_id=p.pos_id, source_id=p.source_id)
            for p in pts_list
        ]
        existing = self.fetch_all_spans_for_keys(keys)
        transition_batch = rebuild_timeline_with_backfill(
            existing_spans=existing,
            backfill_points=pts_list,
            max_bridge_gap_days=max_bridge_gap_days,
        )
        return self.apply_transition_batch(transition_batch, chunk_size=chunk_size)


__all__ = [
    "PostgresSpanRepository",
]
