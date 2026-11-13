"""Shared FastAPI request dependencies for database sessions, templates, and admin auth."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from argenprecios.config import AppConfig, RegionalWeightsConfig
from argenprecios.web.auth import (
    AuthenticatedAdmin,
    require_authenticated_mutation,
    resolve_optional_admin,
)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """Yield a scoped SQLAlchemy 2.0 Session per HTTP request."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


def get_app_config(request: Request) -> AppConfig:
    cfg: AppConfig = request.app.state.config
    return cfg


def get_regional_weights(request: Request) -> RegionalWeightsConfig:
    weights: RegionalWeightsConfig = request.app.state.regional_weights
    return weights


def get_templates(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def get_current_admin_optional(
    request: Request,
    session: Session = Depends(get_db_session),
) -> AuthenticatedAdmin | None:
    return resolve_optional_admin(request, session)


async def require_admin(
    request: Request,
    session: Session = Depends(get_db_session),
) -> AuthenticatedAdmin:
    return await require_authenticated_mutation(request, session)


__all__ = [
    "get_app_config",
    "get_current_admin_optional",
    "get_db_session",
    "get_regional_weights",
    "get_templates",
    "require_admin",
]
