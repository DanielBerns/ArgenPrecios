"""Unit tests for unit normalization, shrinkflation detection, and promotional legend parsing."""

from __future__ import annotations

import pytest

from argenprecios.domain.enums import CanonicalUnit, PromoMechanism
from argenprecios.domain.promo_parser import (
    classify_promo_legend,
    evaluate_promotion,
    evaluate_sepa_dual_promos,
    parse_ars_to_cents,
)
from argenprecios.domain.unit_normalizer import (
    build_variety_slug,
    compute_unit_price_cents,
    resolve_unit_conversion,
    slugify_text,
)


def test_metric_conversions_and_shrinkflation_unit_price() -> None:
    # 500 g pack at $1,000 -> $2,000 / kg
    unit_500, factor_500 = resolve_unit_conversion(500.0, "g")
    assert unit_500 == CanonicalUnit.KG
    assert factor_500 == pytest.approx(0.5)
    unit_price_before = compute_unit_price_cents(100_000, factor_500)
    assert unit_price_before == 200_000

    # Downsized 400 g pack at same $1,000 sticker price -> $2,500 / kg (+25% shrinkflation!)
    unit_400, factor_400 = resolve_unit_conversion(400.0, "grs")
    assert unit_400 == CanonicalUnit.KG
    assert factor_400 == pytest.approx(0.4)
    unit_price_after = compute_unit_price_cents(100_000, factor_400)
    assert unit_price_after == 250_000
    assert (unit_price_after - unit_price_before) / unit_price_before == pytest.approx(0.25)

    # Volume and count conversions
    u_ml, f_ml = resolve_unit_conversion(900.0, "cc")
    assert u_ml == CanonicalUnit.LITER
    assert f_ml == pytest.approx(0.9)

    assert slugify_text("Azúcar Común Tipo A 1 Kg!") == "azucar-comun-tipo-a-1-kg"
    v_slug = build_variety_slug("endulzantes", "Azúcar Común Tipo A", CanonicalUnit.KG)
    assert v_slug == "endulzantes-azucar-comun-tipo-kg"


def test_parse_ars_to_cents_formats() -> None:
    assert parse_ars_to_cents("1250.50") == 125_050
    assert parse_ars_to_cents("1250,50") == 125_050
    assert parse_ars_to_cents("1.250,50") == 125_050
    assert parse_ars_to_cents(18.5) == 1_850
    assert parse_ars_to_cents(0) is None
    assert parse_ars_to_cents("") is None


def test_promotional_mechanisms_and_sepa_dual_promos() -> None:
    mech_2x1, fac_2x1 = classify_promo_legend("Llevando 2x1 en toda la marca")
    assert mech_2x1 == PromoMechanism.TWO_FOR_ONE
    assert fac_2x1 == pytest.approx(0.5)

    mech_3x2, fac_3x2 = classify_promo_legend("Promo 3X2")
    assert mech_3x2 == PromoMechanism.THREE_FOR_TWO
    assert fac_3x2 == pytest.approx(2.0 / 3.0)

    # "2do al 70%" -> pay 100% + 30% = 65% per unit
    mech_2do, fac_2do = classify_promo_legend("2do al 70% combinable")
    assert mech_2do == PromoMechanism.SECOND_UNIT_PCT
    assert fac_2do == pytest.approx(0.65)

    mech_loyalty, _ = classify_promo_legend("20% dto Mi Carrefour")
    assert mech_loyalty == PromoMechanism.LOYALTY_CARD

    mech_bank, _ = classify_promo_legend("30% off Cuenta DNI Banco Provincia")
    assert mech_bank == PromoMechanism.BANK_DISCOUNT

    # Evaluate implied unit promo price when explicit price is absent
    ev = evaluate_promotion(list_price_cents=200_000, raw_legend="2da unidad al 70%")
    assert ev.mechanism == PromoMechanism.SECOND_UNIT_PCT
    assert ev.promo_price_cents == 130_000
    assert ev.code_hash is not None

    # Dual SEPA promos pick the lower consumer-facing price
    best = evaluate_sepa_dual_promos(
        list_price_cents=200_000,
        promo1_raw_price="1700.00",
        promo1_legend="Comunidad Coto",
        promo2_raw_price="1500.00",
        promo2_legend="Llevando 2 unidades",
    )
    assert best.promo_price_cents == 150_000
