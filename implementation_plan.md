# Implementation Plan — ArgenPrecios

## Phases Delivered

1. **Phase 1 — Repository Scaffolding, Configuration, PostgreSQL Schema & Hermes Seeding**:
   - `pyproject.toml` (consolidated `uv`, `ruff` incl. `PTH` & `T20`, `mypy --strict`, `pytest`, `coverage`), `compose.yaml` (`postgres:16-alpine` on `127.0.0.1:5433`), `config/*.yaml`.
   - SQLAlchemy 2.0 declarative schema (`models_geo.py`, `models_catalog.py`, `models_facts.py`, `models_analytics.py`), Alembic migration `0001_initial_warehouse.py`, `partition_manager.py`, and read-only `hermes_seeder.py`.
2. **Phase 2 — Lossless `price_spans` Engine & National SEPA Pipeline**:
   - `unit_normalizer.py`, `promo_parser.py`, `span_compressor.py` (`max_bridge_gap_days = 2` + historical backfill timeline reconciliation), `SepaNationalAdapter`, `AiohttpSepaDownloader`, and `IngestSourceBatchUseCase`.
3. **Phase 3 — Supermarket Web Scraper & Offline Source Adapters**:
   - `WebScrapeVtexAdapter` (VTEX HTML JSON-LD `mpn` 13-digit EAN/PLU recovery) + `WebScrapeJsonlAdapter`, `SurveyFileAdapter` (CSV/XLSX/YAML), `ExternalDbAdapter` (SQLite `?mode=ro` & Parquet), `offline_macro.py`, and `offline_staged_captures.py`.
4. **Phase 4 — Faceted Search, Catalog Editor Logic, Basket Evaluator & CPI Engine**:
   - `PostgresCatalogSearchRepository` (`unaccent` + Spanish `tsvector` + `pg_trgm` + active provincial/chain availability counts), `CurateCatalogUseCase` (`is_manually_curated = True` lock + `catalog_edit_logs` undo), `EvaluateBasketUseCase` + `basket_yaml.py`, `ComputeRollupsAndCpiUseCase`, and `CompareGovernmentPeriodsUseCase`.
5. **Phase 5 — CLI, Python SDK, Report Exporters & FastAPI Intranet App**:
   - `WarehouseClient` SDK, HTML/Markdown/CSV/Parquet `reports.py`, FastAPI + Jinja2/HTMX web server (`:8080`) with `bcrypt` + signed session/CSRF + Bearer token auth, `argenprecios` CLI, `README.md`, and unit + integration test suites.
