"""Unit tests for Stage 1 Jevons, Stage 2 Chained Laspeyres/Young CPI, and cross-government metrics."""

from __future__ import annotations

from datetime import date

import pytest

from argenprecios.domain.cpi_engine import (
    UnitPriceSample,
    aggregate_weighted_geo_indices,
    compute_chained_jevons_series,
    compute_variety_rollup_cell,
    summarize_government_period,
)
from argenprecios.domain.enums import GeoLevel, WeightingScheme


def test_stage_1_variety_rollup_jevons_and_scarcity() -> None:
    samples = [
        UnitPriceSample(
            pos_id=1,
            article_id=101,
            unit_list_cents=100_000,
            unit_effective_cents=100_000,
            has_promo=False,
            in_stock=True,
        ),
        UnitPriceSample(
            pos_id=2,
            article_id=102,
            unit_list_cents=400_000,
            unit_effective_cents=225_000,
            has_promo=True,
            in_stock=True,
        ),
        UnitPriceSample(
            pos_id=3,
            article_id=101,
            unit_list_cents=250_000,
            unit_effective_cents=250_000,
            has_promo=False,
            in_stock=False,
        ),
    ]
    cell = compute_variety_rollup_cell(
        rollup_date=date(2026, 4, 1),
        geo_level=GeoLevel.PROVINCE,
        geo_code="AR-U",
        generic_variety_id=50,
        channel_scope="all",
        samples=samples,
    )
    assert cell is not None
    # Geometric mean of [100_000, 400_000] = 200_000; geometric mean of [100_000, 225_000] = 150_000
    assert cell.geo_mean_unit_list_cents == 200_000
    assert cell.geo_mean_unit_effective_cents == 150_000
    assert cell.promo_gap_pct == pytest.approx(25.0)
    assert cell.active_pos_count == 3
    assert cell.in_stock_pos_count == 2
    assert cell.stockout_rate == pytest.approx(100.0 / 3.0, abs=0.01)


def test_stage_2_chained_jevons_and_cross_government_comparison() -> None:
    # Variety 1 doubles each month in Term A (+100%/mo), then grows +10%/mo in Term B;
    # Variety 2 enters in month 2 seamlessly via chaining.
    prov_gba = compute_chained_jevons_series(
        {
            date(2023, 12, 1): {1: 100_000},
            date(2024, 1, 1): {1: 120_000, 2: 200_000},
            date(2024, 2, 1): {1: 144_000, 2: 240_000},
            date(2028, 1, 1): {1: 300_000, 2: 500_000},
            date(2028, 2, 1): {1: 306_000, 2: 510_000},
            date(2028, 3, 1): {1: 312_120, 2: 520_200},
        },
        geo_level=GeoLevel.PROVINCE,
        geo_code="AR-C",
    )
    prov_pat = compute_chained_jevons_series(
        {
            date(2023, 12, 1): {1: 110_000},
            date(2024, 1, 1): {1: 121_000},
            date(2024, 2, 1): {1: 133_100},
            date(2028, 1, 1): {1: 310_000},
            date(2028, 2, 1): {1: 316_200},
            date(2028, 3, 1): {1: 322_524},
        },
        geo_level=GeoLevel.PROVINCE,
        geo_code="AR-U",
    )

    national = aggregate_weighted_geo_indices(
        {"AR-C": prov_gba, "AR-U": prov_pat},
        {"AR-C": 0.8, "AR-U": 0.2},
        target_geo_level=GeoLevel.COUNTRY,
        target_geo_code="AR",
        weighting=WeightingScheme.EXPENDITURE,
    )
    assert len(national) == 6
    assert national[0].index_value == pytest.approx(100.0)
    # Month 1 relative = 0.8 * 1.20 + 0.2 * 1.10 = 1.18 -> Index = 118.0
    assert national[1].index_value == pytest.approx(118.0, abs=0.01)

    term_1 = summarize_government_period(
        period_slug="mandato_2023_2027",
        period_name="Mandato 1",
        administration_label="Admin A",
        start_date=date(2023, 12, 1),
        end_date=date(2027, 12, 9),
        national_index_points=national,
        indec_ipc_by_date={date(2023, 12, 1): 100.0, date(2024, 2, 1): 135.0},
        ripte_by_date={date(2023, 12, 1): 500_000.0, date(2024, 2, 1): 600_000.0},
    )
    term_2 = summarize_government_period(
        period_slug="mandato_2027_2031",
        period_name="Mandato 2",
        administration_label="Admin B",
        start_date=date(2027, 12, 10),
        end_date=date(2031, 12, 9),
        national_index_points=national,
    )

    assert term_1.cumulative_inflation_pct is not None
    assert term_2.cumulative_inflation_pct is not None
    assert term_1.cmgr_pct is not None and term_2.cmgr_pct is not None
    assert term_1.cmgr_pct > term_2.cmgr_pct
    assert term_1.official_indec_cumulative_pct == pytest.approx(35.0)
    assert term_1.ripte_real_purchasing_power_change_pct is not None
    assert term_1.ripte_real_purchasing_power_change_pct < 0.0
