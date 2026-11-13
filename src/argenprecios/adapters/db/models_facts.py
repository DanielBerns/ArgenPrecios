"""SQLAlchemy 2.0 models for provenance, monetary regimes, promo rules, and partitioned price_spans."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from argenprecios.adapters.db.base import WarehouseBase


class DataSourceModel(WarehouseBase):
    """Canonical provenance channel (sepa, web_scrape, hermes, offline_*)."""

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class IngestionBatchModel(WarehouseBase):
    """Idempotent audit log tracking ingested archives/files via SHA-256 digests."""

    __tablename__ = "ingestion_batches"
    __table_args__ = (
        UniqueConstraint("source_id", "sha256_digest", name="uq_ingestion_batches_source_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spans_extended: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spans_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_articles_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoRuleModel(WarehouseBase):
    """Deduplicated promotional legend and discount mechanism lookup."""

    __tablename__ = "promo_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    mechanism: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    raw_legend: Mapped[str] = mapped_column(String(255), nullable=False)
    discount_factor: Mapped[float | None] = mapped_column(Float, nullable=True)


class MonetaryRegimeModel(WarehouseBase):
    """Tracks Argentine currency regimes across 8+ years in case of future redenomination."""

    __tablename__ = "monetary_regimes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    currency_code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    factor_to_base_ars: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class PriceSpanModel(WarehouseBase):
    """Quarterly range-partitioned lossless temporal price intervals (<500 GB over 8+ years)."""

    __tablename__ = "price_spans"
    __table_args__ = (
        Index("ix_price_spans_article_valid_from", "article_id", "valid_from"),
        Index("ix_price_spans_pos_last_seen", "pos_id", "last_seen"),
        {"postgresql_partition_by": "RANGE (valid_from)"},
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    pos_id: Mapped[int] = mapped_column(
        ForeignKey("points_of_sale.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)

    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    observed_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    list_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promo_price_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    promo_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    in_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ChainCitySpanModel(WarehouseBase):
    """Cold-storage compaction safety valve aggregating identical chain-city branch price lists."""

    __tablename__ = "chain_city_spans"
    __table_args__ = (Index("ix_chain_city_spans_article_valid_from", "article_id", "valid_from"),)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("chains.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    pos_channel: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)

    last_seen: Mapped[date] = mapped_column(Date, nullable=False)
    observed_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    branch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    list_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promo_price_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


__all__ = [
    "ChainCitySpanModel",
    "DataSourceModel",
    "IngestionBatchModel",
    "MonetaryRegimeModel",
    "PriceSpanModel",
    "PromoRuleModel",
]
