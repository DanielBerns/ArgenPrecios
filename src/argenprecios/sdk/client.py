"""Typed Python Research SDK (`WarehouseClient`) for scripts, papers, and notebooks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy import Engine, select

from argenprecios.adapters.db import (
    DailyGeoVarietyRollupModel,
    GenericVarietyModel,
    PriceIndexSeriesModel,
    create_session_factory,
    create_warehouse_engine,
)
from argenprecios.adapters.db.repositories_search import PostgresCatalogSearchRepository
from argenprecios.config import AppConfig, load_config, load_regional_weights
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    BasketDefinition,
    BasketGeoEvaluation,
    PeriodComparisonMetrics,
    PriceIndexPoint,
    SearchFilter,
    SearchPageResult,
)
from argenprecios.use_cases.compute_rollups_and_cpi import CompareGovernmentPeriodsUseCase
from argenprecios.use_cases.evaluate_baskets import EvaluateBasketUseCase


class WarehouseClient:
    """High-level typed Python client over the ArgenPrecios PostgreSQL warehouse."""

    def __init__(self, config: AppConfig, engine: Engine | None = None) -> None:
        self._config = config
        self._owns_engine = engine is None
        self._engine = engine or create_warehouse_engine(config.database_url, config.database)
        self._session_factory = create_session_factory(self._engine)
        self._weights = load_regional_weights(config.analytics.regional_weights_path)

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        workdir: str | Path | None = None,
    ) -> WarehouseClient:
        cfg = load_config(config_path=config_path, workdir=workdir)
        return cls(cfg)

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()

    def __enter__(self) -> WarehouseClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def search_articles(
        self,
        query: str = "",
        *,
        department_id: int | None = None,
        subcategory_id: int | None = None,
        brand_id: int | None = None,
        min_active_provinces: int = 0,
        page: int = 1,
        page_size: int = 25,
    ) -> SearchPageResult:
        with self._session_factory() as session:
            repo = PostgresCatalogSearchRepository(session)
            return repo.search_articles(
                SearchFilter(
                    query=query,
                    department_id=department_id,
                    subcategory_id=subcategory_id,
                    brand_id=brand_id,
                    min_active_provinces=min_active_provinces,
                    page=page,
                    page_size=page_size,
                )
            )

    def evaluate_basket(
        self,
        basket: str | BasketDefinition,
        evaluated_date: date,
        *,
        geo_level: GeoLevel = GeoLevel.COUNTRY,
        geo_code: str = "AR",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    ) -> BasketGeoEvaluation:
        with self._session_factory() as session:
            uc = EvaluateBasketUseCase(session, self._weights)
            b_def = uc.load_basket_definition(basket) if isinstance(basket, str) else basket
            return uc.evaluate_on_date(
                basket=b_def,
                evaluated_date=evaluated_date,
                geo_level=geo_level,
                geo_code=geo_code,
                weighting=weighting,
                persist=False,
            )

    def get_variety_series(
        self,
        variety_slug: str,
        *,
        geo_level: GeoLevel = GeoLevel.COUNTRY,
        geo_code: str = "AR",
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            stmt = (
                select(DailyGeoVarietyRollupModel, GenericVarietyModel.canonical_unit)
                .join(
                    GenericVarietyModel,
                    GenericVarietyModel.id == DailyGeoVarietyRollupModel.generic_variety_id,
                )
                .where(
                    GenericVarietyModel.slug == variety_slug,
                    DailyGeoVarietyRollupModel.geo_level == geo_level.value,
                    DailyGeoVarietyRollupModel.geo_code == geo_code.upper(),
                )
                .order_by(DailyGeoVarietyRollupModel.rollup_date.asc())
            )
            if start_date is not None:
                stmt = stmt.where(DailyGeoVarietyRollupModel.rollup_date >= start_date)
            if end_date is not None:
                stmt = stmt.where(DailyGeoVarietyRollupModel.rollup_date <= end_date)

            return [
                {
                    "date": r.rollup_date,
                    "variety_slug": variety_slug,
                    "canonical_unit": c_unit,
                    "geo_mean_unit_list_ars": r.geo_mean_unit_list_cents / 100.0,
                    "geo_mean_unit_effective_ars": r.geo_mean_unit_effective_cents / 100.0,
                    "active_pos_count": r.active_pos_count,
                    "promo_gap_pct": r.promo_gap_pct,
                    "stockout_rate": r.stockout_rate,
                }
                for r, c_unit in session.execute(stmt).all()
            ]

    def get_cpi_series(
        self,
        *,
        geo_level: GeoLevel = GeoLevel.COUNTRY,
        geo_code: str = "AR",
        frequency: str = "monthly",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    ) -> list[PriceIndexPoint]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(PriceIndexSeriesModel)
                .where(
                    PriceIndexSeriesModel.frequency == frequency,
                    PriceIndexSeriesModel.geo_level == geo_level.value,
                    PriceIndexSeriesModel.geo_code == geo_code.upper(),
                    PriceIndexSeriesModel.weighting == weighting.value,
                )
                .order_by(PriceIndexSeriesModel.period_date.asc())
            ).all()
            return [
                PriceIndexPoint(
                    period_date=r.period_date,
                    geo_level=geo_level,
                    geo_code=r.geo_code,
                    category_id=r.category_id,
                    channel_scope=r.channel_scope,
                    weighting=weighting,
                    index_value=r.index_value,
                    period_pct_change=r.period_pct_change,
                    yoy_pct_change=r.yoy_pct_change,
                    overlapping_varieties_count=r.overlapping_varieties_count,
                )
                for r in rows
            ]

    def compare_government_periods(
        self,
        period_slugs: Sequence[str] | None = None,
        *,
        frequency: str = "monthly",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    ) -> list[PeriodComparisonMetrics]:
        with self._session_factory() as session:
            uc = CompareGovernmentPeriodsUseCase(session)
            return uc.compare_periods(
                period_slugs=period_slugs,
                frequency=frequency,
                weighting=weighting,
            )


__all__ = [
    "WarehouseClient",
]
