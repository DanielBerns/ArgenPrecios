"""Use cases computing daily/monthly geo variety rollups, national CPI series, & period comparisons."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_analytics import (
    DailyGeoVarietyRollupModel,
    GovernmentPeriodModel,
    MacroReferenceSeriesModel,
    MonthlyGeoVarietyRollupModel,
    PriceIndexSeriesModel,
)
from argenprecios.adapters.db.models_catalog import ArticleModel
from argenprecios.adapters.db.models_facts import PriceSpanModel
from argenprecios.adapters.db.models_geo import PointOfSaleModel, ProvinceModel
from argenprecios.config import RegionalWeightsConfig
from argenprecios.domain.basket_math import geometric_mean_cents
from argenprecios.domain.cpi_engine import (
    UnitPriceSample,
    aggregate_weighted_geo_indices,
    compute_chained_jevons_series,
    compute_variety_rollup_cell,
    summarize_government_period,
)
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    PeriodComparisonMetrics,
    PriceIndexPoint,
    VarietyRollupCell,
)
from argenprecios.domain.unit_normalizer import compute_unit_price_cents

logger = logging.getLogger(__name__)


class ComputeRollupsAndCpiUseCase:
    """Materializes Stage 1 daily/monthly variety rollups and Stage 2 chained CPI index series."""

    def __init__(
        self,
        session: Session,
        regional_weights: RegionalWeightsConfig,
        base_index_value: float = 100.0,
    ) -> None:
        self._session = session
        self._regional_weights = regional_weights
        self._base_index = base_index_value

    def compute_daily_rollups(
        self,
        start_date: date,
        end_date: date,
        channel_scopes: Sequence[str] = ("all",),
    ) -> int:
        """Compute daily Jevons variety rollups across Provinces, 6 Regions, and Country (AR)."""
        prov_to_region = self._regional_weights.province_to_region_map()
        prov_id_to_code = {
            p.id: p.code.upper() for p in self._session.scalars(select(ProvinceModel))
        }

        total_cells = 0
        cursor = start_date
        while cursor <= end_date:
            span_rows = self._session.execute(
                select(
                    PriceSpanModel.pos_id,
                    PriceSpanModel.article_id,
                    PriceSpanModel.list_price_cents,
                    PriceSpanModel.promo_price_cents,
                    PriceSpanModel.in_stock,
                    ArticleModel.generic_variety_id,
                    ArticleModel.unit_conversion_factor,
                    PointOfSaleModel.province_id,
                    PointOfSaleModel.pos_channel,
                )
                .join(ArticleModel, ArticleModel.id == PriceSpanModel.article_id)
                .join(PointOfSaleModel, PointOfSaleModel.id == PriceSpanModel.pos_id)
                .where(
                    PriceSpanModel.valid_from <= cursor,
                    PriceSpanModel.last_seen >= cursor,
                    ArticleModel.generic_variety_id.is_not(None),
                )
            ).all()

            for scope in channel_scopes:
                buckets: dict[tuple[GeoLevel, str, int], list[UnitPriceSample]] = {}
                for (
                    pos_id,
                    art_id,
                    list_c,
                    promo_c,
                    in_stock,
                    var_id,
                    conv_factor,
                    prov_id,
                    pos_chan,
                ) in span_rows:
                    if scope != "all" and str(pos_chan) != scope:
                        continue
                    prov_code = prov_id_to_code.get(int(prov_id))
                    if prov_code is None or var_id is None:
                        continue

                    eff_pack = (
                        int(promo_c)
                        if (promo_c is not None and 0 < promo_c < list_c)
                        else int(list_c)
                    )
                    sample = UnitPriceSample(
                        pos_id=int(pos_id),
                        article_id=int(art_id),
                        unit_list_cents=compute_unit_price_cents(int(list_c), conv_factor),
                        unit_effective_cents=compute_unit_price_cents(eff_pack, conv_factor),
                        has_promo=(promo_c is not None and 0 < promo_c < list_c),
                        in_stock=bool(in_stock),
                    )
                    vid = int(var_id)
                    buckets.setdefault((GeoLevel.PROVINCE, prov_code, vid), []).append(sample)
                    reg_code = prov_to_region.get(prov_code)
                    if reg_code:
                        buckets.setdefault((GeoLevel.REGION, reg_code, vid), []).append(sample)
                    buckets.setdefault((GeoLevel.COUNTRY, "AR", vid), []).append(sample)

                cells: list[VarietyRollupCell] = []
                for (g_lvl, g_code, vid), samp_list in buckets.items():
                    cell = compute_variety_rollup_cell(
                        rollup_date=cursor,
                        geo_level=g_lvl,
                        geo_code=g_code,
                        generic_variety_id=vid,
                        channel_scope=scope,
                        samples=samp_list,
                    )
                    if cell is not None:
                        cells.append(cell)

                total_cells += self._upsert_daily_cells(cells)

            cursor += timedelta(days=1)

        self._session.commit()
        return total_cells

    def compute_monthly_rollups_and_cpi(
        self,
        channel_scope: str = "all",
        frequency: str = "monthly",
    ) -> int:
        """Aggregate daily variety rollups into monthly cells and chained Regional/National CPI."""
        # 1. Materialize MonthlyGeoVarietyRollupModel from DailyGeoVarietyRollupModel
        daily_rows = self._session.scalars(
            select(DailyGeoVarietyRollupModel)
            .where(DailyGeoVarietyRollupModel.channel_scope == channel_scope)
            .order_by(DailyGeoVarietyRollupModel.rollup_date.asc())
        ).all()

        if not daily_rows:
            return 0

        monthly_groups: dict[tuple[date, str, str, int], list[DailyGeoVarietyRollupModel]] = {}
        for d_row in daily_rows:
            m_start = date(d_row.rollup_date.year, d_row.rollup_date.month, 1)
            key = (m_start, d_row.geo_level, d_row.geo_code, d_row.generic_variety_id)
            monthly_groups.setdefault(key, []).append(d_row)

        monthly_payloads: list[dict[str, object]] = []
        for (m_start, g_lvl, g_code, vid), grp in monthly_groups.items():
            monthly_payloads.append(
                {
                    "month_start": m_start,
                    "geo_level": g_lvl,
                    "geo_code": g_code,
                    "generic_variety_id": vid,
                    "channel_scope": channel_scope,
                    "days_observed": len(grp),
                    "avg_active_pos_count": sum(r.active_pos_count for r in grp) / len(grp),
                    "geo_mean_unit_list_cents": geometric_mean_cents(
                        [r.geo_mean_unit_list_cents for r in grp]
                    ),
                    "geo_mean_unit_effective_cents": geometric_mean_cents(
                        [r.geo_mean_unit_effective_cents for r in grp]
                    ),
                    "avg_promo_gap_pct": sum(r.promo_gap_pct for r in grp) / len(grp),
                    "avg_stockout_rate": sum(r.stockout_rate for r in grp) / len(grp),
                }
            )

        for i in range(0, len(monthly_payloads), 2_000):
            chunk = monthly_payloads[i : i + 2_000]
            ins = pg_insert(MonthlyGeoVarietyRollupModel).values(chunk)
            self._session.execute(
                ins.on_conflict_do_update(
                    constraint="uq_monthly_geo_variety_rollups_cell",
                    set_={
                        "days_observed": ins.excluded.days_observed,
                        "avg_active_pos_count": ins.excluded.avg_active_pos_count,
                        "geo_mean_unit_list_cents": ins.excluded.geo_mean_unit_list_cents,
                        "geo_mean_unit_effective_cents": ins.excluded.geo_mean_unit_effective_cents,
                        "avg_promo_gap_pct": ins.excluded.avg_promo_gap_pct,
                        "avg_stockout_rate": ins.excluded.avg_stockout_rate,
                    },
                )
            )
        self._session.flush()

        # 2. Build Provincial chained Jevons series (either monthly or daily)
        prov_series_map: dict[str, list[PriceIndexPoint]] = {}
        if frequency == "daily":
            by_prov_date: dict[str, dict[date, dict[int, int]]] = {}
            for d_row in daily_rows:
                if d_row.geo_level == GeoLevel.PROVINCE.value:
                    by_prov_date.setdefault(d_row.geo_code, {}).setdefault(d_row.rollup_date, {})[
                        d_row.generic_variety_id
                    ] = d_row.geo_mean_unit_effective_cents
            for p_code, dt_map in by_prov_date.items():
                prov_series_map[p_code] = compute_chained_jevons_series(
                    dt_map,
                    geo_level=GeoLevel.PROVINCE,
                    geo_code=p_code,
                    channel_scope=channel_scope,
                    weighting=WeightingScheme.UNWEIGHTED,
                    base_index=self._base_index,
                )
        else:
            by_prov_month: dict[str, dict[date, dict[int, int]]] = {}
            for mp in monthly_payloads:
                if mp["geo_level"] == GeoLevel.PROVINCE.value:
                    by_prov_month.setdefault(str(mp["geo_code"]), {}).setdefault(
                        mp["month_start"],  # type: ignore[arg-type]
                        {},
                    )[int(mp["generic_variety_id"])] = int(  # type: ignore[call-overload]
                        mp["geo_mean_unit_effective_cents"]
                    )
            for p_code, m_map in by_prov_month.items():
                prov_series_map[p_code] = compute_chained_jevons_series(
                    m_map,
                    geo_level=GeoLevel.PROVINCE,
                    geo_code=p_code,
                    channel_scope=channel_scope,
                    weighting=WeightingScheme.UNWEIGHTED,
                    base_index=self._base_index,
                )

        all_index_points: list[PriceIndexPoint] = []
        for pts in prov_series_map.values():
            all_index_points.extend(pts)

        # 3. Aggregate Provinces -> 6 INDEC Regions & Country (AR) under Expenditure & Unweighted
        for scheme in (WeightingScheme.EXPENDITURE, WeightingScheme.UNWEIGHTED):
            region_series_for_country: dict[str, list[PriceIndexPoint]] = {}
            region_weights_for_country: dict[str, float] = {}

            for reg in self._regional_weights.regions:
                r_code = reg.code.upper()
                region_weights_for_country[r_code] = reg.expenditure_weight
                child_provs = {
                    p.code.upper(): prov_series_map[p.code.upper()]
                    for p in reg.provinces
                    if p.code.upper() in prov_series_map
                }
                if not child_provs:
                    continue
                prov_w = {p.code.upper(): p.expenditure_share_within_region for p in reg.provinces}
                reg_pts = aggregate_weighted_geo_indices(
                    child_provs,
                    prov_w,
                    target_geo_level=GeoLevel.REGION,
                    target_geo_code=r_code,
                    channel_scope=channel_scope,
                    weighting=scheme,
                    base_index=self._base_index,
                )
                region_series_for_country[r_code] = reg_pts
                all_index_points.extend(reg_pts)

            if region_series_for_country:
                nat_pts = aggregate_weighted_geo_indices(
                    region_series_for_country,
                    region_weights_for_country,
                    target_geo_level=GeoLevel.COUNTRY,
                    target_geo_code="AR",
                    channel_scope=channel_scope,
                    weighting=scheme,
                    base_index=self._base_index,
                )
                all_index_points.extend(nat_pts)

        persisted = self._upsert_index_points(all_index_points, frequency=frequency)
        self._session.commit()
        return persisted

    def _upsert_daily_cells(self, cells: Sequence[VarietyRollupCell]) -> int:
        if not cells:
            return 0
        payload = [
            {
                "rollup_date": c.rollup_date,
                "geo_level": c.geo_level.value,
                "geo_code": c.geo_code,
                "generic_variety_id": c.generic_variety_id,
                "channel_scope": c.channel_scope,
                "active_pos_count": c.active_pos_count,
                "in_stock_pos_count": c.in_stock_pos_count,
                "distinct_articles_count": c.distinct_articles_count,
                "geo_mean_unit_list_cents": c.geo_mean_unit_list_cents,
                "geo_mean_unit_effective_cents": c.geo_mean_unit_effective_cents,
                "min_unit_effective_cents": c.min_unit_effective_cents,
                "median_unit_effective_cents": c.median_unit_effective_cents,
                "max_unit_effective_cents": c.max_unit_effective_cents,
                "iqr_unit_effective_cents": c.iqr_unit_effective_cents,
                "promo_pos_count": c.promo_pos_count,
                "promo_gap_pct": c.promo_gap_pct,
                "stockout_rate": c.stockout_rate,
            }
            for c in cells
        ]
        for i in range(0, len(payload), 2_000):
            chunk = payload[i : i + 2_000]
            ins = pg_insert(DailyGeoVarietyRollupModel).values(chunk)
            self._session.execute(
                ins.on_conflict_do_update(
                    constraint="uq_daily_geo_variety_rollups_cell",
                    set_={
                        "active_pos_count": ins.excluded.active_pos_count,
                        "in_stock_pos_count": ins.excluded.in_stock_pos_count,
                        "distinct_articles_count": ins.excluded.distinct_articles_count,
                        "geo_mean_unit_list_cents": ins.excluded.geo_mean_unit_list_cents,
                        "geo_mean_unit_effective_cents": ins.excluded.geo_mean_unit_effective_cents,
                        "min_unit_effective_cents": ins.excluded.min_unit_effective_cents,
                        "median_unit_effective_cents": ins.excluded.median_unit_effective_cents,
                        "max_unit_effective_cents": ins.excluded.max_unit_effective_cents,
                        "iqr_unit_effective_cents": ins.excluded.iqr_unit_effective_cents,
                        "promo_pos_count": ins.excluded.promo_pos_count,
                        "promo_gap_pct": ins.excluded.promo_gap_pct,
                        "stockout_rate": ins.excluded.stockout_rate,
                    },
                )
            )
        return len(payload)

    def _upsert_index_points(self, points: Sequence[PriceIndexPoint], frequency: str) -> int:
        if not points:
            return 0
        payload = [
            {
                "period_date": p.period_date,
                "frequency": frequency,
                "geo_level": p.geo_level.value,
                "geo_code": p.geo_code,
                "category_scope": f"CAT:{p.category_id}" if p.category_id else "ALL",
                "category_id": p.category_id,
                "channel_scope": p.channel_scope,
                "weighting": p.weighting.value,
                "index_value": p.index_value,
                "period_pct_change": p.period_pct_change,
                "yoy_pct_change": p.yoy_pct_change,
                "overlapping_varieties_count": p.overlapping_varieties_count,
            }
            for p in points
        ]
        for i in range(0, len(payload), 2_000):
            chunk = payload[i : i + 2_000]
            ins = pg_insert(PriceIndexSeriesModel).values(chunk)
            self._session.execute(
                ins.on_conflict_do_update(
                    constraint="uq_price_index_series_point",
                    set_={
                        "index_value": ins.excluded.index_value,
                        "period_pct_change": ins.excluded.period_pct_change,
                        "yoy_pct_change": ins.excluded.yoy_pct_change,
                        "overlapping_varieties_count": ins.excluded.overlapping_varieties_count,
                    },
                )
            )
        return len(payload)


class CompareGovernmentPeriodsUseCase:
    """Compares national inflation, volatility, INDEC gaps, & RIPTE wages across administrations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def compare_periods(
        self,
        period_slugs: Sequence[str] | None = None,
        frequency: str = "monthly",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    ) -> list[PeriodComparisonMetrics]:
        """Compute PeriodComparisonMetrics for requested or all stored presidential administrations."""
        stmt = select(GovernmentPeriodModel).order_by(GovernmentPeriodModel.start_date.asc())
        if period_slugs:
            stmt = stmt.where(GovernmentPeriodModel.slug.in_(list(period_slugs)))
        periods = self._session.scalars(stmt).all()

        nat_rows = self._session.scalars(
            select(PriceIndexSeriesModel)
            .where(
                PriceIndexSeriesModel.frequency == frequency,
                PriceIndexSeriesModel.geo_level == GeoLevel.COUNTRY.value,
                PriceIndexSeriesModel.geo_code == "AR",
                PriceIndexSeriesModel.category_scope == "ALL",
                PriceIndexSeriesModel.channel_scope == "all",
                PriceIndexSeriesModel.weighting == weighting.value,
            )
            .order_by(PriceIndexSeriesModel.period_date.asc())
        ).all()

        nat_pts = [
            PriceIndexPoint(
                period_date=r.period_date,
                geo_level=GeoLevel.COUNTRY,
                geo_code=r.geo_code,
                category_id=r.category_id,
                channel_scope=r.channel_scope,
                weighting=weighting,
                index_value=r.index_value,
                period_pct_change=r.period_pct_change,
                yoy_pct_change=r.yoy_pct_change,
                overlapping_varieties_count=r.overlapping_varieties_count,
            )
            for r in nat_rows
        ]

        indec_map = {
            r.period_date: r.value
            for r in self._session.scalars(
                select(MacroReferenceSeriesModel).where(
                    MacroReferenceSeriesModel.series_code == "INDEC_IPC_GENERAL",
                    MacroReferenceSeriesModel.geo_code == "AR",
                )
            )
        }
        ripte_map = {
            r.period_date: r.value
            for r in self._session.scalars(
                select(MacroReferenceSeriesModel).where(
                    MacroReferenceSeriesModel.series_code == "RIPTE",
                    MacroReferenceSeriesModel.geo_code == "AR",
                )
            )
        }

        results: list[PeriodComparisonMetrics] = []
        for p in periods:
            diag_row = self._session.execute(
                select(
                    func.avg(DailyGeoVarietyRollupModel.promo_gap_pct),
                    func.avg(DailyGeoVarietyRollupModel.stockout_rate),
                ).where(
                    DailyGeoVarietyRollupModel.geo_level == GeoLevel.COUNTRY.value,
                    DailyGeoVarietyRollupModel.geo_code == "AR",
                    DailyGeoVarietyRollupModel.rollup_date >= p.start_date,
                    DailyGeoVarietyRollupModel.rollup_date <= p.end_date,
                )
            ).one()
            avg_promo = float(diag_row[0]) if diag_row[0] is not None else None
            avg_stockout = float(diag_row[1]) if diag_row[1] is not None else None

            pat_gba_spread = self._compute_patagonia_vs_gba_spread(p.start_date, p.end_date)

            results.append(
                summarize_government_period(
                    period_slug=p.slug,
                    period_name=p.name,
                    administration_label=p.administration_label,
                    start_date=p.start_date,
                    end_date=p.end_date,
                    national_index_points=nat_pts,
                    indec_ipc_by_date=indec_map,
                    ripte_by_date=ripte_map,
                    avg_promo_gap_pct=avg_promo,
                    avg_stockout_rate_pct=avg_stockout,
                    patagonia_vs_gba_spread_pct=pat_gba_spread,
                )
            )

        return results

    def _compute_patagonia_vs_gba_spread(self, start_date: date, end_date: date) -> float | None:
        pat_subq = (
            select(
                DailyGeoVarietyRollupModel.rollup_date.label("dt"),
                DailyGeoVarietyRollupModel.generic_variety_id.label("vid"),
                DailyGeoVarietyRollupModel.geo_mean_unit_effective_cents.label("pat_c"),
            )
            .where(
                DailyGeoVarietyRollupModel.geo_level == GeoLevel.REGION.value,
                DailyGeoVarietyRollupModel.geo_code == "PATAGONIA",
                DailyGeoVarietyRollupModel.rollup_date >= start_date,
                DailyGeoVarietyRollupModel.rollup_date <= end_date,
            )
            .subquery("pat")
        )
        gba_subq = (
            select(
                DailyGeoVarietyRollupModel.rollup_date.label("dt"),
                DailyGeoVarietyRollupModel.generic_variety_id.label("vid"),
                DailyGeoVarietyRollupModel.geo_mean_unit_effective_cents.label("gba_c"),
            )
            .where(
                DailyGeoVarietyRollupModel.geo_level == GeoLevel.REGION.value,
                DailyGeoVarietyRollupModel.geo_code == "GBA",
                DailyGeoVarietyRollupModel.rollup_date >= start_date,
                DailyGeoVarietyRollupModel.rollup_date <= end_date,
            )
            .subquery("gba")
        )
        val = self._session.scalar(
            select(
                func.avg(((pat_subq.c.pat_c - gba_subq.c.gba_c) * 100.0) / gba_subq.c.gba_c)
            ).join(
                gba_subq,
                (gba_subq.c.dt == pat_subq.c.dt) & (gba_subq.c.vid == pat_subq.c.vid),
            )
        )
        return float(val) if val is not None else None


__all__ = [
    "CompareGovernmentPeriodsUseCase",
    "ComputeRollupsAndCpiUseCase",
]
