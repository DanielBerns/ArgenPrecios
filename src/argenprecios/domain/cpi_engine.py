"""Two-stage international CPI math (Jevons -> Chained Young/Laspeyres) & cross-government metrics."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from argenprecios.domain.basket_math import geometric_mean_cents
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    PeriodComparisonMetrics,
    PriceIndexPoint,
    VarietyRollupCell,
)


@dataclass(frozen=True, slots=True)
class UnitPriceSample:
    """Single store-article normalized unit-price observation feeding Stage 1 Jevons rollup."""

    pos_id: int
    article_id: int
    unit_list_cents: int
    unit_effective_cents: int
    has_promo: bool
    in_stock: bool


def _percentile_sorted(sorted_vals: Sequence[int], pct: float) -> int:
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = pct * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def compute_variety_rollup_cell(
    rollup_date: date,
    geo_level: GeoLevel,
    geo_code: str,
    generic_variety_id: int,
    channel_scope: str,
    samples: Sequence[UnitPriceSample],
) -> VarietyRollupCell | None:
    """Compute Stage 1 elementary Jevons geometric mean & dispersion for a variety-geography cell."""
    if not samples:
        return None

    distinct_pos = {s.pos_id for s in samples}
    in_stock_samples = [s for s in samples if s.in_stock and s.unit_effective_cents > 0]
    if not in_stock_samples:
        return None

    in_stock_pos = {s.pos_id for s in in_stock_samples}
    distinct_articles = {s.article_id for s in in_stock_samples}

    list_vals = [s.unit_list_cents for s in in_stock_samples if s.unit_list_cents > 0]
    eff_vals = sorted(s.unit_effective_cents for s in in_stock_samples)

    geo_list = geometric_mean_cents(list_vals) if list_vals else geometric_mean_cents(eff_vals)
    geo_eff = geometric_mean_cents(eff_vals)

    q25 = _percentile_sorted(eff_vals, 0.25)
    q50 = _percentile_sorted(eff_vals, 0.50)
    q75 = _percentile_sorted(eff_vals, 0.75)

    promo_pos = {s.pos_id for s in in_stock_samples if s.has_promo}
    promo_gap = max(0.0, (geo_list - geo_eff) / geo_list * 100.0) if geo_list > 0 else 0.0
    stockout_rate = (
        max(0.0, (len(distinct_pos) - len(in_stock_pos)) / len(distinct_pos) * 100.0)
        if distinct_pos
        else 0.0
    )

    return VarietyRollupCell(
        rollup_date=rollup_date,
        geo_level=geo_level,
        geo_code=geo_code,
        generic_variety_id=generic_variety_id,
        channel_scope=channel_scope,
        active_pos_count=len(distinct_pos),
        in_stock_pos_count=len(in_stock_pos),
        distinct_articles_count=len(distinct_articles),
        geo_mean_unit_list_cents=geo_list,
        geo_mean_unit_effective_cents=geo_eff,
        min_unit_effective_cents=eff_vals[0],
        median_unit_effective_cents=q50,
        max_unit_effective_cents=eff_vals[-1],
        iqr_unit_effective_cents=max(0, q75 - q25),
        promo_pos_count=len(promo_pos),
        promo_gap_pct=round(promo_gap, 4),
        stockout_rate=round(stockout_rate, 4),
    )


def compute_chained_jevons_series(
    period_variety_prices: Mapping[date, Mapping[int, int]],
    *,
    geo_level: GeoLevel,
    geo_code: str,
    category_id: int | None = None,
    channel_scope: str = "all",
    weighting: WeightingScheme = WeightingScheme.UNWEIGHTED,
    base_index: float = 100.0,
) -> list[PriceIndexPoint]:
    """Compute a chained index series over overlapping varieties across arbitrary 8+ year horizons.

    Chaining links `I_t = I_{t-1} * exp(mean(ln(P_{v,t} / P_{v,t-1})))` over varieties `v`
    observed in both `t-1` and `t`, seamlessly handling variety entry, exit, and SKU replacement.
    """
    ordered_dates = sorted(d for d, vmap in period_variety_prices.items() if vmap)
    if not ordered_dates:
        return []

    points: list[PriceIndexPoint] = []
    index_by_date: dict[date, float] = {}
    current_index = base_index

    for idx, dt in enumerate(ordered_dates):
        curr_map = period_variety_prices[dt]
        if idx == 0:
            index_by_date[dt] = current_index
            points.append(
                PriceIndexPoint(
                    period_date=dt,
                    geo_level=geo_level,
                    geo_code=geo_code,
                    category_id=category_id,
                    channel_scope=channel_scope,
                    weighting=weighting,
                    index_value=round(current_index, 4),
                    period_pct_change=None,
                    yoy_pct_change=None,
                    overlapping_varieties_count=len(curr_map),
                )
            )
            continue

        prev_dt = ordered_dates[idx - 1]
        prev_map = period_variety_prices[prev_dt]
        common_vars = [
            v_id
            for v_id in curr_map
            if v_id in prev_map and curr_map[v_id] > 0 and prev_map[v_id] > 0
        ]

        if common_vars:
            mean_log_rel = sum(
                math.log(curr_map[v_id] / prev_map[v_id]) for v_id in common_vars
            ) / len(common_vars)
            chain_factor = math.exp(mean_log_rel)
        else:
            chain_factor = 1.0

        prev_index = current_index
        current_index = prev_index * chain_factor
        index_by_date[dt] = current_index
        pct_change = ((current_index / prev_index) - 1.0) * 100.0

        yoy_target = date(
            dt.year - 1,
            dt.month,
            min(dt.day, 28) if dt.month == 2 else dt.day,
        )
        yoy_pct: float | None = None
        if yoy_target in index_by_date and index_by_date[yoy_target] > 0:
            yoy_pct = round(((current_index / index_by_date[yoy_target]) - 1.0) * 100.0, 4)

        points.append(
            PriceIndexPoint(
                period_date=dt,
                geo_level=geo_level,
                geo_code=geo_code,
                category_id=category_id,
                channel_scope=channel_scope,
                weighting=weighting,
                index_value=round(current_index, 4),
                period_pct_change=round(pct_change, 4),
                yoy_pct_change=yoy_pct,
                overlapping_varieties_count=len(common_vars),
            )
        )

    return points


def aggregate_weighted_geo_indices(
    child_series_by_geo: Mapping[str, Sequence[PriceIndexPoint]],
    geo_weights: Mapping[str, float],
    *,
    target_geo_level: GeoLevel,
    target_geo_code: str,
    category_id: int | None = None,
    channel_scope: str = "all",
    weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    base_index: float = 100.0,
) -> list[PriceIndexPoint]:
    """Aggregate Provincial index series into INDEC Regional or National chained Laspeyres/Young series."""
    relatives_by_date: dict[date, list[tuple[float, float, int]]] = {}
    all_dates: set[date] = set()

    for g_code, pts in child_series_by_geo.items():
        w = (
            1.0
            if weighting == WeightingScheme.UNWEIGHTED
            else max(geo_weights.get(g_code.upper(), 0.0), 1e-6)
        )
        sorted_pts = sorted(pts, key=lambda p: p.period_date)
        for i, pt in enumerate(sorted_pts):
            all_dates.add(pt.period_date)
            if i > 0 and sorted_pts[i - 1].index_value > 0:
                rel = pt.index_value / sorted_pts[i - 1].index_value
                relatives_by_date.setdefault(pt.period_date, []).append(
                    (w, rel, pt.overlapping_varieties_count)
                )

    ordered_dates = sorted(all_dates)
    if not ordered_dates:
        return []

    out: list[PriceIndexPoint] = []
    index_by_date: dict[date, float] = {}
    current_idx = base_index

    for idx, dt in enumerate(ordered_dates):
        if idx == 0:
            index_by_date[dt] = current_idx
            out.append(
                PriceIndexPoint(
                    period_date=dt,
                    geo_level=target_geo_level,
                    geo_code=target_geo_code,
                    category_id=category_id,
                    channel_scope=channel_scope,
                    weighting=weighting,
                    index_value=round(current_idx, 4),
                    period_pct_change=None,
                    yoy_pct_change=None,
                    overlapping_varieties_count=0,
                )
            )
            continue

        entries = relatives_by_date.get(dt, [])
        if entries:
            w_total = sum(e[0] for e in entries)
            weighted_rel = sum((e[0] / w_total) * e[1] for e in entries)
            overlap_sum = sum(e[2] for e in entries)
        else:
            weighted_rel = 1.0
            overlap_sum = 0

        prev_idx = current_idx
        current_idx = prev_idx * weighted_rel
        index_by_date[dt] = current_idx
        pct_change = ((current_idx / prev_idx) - 1.0) * 100.0

        yoy_target = date(
            dt.year - 1,
            dt.month,
            min(dt.day, 28) if dt.month == 2 else dt.day,
        )
        yoy_pct: float | None = None
        if yoy_target in index_by_date and index_by_date[yoy_target] > 0:
            yoy_pct = round(((current_idx / index_by_date[yoy_target]) - 1.0) * 100.0, 4)

        out.append(
            PriceIndexPoint(
                period_date=dt,
                geo_level=target_geo_level,
                geo_code=target_geo_code,
                category_id=category_id,
                channel_scope=channel_scope,
                weighting=weighting,
                index_value=round(current_idx, 4),
                period_pct_change=round(pct_change, 4),
                yoy_pct_change=yoy_pct,
                overlapping_varieties_count=overlap_sum,
            )
        )

    return out


def summarize_government_period(
    *,
    period_slug: str,
    period_name: str,
    administration_label: str,
    start_date: date,
    end_date: date,
    national_index_points: Sequence[PriceIndexPoint],
    indec_ipc_by_date: Mapping[date, float] | None = None,
    ripte_by_date: Mapping[date, float] | None = None,
    avg_promo_gap_pct: float | None = None,
    avg_stockout_rate_pct: float | None = None,
    patagonia_vs_gba_spread_pct: float | None = None,
) -> PeriodComparisonMetrics:
    """Compute cross-government inflation, CMGR, volatility, INDEC gap, & RIPTE purchasing power."""
    in_window = sorted(
        (p for p in national_index_points if start_date <= p.period_date <= end_date),
        key=lambda p: p.period_date,
    )
    if len(in_window) < 2:
        obs_start = in_window[0].period_date if in_window else None
        return PeriodComparisonMetrics(
            period_slug=period_slug,
            period_name=period_name,
            administration_label=administration_label,
            start_date=start_date,
            end_date=end_date,
            observed_start_date=obs_start,
            observed_end_date=obs_start,
            months_observed=0.0,
            cumulative_inflation_pct=None,
            cmgr_pct=None,
            annualized_inflation_pct=None,
            monthly_volatility_pct=None,
            official_indec_cumulative_pct=None,
            official_gap_percentage_points=None,
            ripte_real_purchasing_power_change_pct=None,
            avg_promo_gap_pct=avg_promo_gap_pct,
            avg_stockout_rate_pct=avg_stockout_rate_pct,
            patagonia_vs_gba_spread_pct=patagonia_vs_gba_spread_pct,
        )

    first_pt = in_window[0]
    last_pt = in_window[-1]
    elapsed_days = max(1, (last_pt.period_date - first_pt.period_date).days)
    months_obs = elapsed_days / 30.4375

    ratio = last_pt.index_value / first_pt.index_value
    cumulative_pct = (ratio - 1.0) * 100.0
    cmgr = ((ratio ** (1.0 / months_obs)) - 1.0) * 100.0 if months_obs > 0 else 0.0
    annualized = ((ratio ** (365.25 / elapsed_days)) - 1.0) * 100.0

    period_changes = [p.period_pct_change for p in in_window[1:] if p.period_pct_change is not None]
    volatility = statistics.pstdev(period_changes) if len(period_changes) >= 2 else 0.0

    indec_cum: float | None = None
    indec_gap: float | None = None
    if indec_ipc_by_date:
        indec_dates = sorted(d for d in indec_ipc_by_date if start_date <= d <= end_date)
        if len(indec_dates) >= 2:
            i0 = indec_ipc_by_date[indec_dates[0]]
            i1 = indec_ipc_by_date[indec_dates[-1]]
            if i0 > 0:
                indec_cum = round(((i1 / i0) - 1.0) * 100.0, 4)
                indec_gap = round(cumulative_pct - indec_cum, 4)

    ripte_real_pct: float | None = None
    if ripte_by_date:
        r_dates = sorted(d for d in ripte_by_date if start_date <= d <= end_date)
        if len(r_dates) >= 2:
            w0 = ripte_by_date[r_dates[0]]
            w1 = ripte_by_date[r_dates[-1]]
            if w0 > 0 and ratio > 0:
                nominal_wage_ratio = w1 / w0
                ripte_real_pct = round(((nominal_wage_ratio / ratio) - 1.0) * 100.0, 4)

    return PeriodComparisonMetrics(
        period_slug=period_slug,
        period_name=period_name,
        administration_label=administration_label,
        start_date=start_date,
        end_date=end_date,
        observed_start_date=first_pt.period_date,
        observed_end_date=last_pt.period_date,
        months_observed=round(months_obs, 2),
        cumulative_inflation_pct=round(cumulative_pct, 4),
        cmgr_pct=round(cmgr, 4),
        annualized_inflation_pct=round(annualized, 4),
        monthly_volatility_pct=round(volatility, 4),
        official_indec_cumulative_pct=indec_cum,
        official_gap_percentage_points=indec_gap,
        ripte_real_purchasing_power_change_pct=ripte_real_pct,
        avg_promo_gap_pct=(round(avg_promo_gap_pct, 4) if avg_promo_gap_pct is not None else None),
        avg_stockout_rate_pct=(
            round(avg_stockout_rate_pct, 4) if avg_stockout_rate_pct is not None else None
        ),
        patagonia_vs_gba_spread_pct=(
            round(patagonia_vs_gba_spread_pct, 4)
            if patagonia_vs_gba_spread_pct is not None
            else None
        ),
    )


__all__ = [
    "UnitPriceSample",
    "aggregate_weighted_geo_indices",
    "compute_chained_jevons_series",
    "compute_variety_rollup_cell",
    "summarize_government_period",
]
