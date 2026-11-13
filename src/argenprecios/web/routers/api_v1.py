"""Typed OpenAPI JSON routes (`/api/v1/...`) for search, curation, baskets, CPI, and periods."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db import (
    BasketModel,
    CategoryModel,
    PriceIndexSeriesModel,
    get_database_size_bytes,
)
from argenprecios.adapters.db.repositories_search import PostgresCatalogSearchRepository
from argenprecios.adapters.exporters.basket_yaml import serialize_basket_yaml_str
from argenprecios.config import AppConfig, RegionalWeightsConfig
from argenprecios.domain.enums import CanonicalUnit, CurationStatus, GeoLevel, WeightingScheme
from argenprecios.domain.models import BasketDefinition, BasketItemSpec, SearchFilter
from argenprecios.use_cases.compute_rollups_and_cpi import CompareGovernmentPeriodsUseCase
from argenprecios.use_cases.curate_catalog import CurateCatalogUseCase
from argenprecios.use_cases.evaluate_baskets import EvaluateBasketUseCase
from argenprecios.web.auth import AuthenticatedAdmin
from argenprecios.web.dependencies import (
    get_app_config,
    get_db_session,
    get_regional_weights,
    require_admin,
)

router = APIRouter(prefix="/api/v1", tags=["api_v1"])


class CategoryCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    level: int = Field(ge=1, le=2)
    parent_id: int | None = None
    description: str | None = None


class CategoryUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    new_parent_id: int | None = None


class SubcategoryMergePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_subcategory_id: int
    target_subcategory_id: int


class ArticlePatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    clean_description: str | None = None
    brand_name: str | None = None
    package: str | None = None
    package_amount: float | None = None
    package_unit: str | None = None
    category_id: int | None = None
    generic_variety_id: int | None = None
    tag_names: list[str] | None = None


class BulkArticleUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_ids: list[int]
    category_id: int | None = None
    brand_name: str | None = None
    generic_variety_id: int | None = None


class BasketItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ean: str | None = None
    quantity: int = 1
    selected: bool = True
    numerator: int = 1
    denominator: int = 1
    name: str = ""
    brand: str | None = None
    category: str | None = None
    substitute_eans: list[str] = Field(default_factory=list)
    fallback_variety_slug: str | None = None
    target_amount: float | None = None
    target_unit: CanonicalUnit | None = None


class BasketSavePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    uuid: str
    is_public: bool = True
    items: list[BasketItemPayload]


class BasketEvaluatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluated_date: date
    geo_level: GeoLevel = GeoLevel.COUNTRY
    geo_code: str = "AR"
    weighting: WeightingScheme = WeightingScheme.EXPENDITURE


@router.get("/health")
def get_health(
    session: Session = Depends(get_db_session),
    config: AppConfig = Depends(get_app_config),
) -> dict[str, Any]:
    size_bytes = get_database_size_bytes(session)
    size_gb = round(size_bytes / (1024**3), 4)
    return {
        "status": "ok",
        "database_size_gb": size_gb,
        "storage_budget_gb": config.spans.storage_budget_gb,
        "budget_utilization_pct": round((size_gb / config.spans.storage_budget_gb) * 100.0, 2),
    }


@router.get("/articles")
def search_articles_api(
    q: str = "",
    department_id: int | None = None,
    subcategory_id: int | None = None,
    brand_id: int | None = None,
    tag_slug: str | None = None,
    curation_status: CurationStatus | None = None,
    min_active_provinces: int = Query(default=0, ge=0, le=24),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    repo = PostgresCatalogSearchRepository(session)
    res = repo.search_articles(
        SearchFilter(
            query=q,
            department_id=department_id,
            subcategory_id=subcategory_id,
            brand_id=brand_id,
            tag_slug=tag_slug,
            curation_status=curation_status,
            min_active_provinces=min_active_provinces,
            page=page,
            page_size=page_size,
        )
    )
    return asdict(res)


@router.get("/categories")
def list_categories_tree(session: Session = Depends(get_db_session)) -> list[dict[str, Any]]:
    depts = session.scalars(
        select(CategoryModel).where(CategoryModel.level == 1).order_by(CategoryModel.name.asc())
    ).all()
    subs = session.scalars(
        select(CategoryModel).where(CategoryModel.level == 2).order_by(CategoryModel.name.asc())
    ).all()
    subs_by_parent: dict[int, list[dict[str, Any]]] = {}
    for s in subs:
        if s.parent_id is not None:
            subs_by_parent.setdefault(s.parent_id, []).append(
                {
                    "id": s.id,
                    "name": s.name,
                    "slug": s.slug,
                    "description": s.description,
                    "is_manually_curated": s.is_manually_curated,
                }
            )
    return [
        {
            "id": d.id,
            "name": d.name,
            "slug": d.slug,
            "description": d.description,
            "is_manually_curated": d.is_manually_curated,
            "subcategories": subs_by_parent.get(d.id, []),
        }
        for d in depts
    ]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category_api(
    payload: CategoryCreatePayload,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    try:
        cat = uc.create_category(
            name=payload.name,
            level=payload.level,
            parent_id=payload.parent_id,
            description=payload.description,
            actor=admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "id": cat.id,
        "name": cat.name,
        "slug": cat.slug,
        "level": cat.level,
        "parent_id": cat.parent_id,
        "is_manually_curated": cat.is_manually_curated,
    }


@router.patch("/categories/{category_id}")
def update_category_api(
    category_id: int,
    payload: CategoryUpdatePayload,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    try:
        cat = uc.update_category(
            category_id=category_id,
            name=payload.name,
            description=payload.description,
            new_parent_id=payload.new_parent_id,
            actor=admin.username,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "id": cat.id,
        "name": cat.name,
        "parent_id": cat.parent_id,
        "is_manually_curated": cat.is_manually_curated,
    }


@router.post("/categories/merge")
def merge_subcategories_api(
    payload: SubcategoryMergePayload,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    try:
        reassigned = uc.merge_subcategories(
            source_subcategory_id=payload.source_subcategory_id,
            target_subcategory_id=payload.target_subcategory_id,
            actor=admin.username,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "reassigned_articles": reassigned,
        "target_subcategory_id": payload.target_subcategory_id,
    }


@router.patch("/articles/{article_id}")
def patch_article_api(
    article_id: int,
    payload: ArticlePatchPayload,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    try:
        art = uc.update_article(
            article_id,
            name=payload.name,
            clean_description=payload.clean_description,
            brand_name=payload.brand_name,
            package=payload.package,
            package_amount=payload.package_amount,
            package_unit=payload.package_unit,
            category_id=payload.category_id,
            generic_variety_id=payload.generic_variety_id,
            tag_names=payload.tag_names,
            actor=admin.username,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "id": art.id,
        "ean": art.ean,
        "name": art.name,
        "clean_description": art.clean_description,
        "brand_name": art.brand_name,
        "package_amount": art.package_amount,
        "package_unit": art.package_unit,
        "unit_conversion_factor": art.unit_conversion_factor,
        "category_id": art.category_id,
        "generic_variety_id": art.generic_variety_id,
        "is_manually_curated": art.is_manually_curated,
    }


@router.post("/articles/bulk")
def bulk_update_articles_api(
    payload: BulkArticleUpdatePayload,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    updated = uc.bulk_update_articles(
        payload.article_ids,
        category_id=payload.category_id,
        brand_name=payload.brand_name,
        generic_variety_id=payload.generic_variety_id,
        actor=admin.username,
    )
    return {"updated_count": updated}


@router.post("/edit-logs/{log_id}/undo")
def undo_edit_log_api(
    log_id: int,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    uc = CurateCatalogUseCase(session)
    try:
        entry = uc.undo_edit(log_id, actor=admin.username)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "undone_log_id": entry.id,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
    }


@router.get("/baskets")
def list_baskets_api(session: Session = Depends(get_db_session)) -> list[dict[str, Any]]:
    rows = session.scalars(select(BasketModel).order_by(BasketModel.name.asc())).all()
    return [
        {
            "uuid": r.uuid,
            "name": r.name,
            "is_public": r.is_public,
            "items_count": len(r.items),
        }
        for r in rows
    ]


@router.post("/baskets", status_code=status.HTTP_201_CREATED)
def save_basket_api(
    payload: BasketSavePayload,
    _admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> dict[str, Any]:
    b_def = BasketDefinition(
        name=payload.name,
        uuid=payload.uuid,
        is_public=payload.is_public,
        items=tuple(
            BasketItemSpec(
                ean=it.ean,
                quantity=it.quantity,
                selected=it.selected,
                numerator=it.numerator,
                denominator=it.denominator,
                name=it.name,
                brand=it.brand,
                category=it.category,
                substitute_eans=tuple(it.substitute_eans),
                fallback_variety_slug=it.fallback_variety_slug,
                target_amount=it.target_amount,
                target_unit=it.target_unit,
            )
            for it in payload.items
        ),
    )
    uc = EvaluateBasketUseCase(session, weights)
    saved = uc.save_basket(b_def)
    return {"uuid": saved.uuid, "name": saved.name, "items_count": len(saved.items)}


@router.get("/baskets/{basket_uuid}/yaml")
def export_basket_yaml_api(
    basket_uuid: str,
    strict_sur_en_precios: bool = False,
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> Response:
    uc = EvaluateBasketUseCase(session, weights)
    try:
        b_def = uc.load_basket_definition(basket_uuid)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    yaml_text = serialize_basket_yaml_str(b_def, strict_sur_en_precios_only=strict_sur_en_precios)
    return Response(content=yaml_text, media_type="application/x-yaml")


@router.post("/baskets/{basket_uuid}/evaluate")
def evaluate_basket_api(
    basket_uuid: str,
    payload: BasketEvaluatePayload,
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> dict[str, Any]:
    uc = EvaluateBasketUseCase(session, weights)
    try:
        b_def = uc.load_basket_definition(basket_uuid)
        ev = uc.evaluate_on_date(
            basket=b_def,
            evaluated_date=payload.evaluated_date,
            geo_level=payload.geo_level,
            geo_code=payload.geo_code,
            weighting=payload.weighting,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return asdict(ev)


@router.get("/cpi")
def get_cpi_api(
    geo_level: GeoLevel = GeoLevel.COUNTRY,
    geo_code: str = "AR",
    frequency: str = "monthly",
    weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
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
        {
            "period_date": r.period_date.isoformat(),
            "geo_level": r.geo_level,
            "geo_code": r.geo_code,
            "weighting": r.weighting,
            "index_value": r.index_value,
            "period_pct_change": r.period_pct_change,
            "yoy_pct_change": r.yoy_pct_change,
            "overlapping_varieties_count": r.overlapping_varieties_count,
        }
        for r in rows
    ]


@router.get("/periods/compare")
def compare_periods_api(
    frequency: str = "monthly",
    weighting: WeightingScheme = WeightingScheme.EXPENDITURE,
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    uc = CompareGovernmentPeriodsUseCase(session)
    return [asdict(m) for m in uc.compare_periods(frequency=frequency, weighting=weighting)]


__all__ = [
    "router",
]
