"""Integration tests for FastAPI Intranet Dashboard, OpenAPI v1, Editor Auth/CSRF, Merge, & Undo."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_catalog import ArticleModel
from argenprecios.config import AppConfig
from argenprecios.web.app import create_web_app
from argenprecios.web.auth import SessionManager, create_or_update_admin_user


def test_fastapi_public_views_admin_csrf_curation_merge_and_undo(
    isolated_app_config: AppConfig,
    pg_engine: Engine,
    pg_session: Session,
) -> None:
    # Seed an admin user + API token and a sample article to curate
    _admin_user, bearer_token = create_or_update_admin_user(
        pg_session,
        username="editor_chief",
        plain_password="StrongAdminPassword!2026",
        generate_api_token=True,
    )
    assert bearer_token is not None

    test_art = ArticleModel(
        ean="7795555000099",
        name="Galletitas de Agua Original",
        clean_description="galletitas de agua original",
        brand_name="Marca Vieja",
        package="300 g",
        package_amount=300.0,
        package_unit="g",
        unit_conversion_factor=0.3,
        is_reviewed=False,
        is_manually_curated=False,
    )
    pg_session.add(test_art)
    pg_session.commit()
    art_id = test_art.id

    app = create_web_app(config=isolated_app_config, engine=pg_engine)
    with TestClient(app) as client:
        # 1. Public LAN read routes work without login
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/editor?q=galletitas").status_code == 200
        assert client.get("/baskets").status_code == 200

        # 2. Unauthenticated mutation is rejected with 401
        unauth = client.post(
            "/api/v1/categories",
            json={"name": "Depto Intruso", "level": 1},
        )
        assert unauth.status_code == 401

        # 3. Session cookie login + CSRF protection check (missing CSRF -> 403, valid CSRF -> 303)
        login_resp = client.post(
            "/login",
            data={"username": "editor_chief", "password": "StrongAdminPassword!2026"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303
        cookie_val = login_resp.cookies.get(isolated_app_config.web.session_cookie_name)
        assert cookie_val is not None

        sm: SessionManager = app.state.session_manager
        parsed_session = sm.read_session_cookie(cookie_val)
        assert parsed_session is not None

        # Cookie present but missing CSRF token -> 403 Forbidden
        no_csrf = client.post(
            f"/editor/articles/{art_id}",
            data={"name": "Intento Sin CSRF", "clean_description": "intento"},
            follow_redirects=False,
        )
        assert no_csrf.status_code == 403

        # Valid CSRF token -> succeeds (303 redirect) and locks `is_manually_curated = True`
        with_csrf = client.post(
            f"/editor/articles/{art_id}",
            data={
                "csrf_token": parsed_session.csrf_token,
                "name": "Galletitas Criollitas Curadas",
                "clean_description": "galletitas criollitas curadas",
                "brand_name": "Bagley",
                "package_amount": "300",
                "package_unit": "g",
                "tags_csv": "desayuno, clasico",
            },
            follow_redirects=False,
        )
        assert with_csrf.status_code == 303

        # 4. Bearer token JSON API: create Department + 2 Subcategories, merge them, and undo edit
        headers = {"Authorization": f"Bearer {bearer_token}"}
        dept = client.post(
            "/api/v1/categories",
            json={"name": "Panificados Test", "level": 1},
            headers=headers,
        ).json()
        sub_dup = client.post(
            "/api/v1/categories",
            json={"name": "Galletitas Duplicada", "level": 2, "parent_id": dept["id"]},
            headers=headers,
        ).json()
        sub_canonical = client.post(
            "/api/v1/categories",
            json={"name": "Galletitas Saladas", "level": 2, "parent_id": dept["id"]},
            headers=headers,
        ).json()

        # Assign article to sub_dup, then merge sub_dup -> sub_canonical
        patch_resp = client.patch(
            f"/api/v1/articles/{art_id}",
            json={"category_id": sub_dup["id"]},
            headers=headers,
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_manually_curated"] is True

        merge_resp = client.post(
            "/api/v1/categories/merge",
            json={
                "source_subcategory_id": sub_dup["id"],
                "target_subcategory_id": sub_canonical["id"],
            },
            headers=headers,
        )
        assert merge_resp.status_code == 200
        assert merge_resp.json()["reassigned_articles"] == 1

        # Undo the subcategory merge via /api/v1/edit-logs/{log_id}/undo
        pg_session.expire_all()
        refreshed_art = pg_session.get(ArticleModel, art_id)
        assert refreshed_art is not None
        assert refreshed_art.category_id == sub_canonical["id"]

        from sqlalchemy import select

        from argenprecios.adapters.db.models_catalog import CatalogEditLogModel

        merge_log = pg_session.scalar(
            select(CatalogEditLogModel)
            .where(CatalogEditLogModel.action == "merge_subcategories")
            .order_by(CatalogEditLogModel.id.desc())
        )
        assert merge_log is not None
        undo_resp = client.post(
            f"/api/v1/edit-logs/{merge_log.id}/undo",
            headers=headers,
        )
        assert undo_resp.status_code == 200
        pg_session.expire_all()
        restored_art = pg_session.get(ArticleModel, art_id)
        assert restored_art is not None
        assert restored_art.category_id != sub_canonical["id"]
