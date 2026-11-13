"""Multi-level consumer basket persistence and historical evaluation use case."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_analytics import (
    BasketEvaluationItemModel,
    BasketEvaluationModel,
    BasketItemModel,
    BasketItemSubstituteModel,
    BasketModel,
)
from argenprecios.adapters.db.models_catalog import ArticleModel, GenericVarietyModel
from argenprecios.adapters.db.models_facts import PriceSpanModel
from argenprecios.adapters.db.models_geo import (
    CityModel,
    PointOfSaleModel,
    ProvinceModel,
)
from argenprecios.config import RegionalWeightsConfig
from argenprecios.domain.basket_math import (
    aggregate_provincial_basket_evaluations,
    evaluate_store_scope_basket,
)
from argenprecios.domain.enums import CanonicalUnit, GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    BasketDefinition,
    BasketGeoEvaluation,
    BasketItemSpec,
)
from argenprecios.domain.unit_normalizer import compute_unit_price_cents

logger = logging.getLogger(__name__)


class EvaluateBasketUseCase:
    """Persists consumer baskets and evaluates them at City, Province, Region, and Country levels."""

    def __init__(
        self,
        session: Session,
        regional_weights: RegionalWeightsConfig,
        active_window_days: int = 7,
    ) -> None:
        self._session = session
        self._regional_weights = regional_weights
        self._active_window_days = active_window_days

    def save_basket(self, basket_def: BasketDefinition) -> BasketModel:
        """Create or update a BasketModel (by UUID) along with its items and substitutes."""
        existing = self._session.scalar(
            select(BasketModel).where(BasketModel.uuid == basket_def.uuid)
        )
        now = datetime.now(UTC)
        if existing is None:
            basket_row = BasketModel(
                uuid=basket_def.uuid,
                name=basket_def.name[:160],
                is_public=basket_def.is_public,
                created_at=now,
                updated_at=now,
            )
            self._session.add(basket_row)
            self._session.flush()
        else:
            basket_row = existing
            basket_row.name = basket_def.name[:160]
            basket_row.is_public = basket_def.is_public
            basket_row.updated_at = now
            self._session.execute(
                delete(BasketItemModel).where(BasketItemModel.basket_id == basket_row.id)
            )
            self._session.flush()

        for order_idx, item_spec in enumerate(basket_def.items):
            art_id = (
                self._session.scalar(
                    select(ArticleModel.id).where(ArticleModel.ean == item_spec.ean)
                )
                if item_spec.ean
                else None
            )
            var_id = (
                self._session.scalar(
                    select(GenericVarietyModel.id).where(
                        GenericVarietyModel.slug == item_spec.fallback_variety_slug
                    )
                )
                if item_spec.fallback_variety_slug
                else None
            )
            if var_id is None and art_id is not None:
                var_id = self._session.scalar(
                    select(ArticleModel.generic_variety_id).where(ArticleModel.id == art_id)
                )

            item_row = BasketItemModel(
                basket_id=basket_row.id,
                sort_order=order_idx,
                ean=item_spec.ean,
                article_id=art_id,
                fallback_generic_variety_id=var_id,
                quantity=item_spec.quantity,
                selected=item_spec.selected,
                numerator=item_spec.numerator,
                denominator=item_spec.denominator,
                target_amount=item_spec.target_amount,
                target_unit=item_spec.target_unit.value if item_spec.target_unit else None,
                display_name=item_spec.name[:200],
                display_brand=item_spec.brand[:120] if item_spec.brand else None,
                display_category=item_spec.category[:120] if item_spec.category else None,
            )
            self._session.add(item_row)
            self._session.flush()

            for prio, sub_ean in enumerate(item_spec.substitute_eans, start=1):
                sub_art_id = self._session.scalar(
                    select(ArticleModel.id).where(ArticleModel.ean == sub_ean)
                )
                self._session.add(
                    BasketItemSubstituteModel(
                        basket_item_id=item_row.id,
                        priority=prio,
                        substitute_ean=sub_ean[:32],
                        substitute_article_id=sub_art_id,
                    )
                )

        self._session.commit()
        return basket_row

    def load_basket_definition(self, basket_uuid: str) -> BasketDefinition:
        """Reconstruct a portable BasketDefinition from PostgreSQL by UUID."""
        basket_row = self._session.scalar(
            select(BasketModel).where(BasketModel.uuid == basket_uuid)
        )
        if basket_row is None:
            raise LookupError(f"Basket '{basket_uuid}' not found")

        specs: list[BasketItemSpec] = []
        for item_row in basket_row.items:
            v_slug = (
                self._session.scalar(
                    select(GenericVarietyModel.slug).where(
                        GenericVarietyModel.id == item_row.fallback_generic_variety_id
                    )
                )
                if item_row.fallback_generic_variety_id is not None
                else None
            )
            t_unit = (
                CanonicalUnit(item_row.target_unit)
                if item_row.target_unit and item_row.target_unit in CanonicalUnit._value2member_map_
                else None
            )
            subs = tuple(s.substitute_ean for s in item_row.substitutes)
            specs.append(
                BasketItemSpec(
                    ean=item_row.ean,
                    quantity=item_row.quantity,
                    selected=item_row.selected,
                    numerator=item_row.numerator,
                    denominator=item_row.denominator,
                    name=item_row.display_name,
                    brand=item_row.display_brand,
                    category=item_row.display_category,
                    substitute_eans=subs,
                    fallback_variety_slug=v_slug,
                    target_amount=item_row.target_amount,
                    target_unit=t_unit,
                )
            )

        return BasketDefinition(
            name=basket_row.name,
            uuid=basket_row.uuid,
            is_public=basket_row.is_public,
            created_at=basket_row.created_at.isoformat(),
            items=tuple(specs),
        )

    def evaluate_on_date(
        self,
        basket: BasketDefinition,
        evaluated_date: date,
        geo_level: GeoLevel = GeoLevel.COUNTRY,
        geo_code: str = "AR",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
        persist: bool = True,
    ) -> BasketGeoEvaluation:
        """Evaluate basket at City, Province, INDEC Region, or Country level on `evaluated_date`."""
        window_start = evaluated_date - timedelta(days=max(0, self._active_window_days - 1))

        eans_needed: set[str] = set()
        variety_slugs_needed: set[str] = set()
        for it in basket.items:
            if not it.selected:
                continue
            if it.ean:
                eans_needed.add(it.ean)
            eans_needed.update(it.substitute_eans)
            if it.fallback_variety_slug:
                variety_slugs_needed.add(it.fallback_variety_slug)

        # Fetch pack conversion factors for requested EANs
        ean_pack_factor: dict[str, float] = {}
        if eans_needed:
            for ean_v, factor_v in self._session.execute(
                select(ArticleModel.ean, ArticleModel.unit_conversion_factor).where(
                    ArticleModel.ean.in_(list(eans_needed))
                )
            ).all():
                if ean_v and factor_v and factor_v > 0:
                    ean_pack_factor[str(ean_v)] = float(factor_v)

        if geo_level == GeoLevel.CITY:
            result = self._evaluate_single_city(
                basket,
                evaluated_date,
                window_start,
                geo_code,
                eans_needed,
                variety_slugs_needed,
                ean_pack_factor,
            )
        elif geo_level == GeoLevel.PROVINCE:
            result = self._evaluate_single_province(
                basket,
                evaluated_date,
                window_start,
                geo_code.upper(),
                eans_needed,
                variety_slugs_needed,
                ean_pack_factor,
            )
        elif geo_level == GeoLevel.REGION:
            reg_upper = geo_code.upper()
            reg_spec = next(
                (r for r in self._regional_weights.regions if r.code.upper() == reg_upper),
                None,
            )
            if reg_spec is None:
                raise LookupError(f"Statistical region '{geo_code}' not found")
            prov_evals = [
                self._evaluate_single_province(
                    basket,
                    evaluated_date,
                    window_start,
                    p.code.upper(),
                    eans_needed,
                    variety_slugs_needed,
                    ean_pack_factor,
                )
                for p in reg_spec.provinces
            ]
            w_map = {
                p.code.upper(): (
                    p.population_share
                    if weighting == WeightingScheme.POPULATION
                    else p.expenditure_share_within_region
                )
                for p in reg_spec.provinces
            }
            result = aggregate_provincial_basket_evaluations(
                basket=basket,
                evaluated_date=evaluated_date,
                target_geo_level=GeoLevel.REGION,
                target_geo_code=reg_upper,
                target_geo_name=reg_spec.name,
                provincial_evals=prov_evals,
                province_weights=w_map,
                weighting=weighting,
            )
        else:
            # Country (AR) across all 24 provinces
            all_prov_evals: list[BasketGeoEvaluation] = []
            w_map_nat: dict[str, float] = {}
            for reg in self._regional_weights.regions:
                for p in reg.provinces:
                    p_code = p.code.upper()
                    all_prov_evals.append(
                        self._evaluate_single_province(
                            basket,
                            evaluated_date,
                            window_start,
                            p_code,
                            eans_needed,
                            variety_slugs_needed,
                            ean_pack_factor,
                        )
                    )
                    w_map_nat[p_code] = (
                        p.population_share
                        if weighting == WeightingScheme.POPULATION
                        else (reg.expenditure_weight * p.expenditure_share_within_region)
                    )
            result = aggregate_provincial_basket_evaluations(
                basket=basket,
                evaluated_date=evaluated_date,
                target_geo_level=GeoLevel.COUNTRY,
                target_geo_code="AR",
                target_geo_name="Argentina (Nacional)",
                provincial_evals=all_prov_evals,
                province_weights=w_map_nat,
                weighting=weighting,
            )

        if persist:
            self._persist_evaluation(result)
        return result

    def evaluate_date_series(
        self,
        basket: BasketDefinition,
        dates: list[date],
        geo_level: GeoLevel = GeoLevel.COUNTRY,
        geo_code: str = "AR",
        weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    ) -> list[BasketGeoEvaluation]:
        """Evaluate a basket across a sequence of dates for historical/cross-government tracking."""
        return [
            self.evaluate_on_date(
                basket=basket,
                evaluated_date=dt,
                geo_level=geo_level,
                geo_code=geo_code,
                weighting=weighting,
                persist=True,
            )
            for dt in dates
        ]

    def _evaluate_single_city(
        self,
        basket: BasketDefinition,
        evaluated_date: date,
        window_start: date,
        city_slug_or_name: str,
        eans_needed: set[str],
        variety_slugs_needed: set[str],
        ean_pack_factor: dict[str, float],
    ) -> BasketGeoEvaluation:
        city_row = self._session.scalar(
            select(CityModel).where(
                (CityModel.slug == city_slug_or_name.lower())
                | (CityModel.name.ilike(city_slug_or_name))
            )
        )
        city_name = city_row.name if city_row is not None else city_slug_or_name
        city_id = city_row.id if city_row is not None else -1
        store_eans, store_vars = self._fetch_store_prices(
            evaluated_date=evaluated_date,
            window_start=window_start,
            province_id=None,
            city_id=city_id,
            eans_needed=eans_needed,
            variety_slugs_needed=variety_slugs_needed,
        )
        return evaluate_store_scope_basket(
            basket=basket,
            evaluated_date=evaluated_date,
            geo_level=GeoLevel.CITY,
            geo_code=city_row.slug if city_row is not None else city_slug_or_name,
            geo_name=city_name,
            store_ean_prices=store_eans,
            variety_unit_prices_by_store=store_vars,
            ean_default_pack_amount=ean_pack_factor,
        )

    def _evaluate_single_province(
        self,
        basket: BasketDefinition,
        evaluated_date: date,
        window_start: date,
        province_code: str,
        eans_needed: set[str],
        variety_slugs_needed: set[str],
        ean_pack_factor: dict[str, float],
    ) -> BasketGeoEvaluation:
        prov_row = self._session.scalar(
            select(ProvinceModel).where(ProvinceModel.code == province_code)
        )
        prov_name = prov_row.name if prov_row is not None else province_code
        prov_id = prov_row.id if prov_row is not None else -1
        store_eans, store_vars = self._fetch_store_prices(
            evaluated_date=evaluated_date,
            window_start=window_start,
            province_id=prov_id,
            city_id=None,
            eans_needed=eans_needed,
            variety_slugs_needed=variety_slugs_needed,
        )
        return evaluate_store_scope_basket(
            basket=basket,
            evaluated_date=evaluated_date,
            geo_level=GeoLevel.PROVINCE,
            geo_code=province_code,
            geo_name=prov_name,
            store_ean_prices=store_eans,
            variety_unit_prices_by_store=store_vars,
            ean_default_pack_amount=ean_pack_factor,
        )

    def _fetch_store_prices(
        self,
        *,
        evaluated_date: date,
        window_start: date,
        province_id: int | None,
        city_id: int | None,
        eans_needed: set[str],
        variety_slugs_needed: set[str],
    ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
        store_eans: dict[str, dict[str, int]] = {}
        store_vars: dict[str, dict[str, int]] = {}

        geo_conds = [
            PriceSpanModel.valid_from <= evaluated_date,
            PriceSpanModel.last_seen >= window_start,
            PriceSpanModel.in_stock.is_(True),
        ]
        if province_id is not None:
            geo_conds.append(PointOfSaleModel.province_id == province_id)
        if city_id is not None:
            geo_conds.append(PointOfSaleModel.city_id == city_id)

        if eans_needed:
            ean_rows = self._session.execute(
                select(
                    PointOfSaleModel.code,
                    ArticleModel.ean,
                    PriceSpanModel.list_price_cents,
                    PriceSpanModel.promo_price_cents,
                    PriceSpanModel.valid_from,
                )
                .join(PointOfSaleModel, PointOfSaleModel.id == PriceSpanModel.pos_id)
                .join(ArticleModel, ArticleModel.id == PriceSpanModel.article_id)
                .where(*geo_conds, ArticleModel.ean.in_(list(eans_needed)))
                .order_by(PriceSpanModel.valid_from.asc())
            ).all()
            for pos_code, ean_val, list_c, promo_c, _vf in ean_rows:
                if ean_val is None:
                    continue
                eff_c = int(promo_c) if promo_c is not None and promo_c > 0 else int(list_c)
                store_eans.setdefault(str(pos_code), {})[str(ean_val)] = eff_c

        if variety_slugs_needed:
            var_rows = self._session.execute(
                select(
                    PointOfSaleModel.code,
                    GenericVarietyModel.slug,
                    PriceSpanModel.list_price_cents,
                    PriceSpanModel.promo_price_cents,
                    ArticleModel.unit_conversion_factor,
                )
                .join(PointOfSaleModel, PointOfSaleModel.id == PriceSpanModel.pos_id)
                .join(ArticleModel, ArticleModel.id == PriceSpanModel.article_id)
                .join(
                    GenericVarietyModel,
                    GenericVarietyModel.id == ArticleModel.generic_variety_id,
                )
                .where(*geo_conds, GenericVarietyModel.slug.in_(list(variety_slugs_needed)))
            ).all()
            for pos_code, v_slug, list_c, promo_c, factor in var_rows:
                eff_c = int(promo_c) if promo_c is not None and promo_c > 0 else int(list_c)
                unit_c = compute_unit_price_cents(eff_c, factor)
                s_map = store_vars.setdefault(str(pos_code), {})
                if v_slug not in s_map or unit_c < s_map[v_slug]:
                    s_map[str(v_slug)] = unit_c

        return (store_eans, store_vars)

    def _persist_evaluation(self, ev: BasketGeoEvaluation) -> None:
        basket_row = self._session.scalar(
            select(BasketModel).where(BasketModel.uuid == ev.basket_uuid)
        )
        if basket_row is None:
            return

        existing = self._session.scalar(
            select(BasketEvaluationModel).where(
                BasketEvaluationModel.basket_id == basket_row.id,
                BasketEvaluationModel.evaluated_date == ev.evaluated_date,
                BasketEvaluationModel.geo_level == ev.geo_level.value,
                BasketEvaluationModel.geo_code == ev.geo_code,
                BasketEvaluationModel.weighting == ev.weighting.value,
            )
        )
        if existing is not None:
            self._session.delete(existing)
            self._session.flush()

        ev_row = BasketEvaluationModel(
            basket_id=basket_row.id,
            evaluated_date=ev.evaluated_date,
            geo_level=ev.geo_level.value,
            geo_code=ev.geo_code,
            geo_name=ev.geo_name,
            weighting=ev.weighting.value,
            total_items_count=ev.total_items_count,
            matched_items_count=ev.matched_items_count,
            coverage_ratio=ev.coverage_ratio,
            min_total_cents=ev.min_total_cents,
            geo_mean_reference_total_cents=ev.geo_mean_reference_total_cents,
            max_total_cents=ev.max_total_cents,
            best_single_store_cents=ev.best_single_store_cents,
            best_single_store_code=ev.best_single_store_code,
            worst_single_store_cents=ev.worst_single_store_cents,
            complete_stores_count=ev.complete_stores_count,
        )
        self._session.add(ev_row)
        self._session.flush()

        for it in ev.items:
            self._session.add(
                BasketEvaluationItemModel(
                    evaluation_id=ev_row.id,
                    requested_ean=it.requested_ean,
                    resolved_via=it.resolved_via,
                    resolved_identifier=it.resolved_identifier,
                    effective_multiplier=it.effective_multiplier,
                    min_line_cents=it.min_line_cents,
                    geo_mean_line_cents=it.geo_mean_line_cents,
                    max_line_cents=it.max_line_cents,
                    observing_pos_count=it.observing_pos_count,
                )
            )
        self._session.commit()


__all__ = [
    "EvaluateBasketUseCase",
]
