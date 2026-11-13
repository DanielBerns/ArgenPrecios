"""Pure consumer basket valuation math with substitute EAN & GenericVariety 8-year fallbacks."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    BasketDefinition,
    BasketGeoEvaluation,
    EvaluatedBasketItem,
)


def geometric_mean_cents(values: Sequence[int]) -> int:
    """Compute rounded Jevons geometric mean in centavos for strictly positive prices."""
    positives = [v for v in values if v > 0]
    if not positives:
        raise ValueError("Cannot compute geometric mean of empty or non-positive values")
    mean_ln = sum(math.log(v) for v in positives) / len(positives)
    return max(1, round(math.exp(mean_ln)))


def evaluate_store_scope_basket(
    basket: BasketDefinition,
    evaluated_date: date,
    geo_level: GeoLevel,
    geo_code: str,
    geo_name: str,
    store_ean_prices: Mapping[str, Mapping[str, int]],
    variety_unit_prices_by_store: Mapping[str, Mapping[str, int]] | None = None,
    ean_default_pack_amount: Mapping[str, float] | None = None,
) -> BasketGeoEvaluation:
    """Evaluate a basket across a set of stores (`{store_code: {ean: effective_price_cents}}`).

    Resolution priority per selected BasketItemSpec:
      1. `item.ean` across stores in the geographic scope.
      2. Ordered `item.substitute_eans` if `item.ean` has 0 observing stores in this geography/date.
      3. `item.fallback_variety_slug` unit prices (`{store_code: {variety_slug: unit_price_cents}}`)
         multiplied by `item.target_amount` (or primary EAN pack amount, default `1.0`).
    """
    selected_items = [it for it in basket.items if it.selected]
    total_items = len(selected_items)
    if total_items == 0:
        return BasketGeoEvaluation(
            basket_uuid=basket.uuid,
            basket_name=basket.name,
            evaluated_date=evaluated_date,
            geo_level=geo_level,
            geo_code=geo_code,
            geo_name=geo_name,
            weighting=WeightingScheme.UNWEIGHTED,
            total_items_count=0,
            matched_items_count=0,
            coverage_ratio=1.0,
            min_total_cents=0,
            geo_mean_reference_total_cents=0,
            max_total_cents=0,
            best_single_store_cents=None,
            best_single_store_code=None,
            worst_single_store_cents=None,
            complete_stores_count=0,
            items=(),
        )

    var_prices = variety_unit_prices_by_store or {}
    pack_amounts = ean_default_pack_amount or {}

    # Track per-store line costs to compute best/worst complete single-store totals
    store_line_costs: dict[str, dict[int, int]] = {}
    evaluated_items: list[EvaluatedBasketItem] = []
    matched_count = 0
    min_sum = 0
    geo_sum = 0
    max_sum = 0

    for idx, item in enumerate(selected_items):
        mult = item.effective_multiplier
        resolved_via = "missing"
        resolved_id: str | None = None
        per_store_unit_cost: dict[str, int] = {}

        # 1. Primary EAN
        if item.ean:
            for s_code, ean_map in store_ean_prices.items():
                if item.ean in ean_map and ean_map[item.ean] > 0:
                    per_store_unit_cost[s_code] = ean_map[item.ean]
            if per_store_unit_cost:
                resolved_via = "primary_ean"
                resolved_id = item.ean

        # 2. Ordered Substitute EANs
        if not per_store_unit_cost and item.substitute_eans:
            for sub_ean in item.substitute_eans:
                candidate_costs = {
                    s_code: ean_map[sub_ean]
                    for s_code, ean_map in store_ean_prices.items()
                    if sub_ean in ean_map and ean_map[sub_ean] > 0
                }
                if candidate_costs:
                    per_store_unit_cost = candidate_costs
                    resolved_via = "substitute_ean"
                    resolved_id = sub_ean
                    break

        # 3. Fallback GenericVariety (unit price * target pack amount)
        if not per_store_unit_cost and item.fallback_variety_slug:
            scale_amt = item.target_amount
            if scale_amt is None and item.ean and item.ean in pack_amounts:
                scale_amt = pack_amounts[item.ean]
            if scale_amt is None or scale_amt <= 0:
                scale_amt = 1.0

            candidate_var_costs = {
                s_code: max(1, round(v_map[item.fallback_variety_slug] * scale_amt))
                for s_code, v_map in var_prices.items()
                if item.fallback_variety_slug in v_map and v_map[item.fallback_variety_slug] > 0
            }
            if candidate_var_costs:
                per_store_unit_cost = candidate_var_costs
                resolved_via = "generic_variety"
                resolved_id = item.fallback_variety_slug

        if not per_store_unit_cost:
            evaluated_items.append(
                EvaluatedBasketItem(
                    requested_ean=item.ean,
                    resolved_via="missing",
                    resolved_identifier=None,
                    effective_multiplier=mult,
                    min_line_cents=None,
                    geo_mean_line_cents=None,
                    max_line_cents=None,
                    observing_pos_count=0,
                )
            )
            continue

        matched_count += 1
        line_costs_by_store = {
            s_code: max(1, round(unit_p * mult)) for s_code, unit_p in per_store_unit_cost.items()
        }
        for s_code, l_cost in line_costs_by_store.items():
            store_line_costs.setdefault(s_code, {})[idx] = l_cost

        line_vals = list(line_costs_by_store.values())
        min_l = min(line_vals)
        geo_l = geometric_mean_cents(line_vals)
        max_l = max(line_vals)

        min_sum += min_l
        geo_sum += geo_l
        max_sum += max_l

        evaluated_items.append(
            EvaluatedBasketItem(
                requested_ean=item.ean,
                resolved_via=resolved_via,
                resolved_identifier=resolved_id,
                effective_multiplier=mult,
                min_line_cents=min_l,
                geo_mean_line_cents=geo_l,
                max_line_cents=max_l,
                observing_pos_count=len(line_vals),
            )
        )

    complete_stores = {
        s_code: sum(lines.values())
        for s_code, lines in store_line_costs.items()
        if len(lines) == total_items
    }
    best_store_code: str | None = None
    best_store_cents: int | None = None
    worst_store_cents: int | None = None
    if complete_stores:
        best_store_code = min(complete_stores, key=lambda k: complete_stores[k])
        best_store_cents = complete_stores[best_store_code]
        worst_store_cents = max(complete_stores.values())

    coverage = matched_count / total_items
    return BasketGeoEvaluation(
        basket_uuid=basket.uuid,
        basket_name=basket.name,
        evaluated_date=evaluated_date,
        geo_level=geo_level,
        geo_code=geo_code,
        geo_name=geo_name,
        weighting=WeightingScheme.UNWEIGHTED,
        total_items_count=total_items,
        matched_items_count=matched_count,
        coverage_ratio=round(coverage, 4),
        min_total_cents=min_sum if matched_count > 0 else None,
        geo_mean_reference_total_cents=geo_sum if matched_count > 0 else None,
        max_total_cents=max_sum if matched_count > 0 else None,
        best_single_store_cents=best_store_cents,
        best_single_store_code=best_store_code,
        worst_single_store_cents=worst_store_cents,
        complete_stores_count=len(complete_stores),
        items=tuple(evaluated_items),
    )


def aggregate_provincial_basket_evaluations(
    basket: BasketDefinition,
    evaluated_date: date,
    target_geo_level: GeoLevel,
    target_geo_code: str,
    target_geo_name: str,
    provincial_evals: Sequence[BasketGeoEvaluation],
    province_weights: Mapping[str, float],
    weighting: WeightingScheme,
) -> BasketGeoEvaluation:
    """Aggregate provincial basket evaluations into a Region or Country weighted basket valuation."""
    valid = [
        ev
        for ev in provincial_evals
        if ev.geo_mean_reference_total_cents is not None and ev.matched_items_count > 0
    ]
    total_items = len([it for it in basket.items if it.selected])
    if not valid:
        return BasketGeoEvaluation(
            basket_uuid=basket.uuid,
            basket_name=basket.name,
            evaluated_date=evaluated_date,
            geo_level=target_geo_level,
            geo_code=target_geo_code,
            geo_name=target_geo_name,
            weighting=weighting,
            total_items_count=total_items,
            matched_items_count=0,
            coverage_ratio=0.0,
            min_total_cents=None,
            geo_mean_reference_total_cents=None,
            max_total_cents=None,
            best_single_store_cents=None,
            best_single_store_code=None,
            worst_single_store_cents=None,
            complete_stores_count=0,
            items=(),
        )

    raw_weights = [
        1.0
        if weighting == WeightingScheme.UNWEIGHTED
        else max(province_weights.get(ev.geo_code.upper(), 0.0), 1e-6)
        for ev in valid
    ]
    w_sum = sum(raw_weights)
    norm_w = [w / w_sum for w in raw_weights]

    weighted_ref = round(
        sum(
            w * float(ev.geo_mean_reference_total_cents or 0)
            for w, ev in zip(norm_w, valid, strict=True)
        )
    )
    min_across = min(ev.min_total_cents for ev in valid if ev.min_total_cents is not None)
    max_across = max(ev.max_total_cents for ev in valid if ev.max_total_cents is not None)

    best_candidates = [
        (ev.best_single_store_cents, ev.best_single_store_code)
        for ev in valid
        if ev.best_single_store_cents is not None
    ]
    best_cents: int | None = None
    best_code: str | None = None
    if best_candidates:
        best_cents, best_code = min(best_candidates, key=lambda pair: pair[0])

    worst_candidates = [
        ev.worst_single_store_cents for ev in valid if ev.worst_single_store_cents is not None
    ]
    worst_cents = max(worst_candidates) if worst_candidates else None
    avg_matched = round(
        sum(w * ev.matched_items_count for w, ev in zip(norm_w, valid, strict=True))
    )
    avg_cov = sum(w * ev.coverage_ratio for w, ev in zip(norm_w, valid, strict=True))

    return BasketGeoEvaluation(
        basket_uuid=basket.uuid,
        basket_name=basket.name,
        evaluated_date=evaluated_date,
        geo_level=target_geo_level,
        geo_code=target_geo_code,
        geo_name=target_geo_name,
        weighting=weighting,
        total_items_count=total_items,
        matched_items_count=avg_matched,
        coverage_ratio=round(avg_cov, 4),
        min_total_cents=min_across,
        geo_mean_reference_total_cents=weighted_ref,
        max_total_cents=max_across,
        best_single_store_cents=best_cents,
        best_single_store_code=best_code,
        worst_single_store_cents=worst_cents,
        complete_stores_count=sum(ev.complete_stores_count for ev in valid),
        items=(),
    )


__all__ = [
    "aggregate_provincial_basket_evaluations",
    "evaluate_store_scope_basket",
    "geometric_mean_cents",
]
