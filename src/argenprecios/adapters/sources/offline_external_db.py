"""Declarative external SQLite (?mode=ro) and Parquet historical dataset importer."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq
import yaml
from pydantic import BaseModel, ConfigDict, Field

from argenprecios.config import resolve_user_path
from argenprecios.domain.enums import PosChannel, SourceChannel
from argenprecios.domain.models import RawPriceObservation
from argenprecios.domain.promo_parser import evaluate_promotion, parse_ars_to_cents
from argenprecios.domain.unit_normalizer import slugify_text

logger = logging.getLogger(__name__)


class ExternalDbImportProfile(BaseModel):
    """Declarative YAML column-mapping profile for external SQLite or Parquet datasets."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["sqlite", "parquet"]
    sql_query: str | None = None
    default_province_code: str = "AR-U"
    default_city: str = "General"
    default_chain_name: str = "Importación Histórica"
    default_pos_channel: PosChannel = PosChannel.PHYSICAL_SUPERMARKET
    price_in_cents: bool = False
    column_map: dict[str, str] = Field(default_factory=dict)


def load_external_import_profile(profile_path: Path) -> ExternalDbImportProfile:
    """Load and validate an ExternalDbImportProfile from YAML."""
    resolved = resolve_user_path(profile_path)
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    return ExternalDbImportProfile.model_validate(raw)


class ExternalDbAdapter:
    """Streams observations from legacy SQLite (?mode=ro) or Parquet files via mapping profile."""

    def __init__(self, profile: ExternalDbImportProfile) -> None:
        self._profile = profile

    @property
    def source_channel(self) -> SourceChannel:
        return SourceChannel.OFFLINE_DB_IMPORT

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]:
        resolved = resolve_user_path(source_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"External dataset file not found: {resolved}")

        raw_iter = (
            self._iter_sqlite_readonly(resolved)
            if self._profile.format == "sqlite"
            else self._iter_parquet(resolved)
        )
        for row in raw_iter:
            obs = self._map_row(row)
            if obs is not None:
                yield obs

    def _iter_sqlite_readonly(self, db_path: Path) -> Iterator[dict[str, Any]]:
        if not self._profile.sql_query:
            raise ValueError("ExternalDbImportProfile.sql_query is required for format='sqlite'")
        ro_uri = f"{db_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(ro_uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(self._profile.sql_query)
            while batch := cursor.fetchmany(5_000):
                for r in batch:
                    yield dict(r)
        finally:
            conn.close()

    def _iter_parquet(self, parquet_path: Path) -> Iterator[dict[str, Any]]:
        pf = pq.ParquetFile(parquet_path)
        for record_batch in pf.iter_batches(batch_size=10_000):
            yield from record_batch.to_pylist()

    def _get_col(self, row: Mapping[str, Any], canonical_field: str) -> Any:
        mapped_col = self._profile.column_map.get(canonical_field, canonical_field)
        return row.get(mapped_col)

    def _map_row(self, row: Mapping[str, Any]) -> RawPriceObservation | None:
        raw_dt = self._get_col(row, "observed_date")
        if raw_dt is None:
            return None
        obs_date = (
            raw_dt if isinstance(raw_dt, date) else date.fromisoformat(str(raw_dt).strip()[:10])
        )

        raw_list = self._get_col(row, "list_price")
        if raw_list is None:
            return None
        list_cents = int(raw_list) if self._profile.price_in_cents else parse_ars_to_cents(raw_list)
        if list_cents is None or list_cents <= 0:
            return None

        raw_promo = self._get_col(row, "promo_price")
        promo_explicit = (
            int(raw_promo)
            if (raw_promo is not None and self._profile.price_in_cents)
            else parse_ars_to_cents(raw_promo)
        )
        promo = evaluate_promotion(
            list_price_cents=list_cents,
            explicit_promo_cents=promo_explicit,
            raw_legend=(
                str(self._get_col(row, "promo_legend"))
                if self._get_col(row, "promo_legend")
                else None
            ),
        )

        prov = str(
            self._get_col(row, "province_code") or self._profile.default_province_code
        ).strip()
        city = str(self._get_col(row, "city") or self._profile.default_city).strip()
        chain_name = str(
            self._get_col(row, "chain_name") or self._profile.default_chain_name
        ).strip()
        branch_name = str(self._get_col(row, "branch_name") or chain_name).strip()
        pos_code = str(
            self._get_col(row, "pos_code")
            or f"EXT:{prov.upper()}:{slugify_text(city)}:{slugify_text(branch_name)}"
        )[:80]

        ean_val = self._get_col(row, "ean")
        sku_val = self._get_col(row, "external_sku")
        desc_val = str(self._get_col(row, "raw_description") or ean_val or "Artículo").strip()

        return RawPriceObservation(
            observed_date=obs_date,
            source_channel=SourceChannel.OFFLINE_DB_IMPORT,
            pos_code=pos_code,
            pos_channel=self._profile.default_pos_channel,
            chain_name=chain_name,
            branch_name=branch_name,
            city=city,
            province_code=prov,
            ean=str(ean_val).strip() if ean_val else None,
            external_sku=str(sku_val).strip() if sku_val else None,
            raw_description=desc_val,
            raw_brand=(
                str(self._get_col(row, "raw_brand")).strip()
                if self._get_col(row, "raw_brand")
                else None
            ),
            raw_package=(
                str(self._get_col(row, "raw_package")).strip()
                if self._get_col(row, "raw_package")
                else None
            ),
            list_price_cents=list_cents,
            promo_price_cents=promo.promo_price_cents,
            promo_legend=promo.raw_legend,
            promo_mechanism=promo.mechanism,
            in_stock=True,
        )


__all__ = [
    "ExternalDbAdapter",
    "ExternalDbImportProfile",
    "load_external_import_profile",
]
