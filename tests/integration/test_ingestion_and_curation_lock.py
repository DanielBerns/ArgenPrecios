"""Integration tests for multi-source ingestion (SEPA, VTEX HTML, Survey, Hermes) & manual curation lock."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_catalog import ArticleModel
from argenprecios.adapters.db.models_facts import PriceSpanModel
from argenprecios.adapters.sources.hermes_seeder import (
    seed_default_taxonomy,
    seed_from_hermes_sqlite,
)
from argenprecios.adapters.sources.offline_survey import SurveyFileAdapter
from argenprecios.adapters.sources.sepa_national import SepaNationalAdapter
from argenprecios.adapters.sources.web_scrape_vtex import WebScrapeVtexAdapter
from argenprecios.config import SpanSettings
from argenprecios.use_cases.curate_catalog import CurateCatalogUseCase
from argenprecios.use_cases.ingest_observations import IngestSourceBatchUseCase


def _build_synthetic_sepa_zip(dest_zip: Path, obs_date: str, list_price: str = "1850.00") -> Path:
    chain_buf = io.BytesIO()
    with zipfile.ZipFile(chain_buf, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        inner.writestr(
            "comercio.csv",
            "id_comercio|id_bandera|comercio_bandera_nombre\n10|1|Supermercado Federal\n",
        )
        inner.writestr(
            "sucursales.csv",
            (
                "id_comercio|id_bandera|id_sucursal|sucursales_nombre|sucursales_tipo|"
                "sucursales_calle|sucursales_numero|sucursales_localidad|sucursales_provincia|"
                "sucursales_latitud|sucursales_longitud\n"
                "10|1|101|Sucursal Trelew|Hipermercado|San Martin|123|Trelew|AR-U|-43.25|-65.30\n"
                "10|1|102|Sucursal Caballito|Supermercado|Rivadavia|5000|CABA|AR-C|-34.61|-58.43\n"
            ),
        )
        inner.writestr(
            "productos.csv",
            (
                "id_comercio|id_bandera|id_sucursal|id_producto|productos_ean|"
                "productos_descripcion|productos_cantidad_presentacion|"
                "productos_unidad_medida_presentacion|productos_marca|productos_precio_lista|"
                "productos_precio_unitario_promo1|productos_leyenda_promo1|"
                "productos_precio_unitario_promo2|productos_leyenda_promo2\n"
                f"10|1|101|7791111000018|1|AZUCAR COMUN TIPO A LEDESMA 1 KG|1|kg|LEDESMA|{list_price}|1600.00|Oferta||||\n"
                f"10|1|102|7791111000018|1|AZUCAR COMUN TIPO A LEDESMA 1 KG|1|kg|LEDESMA|{list_price}|||||\n"
            ),
        )

    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr(f"sepa_comercio_10_{obs_date}.zip", chain_buf.getvalue())
    return dest_zip


def _build_synthetic_hermes_sqlite(db_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY, name TEXT, slug TEXT, level INTEGER,
                description TEXT, parent_id INTEGER
            );
            CREATE TABLE brands (id INTEGER PRIMARY KEY, name TEXT, slug TEXT);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, slug TEXT);
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY, ean TEXT, name TEXT, clean_description TEXT,
                raw_description TEXT, brand_id INTEGER, brand_name TEXT,
                package TEXT, package_amount REAL, package_unit TEXT, category_id INTEGER
            );
            CREATE TABLE article_tags (article_id INTEGER, tag_id INTEGER);
            INSERT INTO categories VALUES (1, 'Almacén', 'almacen', 1, 'Depto', NULL);
            INSERT INTO categories VALUES (2, 'Endulzantes', 'almacen-endulzantes', 2, 'Sub', 1);
            INSERT INTO brands VALUES (1, 'Ledesma', 'ledesma');
            INSERT INTO tags VALUES (1, 'sin-tacc', 'sin-tacc');
            INSERT INTO articles VALUES (
                1, '7791111000018', 'Azúcar Original Hermes', 'azucar original hermes',
                'AZUCAR HERMES 1KG', 1, 'Ledesma', '1 kg', 1.0, 'kg', 2
            );
            INSERT INTO article_tags VALUES (1, 1);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_multi_source_ingestion_span_bridging_and_manual_curation_lock(
    pg_session: Session,
    tmp_path: Path,
) -> None:
    seed_default_taxonomy(pg_session)
    pg_session.commit()

    # 1. Seed from synthetic hermes_clean.db (?mode=ro)
    hermes_db = _build_synthetic_hermes_sqlite(tmp_path / "hermes_test.db")
    seed_summary = seed_from_hermes_sqlite(pg_session, hermes_db)
    assert seed_summary.articles_upserted >= 1

    # 2. Ingest Day 1 SEPA multi-province archive (read-only) and verify temp cleanup
    temp_extract = tmp_path / "private_tmp"
    temp_extract.mkdir(parents=True, exist_ok=True)
    sepa_adapter = SepaNationalAdapter(temp_extract_dir=temp_extract)
    ingestor = IngestSourceBatchUseCase(pg_session, SpanSettings(max_bridge_gap_days=2))

    zip_day1 = _build_synthetic_sepa_zip(tmp_path / "sepa_2026-04-01.zip", "2026-04-01")
    mtime_before = zip_day1.stat().st_mtime_ns
    res1 = ingestor.ingest_from_adapter(sepa_adapter, zip_day1)
    assert res1.rows_read == 2
    assert res1.spans_inserted == 2
    assert zip_day1.stat().st_mtime_ns == mtime_before
    assert list(temp_extract.iterdir()) == []

    # Idempotency check: re-ingesting same SHA-256 archive is skipped cleanly
    res_dup = ingestor.ingest_from_adapter(sepa_adapter, zip_day1)
    assert res_dup.skipped_duplicate is True

    # 3. Manually curate article via CurateCatalogUseCase -> sets `is_manually_curated = True`
    art = pg_session.scalar(select(ArticleModel).where(ArticleModel.ean == "7791111000018"))
    assert art is not None
    curator = CurateCatalogUseCase(pg_session)
    curator.update_article(
        art.id,
        name="Azúcar Blanca Refinada Curada Manualmente",
        brand_name="Ledesma Selección",
        actor="editor_test",
    )

    # 4. Ingest Day 3 SEPA (2-day bridge gap: 2026-04-01 -> 2026-04-03 at same price) AND re-seed Hermes
    zip_day3 = _build_synthetic_sepa_zip(tmp_path / "sepa_2026-04-03.zip", "2026-04-03")
    res3 = ingestor.ingest_from_adapter(sepa_adapter, zip_day3)
    assert res3.spans_extended == 2
    assert res3.spans_inserted == 0

    reseed = seed_from_hermes_sqlite(pg_session, hermes_db)
    assert reseed.curated_articles_skipped >= 1

    pg_session.expire_all()
    locked_art = pg_session.scalar(select(ArticleModel).where(ArticleModel.ean == "7791111000018"))
    assert locked_art is not None
    assert locked_art.is_manually_curated is True
    assert locked_art.name == "Azúcar Blanca Refinada Curada Manualmente"
    assert locked_art.brand_name == "Ledesma Selección"

    spans = pg_session.scalars(
        select(PriceSpanModel).where(PriceSpanModel.article_id == locked_art.id)
    ).all()
    assert len(spans) == 2
    assert all(sp.observed_days == 2 for sp in spans)

    # 5. VTEX HTML JSON-LD ingestion (`mpn` 13-digit EAN recovery)
    scrape_dir = tmp_path / "samples" / "20260404103000" / "carrefour"
    scrape_dir.mkdir(parents=True, exist_ok=True)
    (scrape_dir / "item_01.html").write_text(
        """<!DOCTYPE html><html><head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org/",
          "@type": "Product",
          "name": "Leche Entera La Serenisima Sachet 1 L",
          "mpn": "7790742000105",
          "sku": "CRF-991",
          "brand": {"@type": "Brand", "name": "La Serenisima"},
          "offers": {
            "@type": "AggregateOffer",
            "lowPrice": 1350.00,
            "highPrice": 1500.00,
            "availability": "https://schema.org/InStock"
          }
        }
        </script></head><body></body></html>""",
        encoding="utf-8",
    )
    vtex_res = ingestor.ingest_from_adapter(WebScrapeVtexAdapter(), scrape_dir)
    assert vtex_res.rows_read == 1
    web_art = pg_session.scalar(select(ArticleModel).where(ArticleModel.ean == "7790742000105"))
    assert web_art is not None

    # 6. Offline Street Survey CSV ingestion
    survey_csv = tmp_path / "relevamiento_barrial.csv"
    survey_csv.write_text(
        "date,province_code,city,pos_name,pos_channel,ean,raw_description,quantity,unit,list_price\n"
        "2026-04-04,AR-U,Comodoro Rivadavia,Carnicería Don José,butchery,,Asado Vacuno Especial,1,kg,9800.00\n",
        encoding="utf-8",
    )
    survey_res = ingestor.ingest_from_adapter(SurveyFileAdapter(), survey_csv)
    assert survey_res.rows_read == 1
