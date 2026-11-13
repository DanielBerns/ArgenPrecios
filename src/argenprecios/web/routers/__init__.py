"""FastAPI routers package."""

from __future__ import annotations

from argenprecios.web.routers.api_v1 import router as api_v1_router
from argenprecios.web.routers.ui_baskets import router as ui_baskets_router
from argenprecios.web.routers.ui_dashboard import router as ui_dashboard_router
from argenprecios.web.routers.ui_editor import router as ui_editor_router

__all__ = [
    "api_v1_router",
    "ui_baskets_router",
    "ui_dashboard_router",
    "ui_editor_router",
]
