"""HTMX Catalog & 2-Level Category Editor UI routes (admin-authenticated mutations)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db import (
    ArticleModel,
    CatalogEditLogModel,
    CategoryModel,
    GenericVarietyModel,
    TagModel,
    UnreviewedArticleModel,
    article_tags,
)
from argenprecios.adapters.db.repositories_search import PostgresCatalogSearchRepository
from argenprecios.domain.enums import CurationStatus
from argenprecios.domain.models import SearchFilter
from argenprecios.use_cases.curate_catalog import CurateCatalogUseCase
from argenprecios.web.auth import AuthenticatedAdmin
from argenprecios.web.dependencies import (
    get_current_admin_optional,
    get_db_session,
    get_templates,
    require_admin,
)

router = APIRouter(prefix="/editor", tags=["ui_editor"])


@router.get("", response_class=HTMLResponse)
def editor_workspace(
    request: Request,
    q: str = "",
    department_id: int | None = None,
    subcategory_id: int | None = None,
    brand_id: int | None = None,
    curation_status: str = "",
    min_active_provinces: int = Query(default=0, ge=0, le=24),
    page: int = Query(default=1, ge=1),
    flash: str = "",
    session: Session = Depends(get_db_session),
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    parsed_status = (
        CurationStatus(curation_status)
        if curation_status in CurationStatus._value2member_map_
        else None
    )
    search_repo = PostgresCatalogSearchRepository(session)
    search_page = search_repo.search_articles(
        SearchFilter(
            query=q,
            department_id=department_id,
            subcategory_id=subcategory_id,
            brand_id=brand_id,
            curation_status=parsed_status,
            min_active_provinces=min_active_provinces,
            page=page,
            page_size=25,
        )
    )

    departments = session.scalars(
        select(CategoryModel).where(CategoryModel.level == 1).order_by(CategoryModel.name.asc())
    ).all()
    subcategories = session.scalars(
        select(CategoryModel).where(CategoryModel.level == 2).order_by(CategoryModel.name.asc())
    ).all()
    varieties = session.scalars(
        select(GenericVarietyModel).order_by(GenericVarietyModel.name.asc()).limit(300)
    ).all()
    pending_unreviewed = session.scalars(
        select(UnreviewedArticleModel)
        .where(UnreviewedArticleModel.status == "pending")
        .order_by(UnreviewedArticleModel.id.desc())
        .limit(20)
    ).all()
    recent_logs = session.scalars(
        select(CatalogEditLogModel).order_by(CatalogEditLogModel.id.desc()).limit(15)
    ).all()

    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "admin": admin,
            "q": q,
            "department_id": department_id,
            "subcategory_id": subcategory_id,
            "curation_status": curation_status,
            "min_active_provinces": min_active_provinces,
            "search_page": search_page,
            "departments": departments,
            "subcategories": subcategories,
            "varieties": varieties,
            "pending_unreviewed": pending_unreviewed,
            "recent_logs": recent_logs,
            "flash": flash,
        },
    )


@router.get("/articles/{article_id}/drawer", response_class=HTMLResponse)
def article_edit_drawer(
    article_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    templates: Jinja2Templates = Depends(get_templates),
    admin: AuthenticatedAdmin | None = Depends(get_current_admin_optional),
) -> Response:
    art = session.get(ArticleModel, article_id)
    if art is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artículo no encontrado")
    subcategories = session.scalars(
        select(CategoryModel).where(CategoryModel.level == 2).order_by(CategoryModel.name.asc())
    ).all()
    departments = {
        d.id: d.name for d in session.scalars(select(CategoryModel).where(CategoryModel.level == 1))
    }
    varieties = session.scalars(
        select(GenericVarietyModel).order_by(GenericVarietyModel.name.asc()).limit(300)
    ).all()
    tags_csv = ", ".join(
        session.scalars(
            select(TagModel.name)
            .join(article_tags, article_tags.c.tag_id == TagModel.id)
            .where(article_tags.c.article_id == art.id)
            .order_by(TagModel.name.asc())
        )
    )
    return templates.TemplateResponse(
        request,
        "partials/article_drawer.html",
        {
            "admin": admin,
            "article": art,
            "subcategories": subcategories,
            "departments": departments,
            "varieties": varieties,
            "tags_csv": tags_csv,
        },
    )


@router.post("/articles/{article_id}")
def save_article_edit(
    article_id: int,
    name: str = Form(...),
    clean_description: str = Form(...),
    brand_name: str = Form(""),
    package_amount: str = Form(""),
    package_unit: str = Form(""),
    category_id: int | None = Form(None),
    generic_variety_id: int | None = Form(None),
    tags_csv: str = Form(""),
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    uc = CurateCatalogUseCase(session)
    pkg_amt_val = float(package_amount) if package_amount.strip() else None
    tag_list = [t.strip() for t in tags_csv.split(",") if t.strip()]
    uc.update_article(
        article_id,
        name=name,
        clean_description=clean_description,
        brand_name=brand_name,
        package_amount=pkg_amt_val,
        package_unit=package_unit,
        category_id=category_id,
        generic_variety_id=generic_variety_id,
        tag_names=tag_list,
        actor=admin.username,
    )
    return RedirectResponse(
        url="/editor?flash=Artículo+curado+y+bloqueado+exitosamente",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/articles/bulk")
def bulk_recategorize_submit(
    article_ids_csv: str = Form(...),
    category_id: int | None = Form(None),
    brand_name: str = Form(""),
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    ids = [int(x.strip()) for x in article_ids_csv.split(",") if x.strip().isdigit()]
    if ids:
        uc = CurateCatalogUseCase(session)
        uc.bulk_update_articles(
            ids,
            category_id=category_id,
            brand_name=brand_name.strip() or None,
            actor=admin.username,
        )
    return RedirectResponse(
        url=f"/editor?flash=Actualizados+{len(ids)}+artículos+en+lote",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories")
def create_category_submit(
    name: str = Form(...),
    level: int = Form(...),
    parent_id: str = Form(""),
    description: str = Form(""),
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    uc = CurateCatalogUseCase(session)
    resolved_parent = int(parent_id) if parent_id.strip().isdigit() else None
    uc.create_category(
        name=name,
        level=level,
        parent_id=resolved_parent,
        description=description.strip() or None,
        actor=admin.username,
    )
    return RedirectResponse(
        url="/editor?flash=Categoría+creada",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories/{category_id}/update")
def update_category_submit(
    category_id: int,
    name: str = Form(...),
    new_parent_id: str = Form(""),
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    uc = CurateCatalogUseCase(session)
    parsed_parent = int(new_parent_id) if new_parent_id.strip().isdigit() else None
    uc.update_category(
        category_id=category_id,
        name=name,
        new_parent_id=parsed_parent,
        actor=admin.username,
    )
    return RedirectResponse(
        url="/editor?flash=Categoría+actualizada",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/categories/merge")
def merge_subcategories_submit(
    source_subcategory_id: int = Form(...),
    target_subcategory_id: int = Form(...),
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    uc = CurateCatalogUseCase(session)
    moved = uc.merge_subcategories(
        source_subcategory_id=source_subcategory_id,
        target_subcategory_id=target_subcategory_id,
        actor=admin.username,
    )
    return RedirectResponse(
        url=f"/editor?flash=Subcategorías+fusionadas+({moved}+artículos+reasignados)",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/logs/{log_id}/undo")
def undo_log_submit(
    log_id: int,
    admin: AuthenticatedAdmin = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> RedirectResponse:
    uc = CurateCatalogUseCase(session)
    uc.undo_edit(log_id, actor=admin.username)
    return RedirectResponse(
        url=f"/editor?flash=Cambio+#{log_id}+revertido",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = [
    "router",
]
