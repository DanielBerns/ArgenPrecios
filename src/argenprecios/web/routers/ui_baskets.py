"""HTMX Basket Studio UI routes: search-to-basket builder, YAML import/export, & evaluation."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argenprecios.adapters.db import ArticleModel, BasketModel, PriceSpanModel
from argenprecios.adapters.db.repositories_search import PostgresCatalogSearchRepository
from argenprecios.adapters.exporters.basket_yaml import parse_basket_yaml_str
from argenprecios.config import RegionalWeightsConfig
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.domain.models import (
    BasketDefinition,
    BasketGeoEvaluation,
    BasketItemSpec,
    SearchFilter,
)
from argenprecios.use_cases.evaluate_baskets import EvaluateBasketUseCase
from argenprecios.web.auth import AuthenticatedAdmin
from argenprecios.web.dependencies import (
    get_current_admin_optional,
    get_db_session,
    get_regional_weights,
    get_templates,
    require_admin,
)

router = APIRouter(prefix="/baskets", tags=["ui_baskets"])


@router.get("", response_class=HTMLResponse)
def baskets_index(
    request: Request,
    session: Session = Depends(get_db_session),
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    baskets = session.scalars(select(BasketModel).order_by(BasketModel.name.asc())).all()
    return templates.TemplateResponse(
        request,
        "baskets.html",
        {"admin": admin, "baskets": baskets},
    )


@router.post("/create")
def create_basket_submit(
    name: str = Form(...),
    _admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> RedirectResponse:
    new_uuid = str(uuid.uuid4())
    uc = EvaluateBasketUseCase(session, weights)
    uc.save_basket(BasketDefinition(name=name.strip(), uuid=new_uuid, is_public=True, items=()))
    return RedirectResponse(url=f"/baskets/{new_uuid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/import-yaml")
def import_basket_yaml_submit(
    yaml_text: str = Form(...),
    _admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> RedirectResponse:
    b_def = parse_basket_yaml_str(yaml_text)
    uc = EvaluateBasketUseCase(session, weights)
    uc.save_basket(b_def)
    return RedirectResponse(url=f"/baskets/{b_def.uuid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{basket_uuid}", response_class=HTMLResponse)
def basket_detail_studio(
    basket_uuid: str,
    request: Request,
    q: str = "",
    eval_date: str = "",
    geo_level: str = "country",
    geo_code: str = "AR",
    weighting: str = "expenditure",
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    uc = EvaluateBasketUseCase(session, weights)
    try:
        basket_def = uc.load_basket_definition(basket_uuid)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    search_results = None
    if q.strip():
        search_results = PostgresCatalogSearchRepository(session).search_articles(
            SearchFilter(query=q.strip(), page=1, page_size=12)
        )

    latest_date = session.scalar(select(func.max(PriceSpanModel.last_seen))) or date.today()
    target_date = date.fromisoformat(eval_date[:10]) if eval_date.strip() else latest_date
    g_lvl = GeoLevel(geo_level) if geo_level in GeoLevel._value2member_map_ else GeoLevel.COUNTRY
    w_scheme = (
        WeightingScheme(weighting)
        if weighting in WeightingScheme._value2member_map_
        else WeightingScheme.EXPENDITURE
    )

    evaluation: BasketGeoEvaluation | None = None
    if basket_def.items:
        evaluation = uc.evaluate_on_date(
            basket=basket_def,
            evaluated_date=target_date,
            geo_level=g_lvl,
            geo_code=geo_code,
            weighting=w_scheme,
            persist=False,
        )

    return templates.TemplateResponse(
        request,
        "basket_detail.html",
        {
            "admin": admin,
            "basket": basket_def,
            "q": q,
            "search_results": search_results,
            "eval_date": target_date.isoformat(),
            "geo_level": g_lvl.value,
            "geo_code": geo_code,
            "weighting": w_scheme.value,
            "evaluation": evaluation,
            "regions": weights.regions,
        },
    )


@router.post("/{basket_uuid}/items/add")
def add_item_to_basket_submit(
    basket_uuid: str,
    article_id: int = Form(...),
    quantity: int = Form(1),
    numerator: int = Form(1),
    denominator: int = Form(1),
    substitute_eans_csv: str = Form(""),
    fallback_variety_slug: str = Form(""),
    _admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> RedirectResponse:
    art = session.get(ArticleModel, article_id)
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artículo no encontrado")

    uc = EvaluateBasketUseCase(session, weights)
    existing = uc.load_basket_definition(basket_uuid)
    subs = tuple(s.strip() for s in substitute_eans_csv.split(",") if s.strip())
    var_slug = fallback_variety_slug.strip() or (
        art.generic_variety.slug if art.generic_variety else None
    )

    new_item = BasketItemSpec(
        ean=art.ean,
        quantity=max(1, quantity),
        selected=True,
        numerator=max(1, numerator),
        denominator=max(1, denominator),
        name=art.name,
        brand=art.brand_name,
        category=art.category.name if art.category else None,
        substitute_eans=subs,
        fallback_variety_slug=var_slug,
        target_amount=art.unit_conversion_factor,
    )
    updated = BasketDefinition(
        name=existing.name,
        uuid=existing.uuid,
        is_public=existing.is_public,
        created_at=existing.created_at,
        items=(*existing.items, new_item),
    )
    uc.save_basket(updated)
    return RedirectResponse(url=f"/baskets/{basket_uuid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{basket_uuid}/items/{item_index}/remove")
def remove_item_from_basket_submit(
    basket_uuid: str,
    item_index: int,
    _admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
    weights: RegionalWeightsConfig = Depends(get_regional_weights),
) -> RedirectResponse:
    uc = EvaluateBasketUseCase(session, weights)
    existing = uc.load_basket_definition(basket_uuid)
    remaining = tuple(it for idx, it in enumerate(existing.items) if idx != item_index)
    uc.save_basket(
        BasketDefinition(
            name=existing.name,
            uuid=existing.uuid,
            is_public=existing.is_public,
            created_at=existing.created_at,
            items=remaining,
        )
    )
    return RedirectResponse(url=f"/baskets/{basket_uuid}", status_code=status.HTTP_303_SEE_OTHER)


__all__ = [
    "router",
]
