"""Ingestion orchestration use case connecting source adapters to catalog & price_spans."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_facts import IngestionBatchModel
from argenprecios.adapters.db.partition_manager import ensure_partitions_window
from argenprecios.adapters.db.repositories_catalog import CatalogAndGeoRepository
from argenprecios.adapters.db.repositories_spans import PostgresSpanRepository
from argenprecios.adapters.sources.sepa_national import compute_file_sha256
from argenprecios.config import SpanSettings, resolve_user_path
from argenprecios.domain.enums import SourceChannel
from argenprecios.domain.models import (
    IncomingPricePoint,
    IngestionRunResult,
    RawPriceObservation,
    SpanKey,
)
from argenprecios.domain.ports import ObservationSourcePort
from argenprecios.domain.span_compressor import compute_incremental_transitions

logger = logging.getLogger(__name__)


def compute_directory_or_file_sha256(path: Path) -> str:
    """Compute a deterministic SHA-256 digest for either a single file or a timestamped folder."""
    resolved = resolve_user_path(path)
    if resolved.is_file():
        return compute_file_sha256(resolved)
    hasher = hashlib.sha256()
    for child in sorted(p for p in resolved.rglob("*") if p.is_file()):
        rel = child.relative_to(resolved).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(compute_file_sha256(child).encode("ascii"))
    return hasher.hexdigest()


class IngestSourceBatchUseCase:
    """Ingests RawPriceObservations with SHA-256 idempotency, partition creation, and HOT spans."""

    def __init__(
        self,
        session: Session,
        span_settings: SpanSettings | None = None,
    ) -> None:
        self._session = session
        self._settings = span_settings or SpanSettings()

    def ingest_from_adapter(
        self,
        adapter: ObservationSourcePort,
        source_path: Path,
        historical_backfill: bool = False,
        force: bool = False,
    ) -> IngestionRunResult:
        """Ingest a file/folder via an ObservationSourcePort with ingestion_batches tracking."""
        resolved_path = resolve_user_path(source_path)
        digest = compute_directory_or_file_sha256(resolved_path)
        return self.ingest_stream(
            source_channel=adapter.source_channel,
            source_uri=str(resolved_path),
            sha256_digest=digest,
            observations=adapter.iter_observations(resolved_path),
            historical_backfill=historical_backfill,
            force=force,
        )

    def ingest_stream(
        self,
        source_channel: SourceChannel,
        source_uri: str,
        sha256_digest: str,
        observations: Iterable[RawPriceObservation],
        historical_backfill: bool = False,
        force: bool = False,
    ) -> IngestionRunResult:
        """Process a stream of RawPriceObservation records in bounded chunks."""
        cat_repo = CatalogAndGeoRepository(self._session)
        span_repo = PostgresSpanRepository(self._session)
        source_id = cat_repo.get_or_create_source_id(source_channel.value)

        existing_batch = self._session.scalar(
            select(IngestionBatchModel).where(
                IngestionBatchModel.source_id == source_id,
                IngestionBatchModel.sha256_digest == sha256_digest,
            )
        )
        if existing_batch is not None and existing_batch.status == "succeeded" and not force:
            logger.info(
                "Skipping already-ingested batch uri=%s sha256=%s",
                source_uri,
                sha256_digest[:12],
            )
            return IngestionRunResult(
                batch_id=existing_batch.id,
                skipped_duplicate=True,
                source_uri=source_uri,
                data_date=existing_batch.data_date,
                rows_read=existing_batch.rows_read,
                spans_extended=existing_batch.spans_extended,
                spans_inserted=existing_batch.spans_inserted,
                new_articles_count=existing_batch.new_articles_count,
            )

        batch_row = existing_batch or IngestionBatchModel(
            source_id=source_id,
            source_uri=source_uri[:500],
            sha256_digest=sha256_digest,
            data_date=date.today(),
            status="running",
            started_at=datetime.now(UTC),
        )
        batch_row.status = "running"
        batch_row.error_message = None
        if existing_batch is None:
            self._session.add(batch_row)
        self._session.flush()

        total_rows = 0
        total_extended = 0
        total_inserted = 0
        ensured_quarters_dates: set[tuple[int, int]] = set()
        first_observed_date: date | None = None

        chunk_size = self._settings.upsert_batch_size
        point_buffer: list[IncomingPricePoint] = []

        try:
            for obs in observations:
                total_rows += 1
                if first_observed_date is None:
                    first_observed_date = obs.observed_date
                    batch_row.data_date = obs.observed_date

                yq = (obs.observed_date.year, ((obs.observed_date.month - 1) // 3) + 1)
                if yq not in ensured_quarters_dates:
                    conn = self._session.connection()
                    ensure_partitions_window(
                        conn,
                        anchor_date=obs.observed_date,
                        quarters_back=1,
                        quarters_ahead=self._settings.precreate_quarters_ahead,
                        fillfactor=self._settings.hot_partition_fillfactor,
                    )
                    ensured_quarters_dates.add(yq)

                prov_id = cat_repo.resolve_province_id(obs.province_code)
                if prov_id is None:
                    logger.warning(
                        "Skipping observation with unrecognized province '%s' at pos=%s",
                        obs.province_code,
                        obs.pos_code,
                    )
                    continue

                pos_id, chain_id = cat_repo.get_or_create_pos_id(obs, prov_id)
                article_id = cat_repo.resolve_or_create_article_id(obs, source_id, chain_id)
                eff_promo_cents, promo_rule_id = cat_repo.resolve_promo_rule(
                    list_price_cents=obs.list_price_cents,
                    promo_price_cents=obs.promo_price_cents,
                    promo_legend=obs.promo_legend,
                )

                point_buffer.append(
                    IncomingPricePoint(
                        article_id=article_id,
                        pos_id=pos_id,
                        source_id=source_id,
                        observed_date=obs.observed_date,
                        list_price_cents=obs.list_price_cents,
                        promo_price_cents=eff_promo_cents,
                        promo_rule_id=promo_rule_id,
                        in_stock=obs.in_stock,
                    )
                )

                if len(point_buffer) >= chunk_size:
                    ext, ins = self._flush_points(
                        span_repo,
                        point_buffer,
                        historical_backfill=historical_backfill,
                    )
                    total_extended += ext
                    total_inserted += ins
                    point_buffer.clear()

            if point_buffer:
                ext, ins = self._flush_points(
                    span_repo,
                    point_buffer,
                    historical_backfill=historical_backfill,
                )
                total_extended += ext
                total_inserted += ins
                point_buffer.clear()

            batch_row.status = "succeeded"
            batch_row.rows_read = total_rows
            batch_row.spans_extended = total_extended
            batch_row.spans_inserted = total_inserted
            batch_row.new_articles_count = cat_repo.new_articles_created
            batch_row.completed_at = datetime.now(UTC)
            self._session.commit()

            logger.info(
                "Ingested batch %s (%s): rows=%d extended=%d inserted=%d new_articles=%d",
                batch_row.id,
                source_channel.value,
                total_rows,
                total_extended,
                total_inserted,
                cat_repo.new_articles_created,
            )
            return IngestionRunResult(
                batch_id=batch_row.id,
                skipped_duplicate=False,
                source_uri=source_uri,
                data_date=batch_row.data_date,
                rows_read=total_rows,
                spans_extended=total_extended,
                spans_inserted=total_inserted,
                new_articles_count=cat_repo.new_articles_created,
            )
        except Exception as exc:
            self._session.rollback()
            # Record failed batch state in a clean transaction
            with Session(self._session.get_bind()) as err_session:
                err_row = err_session.scalar(
                    select(IngestionBatchModel).where(
                        IngestionBatchModel.source_id == source_id,
                        IngestionBatchModel.sha256_digest == sha256_digest,
                    )
                )
                if err_row is None:
                    err_row = IngestionBatchModel(
                        source_id=source_id,
                        source_uri=source_uri[:500],
                        sha256_digest=sha256_digest,
                        data_date=first_observed_date or date.today(),
                        status="failed",
                        error_message=str(exc),
                        completed_at=datetime.now(UTC),
                    )
                    err_session.add(err_row)
                else:
                    err_row.status = "failed"
                    err_row.error_message = str(exc)
                    err_row.completed_at = datetime.now(UTC)
                err_session.commit()
            raise

    def _flush_points(
        self,
        span_repo: PostgresSpanRepository,
        points: list[IncomingPricePoint],
        historical_backfill: bool,
    ) -> tuple[int, int]:
        if historical_backfill:
            return span_repo.upsert_historical_points(
                points,
                max_bridge_gap_days=self._settings.max_bridge_gap_days,
                chunk_size=self._settings.upsert_batch_size,
            )

        keys = [
            SpanKey(article_id=p.article_id, pos_id=p.pos_id, source_id=p.source_id) for p in points
        ]
        open_spans = span_repo.fetch_latest_spans(keys)
        transitions = compute_incremental_transitions(
            open_spans=open_spans,
            incoming=points,
            max_bridge_gap_days=self._settings.max_bridge_gap_days,
        )
        return span_repo.apply_transition_batch(
            transitions,
            chunk_size=self._settings.upsert_batch_size,
        )


__all__ = [
    "IngestSourceBatchUseCase",
    "IngestionRunResult",
    "compute_directory_or_file_sha256",
]
