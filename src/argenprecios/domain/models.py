"""Immutable domain DTOs for ingestion, span compression, search, baskets, and CPI analytics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from argenprecios.domain.enums import (
    CanonicalUnit,
    CurationStatus,
    GeoLevel,
    PosChannel,
    PromoMechanism,
    SourceChannel,
    WeightingScheme,
)


@dataclass(frozen=True, slots=True)
class RawPriceObservation:
    """Canonical observation emitted by SEPA, Web Scrape, or Offline source adapters."""

    observed_date: date
    source_channel: SourceChannel
    pos_code: str
    pos_channel: PosChannel
    chain_name: str
    branch_name: str
    city: str
    province_code: str
    list_price_cents: int
    promo_price_cents: int | None = None
    promo_legend: str | None = None
    promo_mechanism: PromoMechanism = PromoMechanism.NONE
    in_stock: bool = True
    ean: str | None = None
    external_sku: str | None = None
    generic_variety_slug: str | None = None
    raw_description: str = ""
    raw_brand: str | None = None
    raw_package: str | None = None
    package_amount: float | None = None
    package_unit: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    currency_code: str = "ARS"


@dataclass(frozen=True, slots=True)
class SpanKey:
    """Composite natural identity of a store-article-source price timeline."""

    article_id: int
    pos_id: int
    source_id: int


@dataclass(frozen=True, slots=True)
class OpenSpanState:
    """Latest active span state stored in price_spans for incremental HOT-update comparison."""

    article_id: int
    pos_id: int
    source_id: int
    valid_from: date
    last_seen: date
    observed_days: int
    list_price_cents: int
    promo_price_cents: int | None
    promo_rule_id: int | None
    in_stock: bool


@dataclass(frozen=True, slots=True)
class IncomingPricePoint:
    """Resolved, integer-keyed daily observation ready for temporal span compression."""

    article_id: int
    pos_id: int
    source_id: int
    observed_date: date
    list_price_cents: int
    promo_price_cents: int | None
    promo_rule_id: int | None
    in_stock: bool = True


@dataclass(frozen=True, slots=True)
class SpanExtensionOp:
    """Instruction to extend an existing price_spans row in place (PostgreSQL HOT update)."""

    article_id: int
    pos_id: int
    source_id: int
    valid_from: date
    new_last_seen: date
    new_observed_days: int


@dataclass(frozen=True, slots=True)
class NewSpanRecord:
    """Instruction to insert a new contiguous price_spans interval."""

    article_id: int
    pos_id: int
    source_id: int
    valid_from: date
    last_seen: date
    observed_days: int
    list_price_cents: int
    promo_price_cents: int | None
    promo_rule_id: int | None
    in_stock: bool


@dataclass(frozen=True, slots=True)
class SpanDeleteOp:
    """Instruction to remove an existing span when splitting/merging historical backfills."""

    article_id: int
    pos_id: int
    source_id: int
    valid_from: date


@dataclass(frozen=True, slots=True)
class SpanTransitionBatch:
    """Deterministic batch of mutations produced by the temporal span compressor."""

    extensions: tuple[SpanExtensionOp, ...]
    inserts: tuple[NewSpanRecord, ...]
    deletes: tuple[SpanDeleteOp, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """Query and facet filters for national catalog search."""

    query: str = ""
    department_id: int | None = None
    subcategory_id: int | None = None
    brand_id: int | None = None
    tag_slug: str | None = None
    package_unit: str | None = None
    curation_status: CurationStatus | None = None
    min_active_provinces: int = 0
    generic_variety_id: int | None = None
    reference_date: date | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class FacetBucket:
    """Count bucket for a single facet value."""

    id_or_slug: int | str
    label: str
    count: int
    parent_id: int | None = None


@dataclass(frozen=True, slots=True)
class ArticleSearchHit:
    """Enriched article row returned by national search with geographic availability badges."""

    article_id: int
    ean: str | None
    name: str
    clean_description: str
    brand_id: int | None
    brand_name: str | None
    package: str | None
    package_amount: float | None
    package_unit: str | None
    unit_conversion_factor: float | None
    department_id: int | None
    department_name: str | None
    subcategory_id: int | None
    subcategory_name: str | None
    generic_variety_id: int | None
    generic_variety_slug: str | None
    is_reviewed: bool
    is_manually_curated: bool
    tags: tuple[str, ...] = ()
    active_provinces_count: int = 0
    active_cities_count: int = 0
    active_chains_count: int = 0
    latest_ref_price_cents: int | None = None


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    """Paginated article search response with 2-level category, brand, and tag facets."""

    items: tuple[ArticleSearchHit, ...]
    total_count: int
    page: int
    page_size: int
    department_facets: tuple[FacetBucket, ...] = ()
    subcategory_facets: tuple[FacetBucket, ...] = ()
    brand_facets: tuple[FacetBucket, ...] = ()
    tag_facets: tuple[FacetBucket, ...] = ()


@dataclass(frozen=True, slots=True)
class BasketItemSpec:
    """Single item in a consumer basket (100% compatible with sur_en_precios YAML + 8-year fallbacks)."""

    ean: str | None
    quantity: int = 1
    selected: bool = True
    numerator: int = 1
    denominator: int = 1
    name: str = ""
    brand: str | None = None
    category: str | None = None
    substitute_eans: tuple[str, ...] = ()
    fallback_variety_slug: str | None = None
    target_amount: float | None = None
    target_unit: CanonicalUnit | None = None

    @property
    def effective_multiplier(self) -> float:
        denom = self.denominator if self.denominator > 0 else 1
        return float(self.quantity) * (float(self.numerator) / float(denom))


@dataclass(frozen=True, slots=True)
class BasketDefinition:
    """Portable basket definition supporting YAML round-trip and warehouse persistence."""

    name: str
    uuid: str
    is_public: bool = True
    created_at: str = ""
    items: tuple[BasketItemSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluatedBasketItem:
    """Item-level resolution details for a basket evaluation on a specific date and geography."""

    requested_ean: str | None
    resolved_via: str  # "primary_ean" | "substitute_ean" | "generic_variety" | "missing"
    resolved_identifier: str | None
    effective_multiplier: float
    min_line_cents: int | None
    geo_mean_line_cents: int | None
    max_line_cents: int | None
    observing_pos_count: int


@dataclass(frozen=True, slots=True)
class BasketGeoEvaluation:
    """Aggregated basket cost metrics for a date and geographic scope."""

    basket_uuid: str
    basket_name: str
    evaluated_date: date
    geo_level: GeoLevel
    geo_code: str
    geo_name: str
    weighting: WeightingScheme
    total_items_count: int
    matched_items_count: int
    coverage_ratio: float
    min_total_cents: int | None
    geo_mean_reference_total_cents: int | None
    max_total_cents: int | None
    best_single_store_cents: int | None
    best_single_store_code: str | None
    worst_single_store_cents: int | None
    complete_stores_count: int
    items: tuple[EvaluatedBasketItem, ...] = ()


@dataclass(frozen=True, slots=True)
class VarietyRollupCell:
    """Elementary Jevons cell metrics for (date, geo_level, geo_code, generic_variety_id, channel_scope)."""

    rollup_date: date
    geo_level: GeoLevel
    geo_code: str
    generic_variety_id: int
    channel_scope: str
    active_pos_count: int
    in_stock_pos_count: int
    distinct_articles_count: int
    geo_mean_unit_list_cents: int
    geo_mean_unit_effective_cents: int
    min_unit_effective_cents: int
    median_unit_effective_cents: int
    max_unit_effective_cents: int
    iqr_unit_effective_cents: int
    promo_pos_count: int
    promo_gap_pct: float
    stockout_rate: float


@dataclass(frozen=True, slots=True)
class PriceIndexPoint:
    """Monthly or daily chained price index observation."""

    period_date: date
    geo_level: GeoLevel
    geo_code: str
    category_id: int | None
    channel_scope: str
    weighting: WeightingScheme
    index_value: float
    period_pct_change: float | None
    yoy_pct_change: float | None
    overlapping_varieties_count: int


@dataclass(frozen=True, slots=True)
class PeriodComparisonMetrics:
    """Cross-government inflation, volatility, wage purchasing power, and structural diagnostics."""

    period_slug: str
    period_name: str
    administration_label: str
    start_date: date
    end_date: date
    observed_start_date: date | None
    observed_end_date: date | None
    months_observed: float
    cumulative_inflation_pct: float | None
    cmgr_pct: float | None
    annualized_inflation_pct: float | None
    monthly_volatility_pct: float | None
    official_indec_cumulative_pct: float | None
    official_gap_percentage_points: float | None
    ripte_real_purchasing_power_change_pct: float | None
    avg_promo_gap_pct: float | None
    avg_stockout_rate_pct: float | None
    patagonia_vs_gba_spread_pct: float | None
    extra_diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionRunResult:
    """Audit metrics returned after ingesting a source file or observation stream."""

    batch_id: int | None
    skipped_duplicate: bool
    source_uri: str
    data_date: date
    rows_read: int
    spans_extended: int
    spans_inserted: int
    new_articles_count: int


__all__ = [
    "ArticleSearchHit",
    "BasketDefinition",
    "BasketGeoEvaluation",
    "BasketItemSpec",
    "EvaluatedBasketItem",
    "FacetBucket",
    "IncomingPricePoint",
    "IngestionRunResult",
    "NewSpanRecord",
    "OpenSpanState",
    "PeriodComparisonMetrics",
    "PriceIndexPoint",
    "RawPriceObservation",
    "SearchFilter",
    "SearchPageResult",
    "SpanDeleteOp",
    "SpanExtensionOp",
    "SpanKey",
    "SpanTransitionBatch",
    "VarietyRollupCell",
]
