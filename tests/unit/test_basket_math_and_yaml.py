"""Unit tests for sur_en_precios basket YAML compatibility and multi-store / fallback valuation math."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from argenprecios.adapters.exporters.basket_yaml import (
    load_basket_yaml_file,
    parse_basket_yaml_str,
    serialize_basket_yaml_str,
)
from argenprecios.domain.basket_math import (
    aggregate_provincial_basket_evaluations,
    evaluate_store_scope_basket,
)
from argenprecios.domain.enums import CanonicalUnit, GeoLevel, WeightingScheme
from argenprecios.domain.models import BasketDefinition, BasketItemSpec


def test_round_trip_sur_en_precios_canasta_basica_yaml() -> None:
    cba_path = Path(
        "/home/dberns/Projects/github/DanielBerns/sur_en_precios/sur_en_precios/data/baskets/canasta_basica_alimentos.yaml"
    )
    basket = load_basket_yaml_file(cba_path)
    assert basket.name
    assert basket.uuid
    assert len(basket.items) >= 5

    dumped = serialize_basket_yaml_str(basket, strict_sur_en_precios_only=True)
    reparsed = parse_basket_yaml_str(dumped)
    assert reparsed.uuid == basket.uuid
    assert reparsed.name == basket.name
    assert len(reparsed.items) == len(basket.items)
    assert reparsed.items[0].ean == basket.items[0].ean
    assert reparsed.items[0].effective_multiplier == pytest.approx(
        basket.items[0].effective_multiplier
    )


def test_substitute_ean_and_generic_variety_fallbacks() -> None:
    basket = BasketDefinition(
        name="Canasta Federal Test",
        uuid="test-federal-v1",
        is_public=True,
        items=(
            # Item 1: primary EAN available in both stores, fractional 1 * (1/2) = 0.5
            BasketItemSpec(
                ean="7790001000011",
                quantity=1,
                numerator=1,
                denominator=2,
                name="Arroz 1kg",
            ),
            # Item 2: primary EAN missing in province, resolves via substitute EAN
            BasketItemSpec(
                ean="7790002000022",
                substitute_eans=("7790002999999",),
                quantity=2,
                name="Leche Entera 1L",
            ),
            # Item 3: neither primary nor substitute EAN present -> resolves via generic_variety!
            BasketItemSpec(
                ean="7790003000033",
                fallback_variety_slug="aceites-aceite-girasol-l",
                target_amount=1.5,
                target_unit=CanonicalUnit.LITER,
                quantity=1,
                name="Aceite Girasol 1.5L",
            ),
        ),
    )

    store_eans = {
        "POS-A": {"7790001000011": 200_000, "7790002999999": 140_000},
        "POS-B": {"7790001000011": 220_000, "7790002999999": 130_000},
    }
    store_vars = {
        "POS-A": {"aceites-aceite-girasol-l": 200_000},  # * 1.5L = 300,000
        "POS-B": {"aceites-aceite-girasol-l": 240_000},  # * 1.5L = 360,000
    }

    ev_prov_1 = evaluate_store_scope_basket(
        basket=basket,
        evaluated_date=date(2026, 4, 8),
        geo_level=GeoLevel.PROVINCE,
        geo_code="AR-U",
        geo_name="Chubut",
        store_ean_prices=store_eans,
        variety_unit_prices_by_store=store_vars,
    )

    assert ev_prov_1.matched_items_count == 3
    assert ev_prov_1.coverage_ratio == 1.0
    assert [it.resolved_via for it in ev_prov_1.items] == [
        "primary_ean",
        "substitute_ean",
        "generic_variety",
    ]
    # POS-A lines: 100,000 + 280,000 + 300,000 = 680,000
    # POS-B lines: 110,000 + 260,000 + 360,000 = 730,000
    # Multi-store min: 100,000 + 260,000 + 300,000 = 660,000
    assert ev_prov_1.min_total_cents == 660_000
    assert ev_prov_1.best_single_store_cents == 680_000
    assert ev_prov_1.best_single_store_code == "POS-A"
    assert ev_prov_1.worst_single_store_cents == 730_000

    # Weighted national aggregation across two provinces
    ev_prov_2 = evaluate_store_scope_basket(
        basket=basket,
        evaluated_date=date(2026, 4, 8),
        geo_level=GeoLevel.PROVINCE,
        geo_code="AR-C",
        geo_name="CABA",
        store_ean_prices={"POS-C": {"7790001000011": 180_000, "7790002000022": 120_000}},
        variety_unit_prices_by_store={"POS-C": {"aceites-aceite-girasol-l": 180_000}},
    )
    nat = aggregate_provincial_basket_evaluations(
        basket=basket,
        evaluated_date=date(2026, 4, 8),
        target_geo_level=GeoLevel.COUNTRY,
        target_geo_code="AR",
        target_geo_name="Argentina",
        provincial_evals=[ev_prov_1, ev_prov_2],
        province_weights={"AR-C": 0.9, "AR-U": 0.1},
        weighting=WeightingScheme.EXPENDITURE,
    )
    assert nat.geo_mean_reference_total_cents is not None
    # National expenditure-weighted total stays much closer to CABA (0.9 weight) than Chubut (0.1)
    assert abs(
        nat.geo_mean_reference_total_cents - (ev_prov_2.geo_mean_reference_total_cents or 0)
    ) < abs(nat.geo_mean_reference_total_cents - (ev_prov_1.geo_mean_reference_total_cents or 0))
