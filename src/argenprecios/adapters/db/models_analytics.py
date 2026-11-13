"""SQLAlchemy 2.0 models for consumer baskets, variety rollups, CPI series, macro benchmarks, and auth."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from argenprecios.adapters.db.base import WarehouseBase


class BasketModel(WarehouseBase):
    """Saved consumer basket compatible with sur_en_precios YAML and national evaluation."""

    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    items: Mapped[list[BasketItemModel]] = relationship(
        "BasketItemModel",
        back_populates="basket",
        cascade="all, delete-orphan",
        order_by="BasketItemModel.sort_order",
    )


class BasketItemModel(WarehouseBase):
    """Line item in a consumer basket with fractional quantity and 8-year variety fallback."""

    __tablename__ = "basket_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    basket_id: Mapped[int] = mapped_column(
        ForeignKey("baskets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ean: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    fallback_generic_variety_id: Mapped[int | None] = mapped_column(
        ForeignKey("generic_varieties.id", ondelete="SET NULL"),
        nullable=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    numerator: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    denominator: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    display_brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_category: Mapped[str | None] = mapped_column(String(120), nullable=True)

    basket: Mapped[BasketModel] = relationship("BasketModel", back_populates="items")
    substitutes: Mapped[list[BasketItemSubstituteModel]] = relationship(
        "BasketItemSubstituteModel",
        back_populates="basket_item",
        cascade="all, delete-orphan",
        order_by="BasketItemSubstituteModel.priority",
    )


class BasketItemSubstituteModel(WarehouseBase):
    """Ordered substitute EANs/articles when primary SKU is unavailable in a province or year."""

    __tablename__ = "basket_item_substitutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    basket_item_id: Mapped[int] = mapped_column(
        ForeignKey("basket_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    substitute_ean: Mapped[str] = mapped_column(String(32), nullable=False)
    substitute_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
    )

    basket_item: Mapped[BasketItemModel] = relationship(
        "BasketItemModel",
        back_populates="substitutes",
    )


class BasketEvaluationModel(WarehouseBase):
    """Persisted multi-level basket valuation snapshot (City, Province, Region, Country)."""

    __tablename__ = "basket_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "basket_id",
            "evaluated_date",
            "geo_level",
            "geo_code",
            "weighting",
            name="uq_basket_evaluations_scope",
        ),
        Index("ix_basket_eval_date_geo", "evaluated_date", "geo_level", "geo_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    basket_id: Mapped[int] = mapped_column(
        ForeignKey("baskets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evaluated_date: Mapped[date] = mapped_column(Date, nullable=False)
    geo_level: Mapped[str] = mapped_column(String(24), nullable=False)
    geo_code: Mapped[str] = mapped_column(String(40), nullable=False)
    geo_name: Mapped[str] = mapped_column(String(120), nullable=False)
    weighting: Mapped[str] = mapped_column(String(24), nullable=False, default="expenditure")

    total_items_count: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_items_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    min_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    geo_mean_reference_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    best_single_store_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    best_single_store_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worst_single_store_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    complete_stores_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    item_details: Mapped[list[BasketEvaluationItemModel]] = relationship(
        "BasketEvaluationItemModel",
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )


class BasketEvaluationItemModel(WarehouseBase):
    """Item-level valuation breakdown inside a BasketEvaluationModel."""

    __tablename__ = "basket_evaluation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("basket_evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_ean: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_via: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_identifier: Mapped[str | None] = mapped_column(String(160), nullable=True)
    effective_multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    min_line_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    geo_mean_line_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_line_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observing_pos_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    evaluation: Mapped[BasketEvaluationModel] = relationship(
        "BasketEvaluationModel",
        back_populates="item_details",
    )


class DailyGeoVarietyRollupModel(WarehouseBase):
    """Stage 1 Jevons elementary geometric mean unit-price rollup per day and geography."""

    __tablename__ = "daily_geo_variety_rollups"
    __table_args__ = (
        UniqueConstraint(
            "rollup_date",
            "geo_level",
            "geo_code",
            "generic_variety_id",
            "channel_scope",
            name="uq_daily_geo_variety_rollups_cell",
        ),
        Index("ix_daily_rollup_geo_date", "geo_level", "geo_code", "rollup_date"),
        Index("ix_daily_rollup_variety_date", "generic_variety_id", "rollup_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rollup_date: Mapped[date] = mapped_column(Date, nullable=False)
    geo_level: Mapped[str] = mapped_column(String(24), nullable=False)
    geo_code: Mapped[str] = mapped_column(String(40), nullable=False)
    generic_variety_id: Mapped[int] = mapped_column(
        ForeignKey("generic_varieties.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="all")

    active_pos_count: Mapped[int] = mapped_column(Integer, nullable=False)
    in_stock_pos_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_articles_count: Mapped[int] = mapped_column(Integer, nullable=False)
    geo_mean_unit_list_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    geo_mean_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    median_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    iqr_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    promo_pos_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    promo_gap_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stockout_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class MonthlyGeoVarietyRollupModel(WarehouseBase):
    """Monthly geometric mean variety cell used for chained Young/Laspeyres CPI linking."""

    __tablename__ = "monthly_geo_variety_rollups"
    __table_args__ = (
        UniqueConstraint(
            "month_start",
            "geo_level",
            "geo_code",
            "generic_variety_id",
            "channel_scope",
            name="uq_monthly_geo_variety_rollups_cell",
        ),
        Index("ix_monthly_rollup_geo_month", "geo_level", "geo_code", "month_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    month_start: Mapped[date] = mapped_column(Date, nullable=False)
    geo_level: Mapped[str] = mapped_column(String(24), nullable=False)
    geo_code: Mapped[str] = mapped_column(String(40), nullable=False)
    generic_variety_id: Mapped[int] = mapped_column(
        ForeignKey("generic_varieties.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="all")

    days_observed: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_active_pos_count: Mapped[float] = mapped_column(Float, nullable=False)
    geo_mean_unit_list_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    geo_mean_unit_effective_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    avg_promo_gap_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_stockout_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class PriceIndexSeriesModel(WarehouseBase):
    """Chained category, regional, and national CPI index series."""

    __tablename__ = "price_index_series"
    __table_args__ = (
        UniqueConstraint(
            "period_date",
            "frequency",
            "geo_level",
            "geo_code",
            "category_scope",
            "channel_scope",
            "weighting",
            name="uq_price_index_series_point",
        ),
        Index("ix_price_index_geo_date", "geo_level", "geo_code", "period_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_date: Mapped[date] = mapped_column(Date, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    geo_level: Mapped[str] = mapped_column(String(24), nullable=False)
    geo_code: Mapped[str] = mapped_column(String(40), nullable=False)
    category_scope: Mapped[str] = mapped_column(String(140), nullable=False, default="ALL")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="all")
    weighting: Mapped[str] = mapped_column(String(24), nullable=False, default="expenditure")

    index_value: Mapped[float] = mapped_column(Float, nullable=False)
    period_pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    yoy_pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    overlapping_varieties_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GovernmentPeriodModel(WarehouseBase):
    """Presidential administration or macroeconomic sub-period benchmark window."""

    __tablename__ = "government_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    administration_label: Mapped[str] = mapped_column(String(120), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="presidential_term"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MacroReferenceSeriesModel(WarehouseBase):
    """Official offline macroeconomic series (INDEC IPC, CBA, CBT, RIPTE, USD FX)."""

    __tablename__ = "macro_reference_series"
    __table_args__ = (
        UniqueConstraint(
            "series_code",
            "geo_code",
            "period_date",
            name="uq_macro_reference_series_obs",
        ),
        Index("ix_macro_series_code_date", "series_code", "period_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_code: Mapped[str] = mapped_column(String(64), nullable=False)
    series_name: Mapped[str] = mapped_column(String(160), nullable=False)
    geo_code: Mapped[str] = mapped_column(String(40), nullable=False, default="AR")
    period_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="index")
    source_label: Mapped[str] = mapped_column(String(120), nullable=False, default="INDEC")


class StagedObservationModel(WarehouseBase):
    """Staging queue for semi-structured receipt, PDF, or crowdsourced price captures."""

    __tablename__ = "staged_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_channel: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="offline_crowdsourced",
    )
    province_code: Mapped[str] = mapped_column(String(10), nullable=False)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    pos_name: Mapped[str] = mapped_column(String(160), nullable=False)
    pos_channel: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="neighborhood_shop",
    )
    ean_or_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_description: Mapped[str] = mapped_column(Text, nullable=False)
    list_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promo_price_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class AdminUserModel(WarehouseBase):
    """Intranet administrator account protecting Catalog Editor and Basket Studio mutations."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    api_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


__all__ = [
    "AdminUserModel",
    "BasketEvaluationItemModel",
    "BasketEvaluationModel",
    "BasketItemModel",
    "BasketItemSubstituteModel",
    "BasketModel",
    "DailyGeoVarietyRollupModel",
    "GovernmentPeriodModel",
    "MacroReferenceSeriesModel",
    "MonthlyGeoVarietyRollupModel",
    "PriceIndexSeriesModel",
    "StagedObservationModel",
]
