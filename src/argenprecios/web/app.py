"""FastAPI application factory for the ArgenPrecios Intranet Dashboard, Editor, & OpenAPI v1."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from argenprecios.adapters.db import create_session_factory, create_warehouse_engine
from argenprecios.config import AppConfig, load_config, load_regional_weights
from argenprecios.web.auth import SessionManager
from argenprecios.web.routers import (
    api_v1_router,
    ui_baskets_router,
    ui_dashboard_router,
    ui_editor_router,
)

logger = logging.getLogger(__name__)


def create_web_app(
    config: AppConfig | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Create and wire the FastAPI intranet server on isolated port 8080."""
    cfg = config or load_config()
    owns_engine = engine is None
    db_engine = engine or create_warehouse_engine(cfg.database_url, cfg.database)
    session_factory = create_session_factory(db_engine)
    regional_weights = load_regional_weights(cfg.analytics.regional_weights_path)

    web_root = Path(__file__).resolve().parent
    templates_dir = web_root / "templates"
    static_dir = web_root / "static"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("ArgenPrecios FastAPI web server starting on %s:%d", cfg.web.host, cfg.web.port)
        try:
            yield
        finally:
            if owns_engine:
                db_engine.dispose()
                logger.info("ArgenPrecios FastAPI database engine disposed")

    app = FastAPI(
        title="ArgenPrecios — Almacén Histórico Nacional de Precios",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.config = cfg
    app.state.engine = db_engine
    app.state.session_factory = session_factory
    app.state.regional_weights = regional_weights
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.session_manager = SessionManager(
        secret_key=cfg.secret_key,
        cookie_name=cfg.web.session_cookie_name,
        max_age_seconds=cfg.web.session_max_age_seconds,
    )

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled HTTP error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "error_type": type(exc).__name__},
        )

    app.include_router(api_v1_router)
    app.include_router(ui_dashboard_router)
    app.include_router(ui_editor_router)
    app.include_router(ui_baskets_router)

    return app


__all__ = [
    "create_web_app",
]
