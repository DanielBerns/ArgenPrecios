"""Data source adapters for SEPA, Web Scraping, Hermes Reborn, and Offline datasets."""

from __future__ import annotations

from argenprecios.adapters.sources.hermes_seeder import (
    HermesSeedSummary,
    load_category_lookup,
    seed_default_taxonomy,
    seed_from_hermes_sqlite,
)
from argenprecios.adapters.sources.offline_external_db import (
    ExternalDbAdapter,
    ExternalDbImportProfile,
    load_external_import_profile,
)
from argenprecios.adapters.sources.offline_macro import (
    ingest_macro_csv,
    sync_government_periods,
)
from argenprecios.adapters.sources.offline_staged_captures import (
    promote_staged_observations,
    stage_captures_file,
)
from argenprecios.adapters.sources.offline_survey import SurveyFileAdapter
from argenprecios.adapters.sources.sepa_national import (
    AiohttpSepaDownloader,
    SepaNationalAdapter,
    compute_file_sha256,
    infer_sepa_archive_date,
)
from argenprecios.adapters.sources.web_scrape_vtex import (
    JsonlScrapedRow,
    WebScrapeJsonlAdapter,
    WebScrapeVtexAdapter,
    infer_scrape_date,
)

__all__ = [
    "AiohttpSepaDownloader",
    "ExternalDbAdapter",
    "ExternalDbImportProfile",
    "HermesSeedSummary",
    "JsonlScrapedRow",
    "SepaNationalAdapter",
    "SurveyFileAdapter",
    "WebScrapeJsonlAdapter",
    "WebScrapeVtexAdapter",
    "compute_file_sha256",
    "infer_scrape_date",
    "infer_sepa_archive_date",
    "ingest_macro_csv",
    "load_category_lookup",
    "load_external_import_profile",
    "promote_staged_observations",
    "seed_default_taxonomy",
    "seed_from_hermes_sqlite",
    "stage_captures_file",
    "sync_government_periods",
]
