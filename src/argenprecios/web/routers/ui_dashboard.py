"""Intranet Dashboard & Admin Login/Logout UI routes (Jinja2 + HTMX)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from argenprecios.adapters.db import (
    AdminUserModel,
    ArticleModel,
    PointOfSaleModel,
    PriceIndexSeriesModel,
    ProvinceModel,
    get_database_size_bytes,
)
from argenprecios.config import AppConfig
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.use_cases.compute_rollups_and_cpi import CompareGovernmentPeriodsUseCase
from argenprecios.web.auth import (
    AuthenticatedAdmin,
    SessionManager,
    verify_password,
)
from argenprecios.web.dependencies import (
    get_app_config,
    get_current_admin_optional,
    get_db_session,
    get_templates,
    require_admin,
)

router = APIRouter(tags=["ui_dashboard"])


@router.get("/", response_class=HTMLResponse)
def dashboard_home(
    request: Request,
    session: Session = Depends(get_db_session),
    config: AppConfig = Depends(get_app_config),
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    size_gb = round(get_database_size_bytes(session) / (1024**3), 3)
    articles_total = int(session.scalar(select(func.count(ArticleModel.id))) or 0)
    curated_total = int(
        session.scalar(
            select(func.count(ArticleModel.id)).where(ArticleModel.is_manually_curated.is_(True))
        )
        or 0
    )
    pos_total = int(session.scalar(select(func.count(PointOfSaleModel.id))) or 0)

    prov_pos_rows = session.execute(
        select(
            ProvinceModel.code,
            ProvinceModel.name,
            func.count(PointOfSaleModel.id).label("pos_cnt"),
        )
        .outerjoin(PointOfSaleModel, PointOfSaleModel.province_id == ProvinceModel.id)
        .group_by(ProvinceModel.code, ProvinceModel.name)
        .order_by(ProvinceModel.code.asc())
    ).all()

    cpi_rows = session.scalars(
        select(PriceIndexSeriesModel)
        .where(
            PriceIndexSeriesModel.geo_level == GeoLevel.COUNTRY.value,
            PriceIndexSeriesModel.geo_code == "AR",
            PriceIndexSeriesModel.weighting == WeightingScheme.EXPENDITURE.value,
        )
        .order_by(PriceIndexSeriesModel.period_date.desc())
        .limit(24)
    ).all()

    period_comparisons = CompareGovernmentPeriodsUseCase(session).compare_periods()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "size_gb": size_gb,
            "budget_gb": config.spans.storage_budget_gb,
            "articles_total": articles_total,
            "curated_total": curated_total,
            "pos_total": pos_total,
            "province_coverage": prov_pos_rows,
            "cpi_rows": list(reversed(cpi_rows)),
            "period_comparisons": period_comparisons,
        },
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_url: str = "/editor",
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    if admin is not None:
        return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"admin": None, "next_url": next_url, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/editor"),
    session: Session = Depends(get_db_session),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    user = session.scalar(
        select(AdminUserModel).where(
            AdminUserModel.username == username.strip(),
            AdminUserModel.is_active.is_(True),
        )
    )
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "admin": None,
                "next_url": next_url,
                "error": "Credenciales inválidas.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    manager: SessionManager = request.app.state.session_manager
    cookie_val, _csrf = manager.create_session_cookie_value(user.username)
    redirect = RedirectResponse(
        url=next_url if next_url.startswith("/") else "/editor",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    redirect.set_cookie(
        key=manager.cookie_name,
        value=cookie_val,
        max_age=manager.max_age_seconds,
        httponly=True,
        samesite="lax",
    )
    return redirect


@router.post("/logout")
def logout_submit(
    request: Request,
    _admin: AuthenticatedAdmin = Depends(require_admin),
) -> RedirectResponse:
    manager: SessionManager = request.app.state.session_manager
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(key=manager.cookie_name)
    return redirect


__all__ = [
    "router",
]
