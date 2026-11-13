"""Unified CLI entrypoint for ArgenPrecios warehouse operations, curation, baskets, & FastAPI."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import getpass
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from argenprecios.adapters.db import create_session_factory, create_warehouse_engine
from argenprecios.adapters.db.migrations.runner import upgrade_warehouse_head
from argenprecios.adapters.db.partition_manager import (
    compact_cold_partition_to_chain_city,
    ensure_partitions_window,
    get_database_size_bytes,
    seal_historical_partition,
)
from argenprecios.adapters.exporters import (
    export_report_bundle,
    load_basket_yaml_file,
    save_basket_yaml_file,
)
from argenprecios.adapters.sources import (
    AiohttpSepaDownloader,
    ExternalDbAdapter,
    SepaNationalAdapter,
    SurveyFileAdapter,
    WebScrapeJsonlAdapter,
    WebScrapeVtexAdapter,
    ingest_macro_csv,
    load_external_import_profile,
    promote_staged_observations,
    seed_default_taxonomy,
    seed_from_hermes_sqlite,
    stage_captures_file,
    sync_government_periods,
)
from argenprecios.config import AppConfig, load_config, load_regional_weights, resolve_user_path
from argenprecios.domain.enums import GeoLevel, WeightingScheme
from argenprecios.logging_setup import configure_logging
from argenprecios.sdk.client import WarehouseClient
from argenprecios.use_cases import (
    ComputeRollupsAndCpiUseCase,
    CurateCatalogUseCase,
    EvaluateBasketUseCase,
    IngestSourceBatchUseCase,
)
from argenprecios.web.app import create_web_app
from argenprecios.web.auth import create_or_update_admin_user

logger = logging.getLogger(__name__)


def _emit_line(message: str = "") -> None:
    """Write structured/CLI output to stdout without using banned print()."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _emit_json(payload: Any) -> None:
    """Serialize dataclass or dict results to formatted JSON on stdout."""
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    _emit_line(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level `argenprecios` argument parser and all subcommands."""
    parser = argparse.ArgumentParser(
        prog="argenprecios",
        description="ArgenPrecios — Country-level multi-source historical price warehouse & CPI engine",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to YAML configuration file")
    parser.add_argument("--workdir", type=Path, default=None, help="Runtime working directory")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Structured JSON log verbosity",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. init
    p_init = sub.add_parser(
        "init",
        help="Create owned workdir folders, run `alembic upgrade head`, and seed taxonomy/periods",
    )
    p_init.add_argument(
        "--skip-db",
        action="store_true",
        help="Only initialize owned filesystem directories and config.yaml without DB migration",
    )

    # 2. create-admin
    p_admin = sub.add_parser(
        "create-admin",
        help="Bootstrap or rotate an administrator user and API Bearer token",
    )
    p_admin.add_argument("--username", default="admin", help="Administrator username")
    p_admin.add_argument(
        "--password",
        default=None,
        help="Administrator password (prefer ARGENPRECIOS_ADMIN_PASSWORD env or interactive prompt)",
    )
    p_admin.add_argument(
        "--no-token",
        action="store_true",
        help="Do not issue/rotate an API Bearer token",
    )

    # 3. seed-hermes
    p_hermes = sub.add_parser(
        "seed-hermes",
        help="Seed categories, brands, tags, and articles from hermes_clean.db (?mode=ro)",
    )
    p_hermes.add_argument(
        "--hermes-db",
        type=Path,
        default=None,
        help="Explicit path to hermes_clean.db (defaults to config sources.hermes_clean_db_path)",
    )

    # 4. ingest
    p_ingest = sub.add_parser(
        "ingest", help="Ingest observations from SEPA, Web, or Offline sources"
    )
    ingest_sub = p_ingest.add_subparsers(dest="ingest_source", required=True)

    p_in_sepa = ingest_sub.add_parser(
        "sepa",
        help="Ingest daily national 24-province SEPA archive (read-only on sur_en_precios)",
    )
    p_in_sepa.add_argument("--archive", type=Path, default=None, help="Explicit SEPA .zip or dir")
    p_in_sepa.add_argument(
        "--date", type=date.fromisoformat, default=None, help="Target YYYY-MM-DD"
    )
    p_in_sepa.add_argument(
        "--download-if-missing",
        action="store_true",
        help="Download into ArgenPrecios fallback dir if absent from sur_en_precios",
    )
    p_in_sepa.add_argument("--force", action="store_true", help="Re-ingest even if SHA-256 exists")

    p_in_backfill = ingest_sub.add_parser(
        "sepa-backfill",
        help="Ingest historical multi-year SEPA archives with timeline reconciliation",
    )
    p_in_backfill.add_argument(
        "--dir", type=Path, default=None, help="Directory with SEPA .zip files"
    )
    p_in_backfill.add_argument("--from-date", type=date.fromisoformat, default=None)
    p_in_backfill.add_argument("--to-date", type=date.fromisoformat, default=None)
    p_in_backfill.add_argument("--force", action="store_true")

    p_in_web = ingest_sub.add_parser(
        "web-scrape",
        help="Ingest VTEX HTML captures (recovering JSON-LD `mpn` EANs) read-only",
    )
    p_in_web.add_argument(
        "--path", type=Path, default=None, help="Specific run folder or HTML file"
    )
    p_in_web.add_argument("--province", default="AR-C", help="Default ISO province code")
    p_in_web.add_argument("--city", default="Tienda Online", help="Default online city label")
    p_in_web.add_argument("--force", action="store_true")

    p_in_jsonl = ingest_sub.add_parser(
        "web-jsonl",
        help="Ingest canonical .jsonl supermarket scraper output",
    )
    p_in_jsonl.add_argument("--file", type=Path, required=True, help="Path to .jsonl file")
    p_in_jsonl.add_argument("--backfill", action="store_true")
    p_in_jsonl.add_argument("--force", action="store_true")

    p_in_survey = ingest_sub.add_parser(
        "survey",
        help="Ingest offline street/field price surveys (CSV, XLSX, or YAML)",
    )
    p_in_survey.add_argument("--file", type=Path, required=True, help="Path to survey file")
    p_in_survey.add_argument("--backfill", action="store_true")
    p_in_survey.add_argument("--force", action="store_true")

    p_in_ext = ingest_sub.add_parser(
        "external-db",
        help="Import historical SQLite (?mode=ro) or Parquet dataset via YAML mapping profile",
    )
    p_in_ext.add_argument(
        "--file", type=Path, required=True, help="External .db/.sqlite or .parquet"
    )
    p_in_ext.add_argument("--profile", type=Path, required=True, help="YAML column mapping profile")
    p_in_ext.add_argument("--force", action="store_true")

    p_in_macro = ingest_sub.add_parser(
        "macro",
        help="Ingest official INDEC/CBA/CBT/RIPTE/FX CSV series and/or sync government periods",
    )
    p_in_macro.add_argument("--file", type=Path, default=None, help="Macro reference CSV path")
    p_in_macro.add_argument(
        "--sync-periods",
        action="store_true",
        help="Sync government_periods.yaml into PostgreSQL",
    )

    p_in_stage = ingest_sub.add_parser(
        "stage-captures",
        help="Stage semi-structured receipt/PDF extractions (JSON or CSV) for human review",
    )
    p_in_stage.add_argument("--file", type=Path, required=True, help="Path to JSON/CSV captures")

    p_in_promote = ingest_sub.add_parser(
        "promote-staged",
        help="Promote verified staged_observations IDs into canonical price_spans",
    )
    p_in_promote.add_argument(
        "--ids",
        required=True,
        help="Comma-separated staged observation IDs (e.g. 1,2,3)",
    )

    # 5. search
    p_search = sub.add_parser("search", help="Faceted accent-insensitive & trigram article search")
    p_search.add_argument("query", nargs="?", default="", help="Free-text or EAN query")
    p_search.add_argument("--dept", type=int, default=None, help="Level 1 Department ID")
    p_search.add_argument("--subcat", type=int, default=None, help="Level 2 Subcategory ID")
    p_search.add_argument("--min-provinces", type=int, default=0, help="Minimum active provinces")
    p_search.add_argument("--limit", type=int, default=25, help="Page size")

    # 6. curate
    p_curate = sub.add_parser(
        "curate",
        help="2-level category hierarchy & article curation with manual lock and undo",
    )
    cur_sub = p_curate.add_subparsers(dest="curate_action", required=True)

    p_c_cat = cur_sub.add_parser("create-category", help="Create Department (1) or Subcategory (2)")
    p_c_cat.add_argument("--name", required=True)
    p_c_cat.add_argument("--level", type=int, choices=(1, 2), required=True)
    p_c_cat.add_argument("--parent-id", type=int, default=None)
    p_c_cat.add_argument("--description", default=None)
    p_c_cat.add_argument("--actor", default="cli_admin")

    p_c_merge = cur_sub.add_parser(
        "merge-subcategories",
        help="Merge source Level 2 Subcategory into target Level 2 Subcategory",
    )
    p_c_merge.add_argument("--source-id", type=int, required=True)
    p_c_merge.add_argument("--target-id", type=int, required=True)
    p_c_merge.add_argument("--actor", default="cli_admin")

    p_c_art = cur_sub.add_parser(
        "update-article",
        help="Curate an article and lock `is_manually_curated = True`",
    )
    p_c_art.add_argument("--article-id", type=int, required=True)
    p_c_art.add_argument("--name", default=None)
    p_c_art.add_argument("--clean-description", default=None)
    p_c_art.add_argument("--brand", default=None)
    p_c_art.add_argument("--package-amount", type=float, default=None)
    p_c_art.add_argument("--package-unit", default=None)
    p_c_art.add_argument("--category-id", type=int, default=None)
    p_c_art.add_argument("--variety-id", type=int, default=None)
    p_c_art.add_argument("--tags", default=None, help="Comma-separated tag names")
    p_c_art.add_argument("--actor", default="cli_admin")

    p_c_undo = cur_sub.add_parser("undo", help="Revert an edit from catalog_edit_logs")
    p_c_undo.add_argument("--log-id", type=int, required=True)
    p_c_undo.add_argument("--actor", default="cli_admin")

    # 7. basket
    p_basket = sub.add_parser("basket", help="Import, export, and evaluate multi-level baskets")
    bsk_sub = p_basket.add_subparsers(dest="basket_action", required=True)

    p_b_imp = bsk_sub.add_parser("import-yaml", help="Import sur_en_precios-compatible basket YAML")
    p_b_imp.add_argument("--file", type=Path, required=True)

    p_b_exp = bsk_sub.add_parser("export-yaml", help="Export basket by UUID to YAML")
    p_b_exp.add_argument("--uuid", required=True)
    p_b_exp.add_argument("--output", type=Path, required=True)

    p_b_eval = bsk_sub.add_parser(
        "evaluate",
        help="Evaluate a basket at City, Province, Region, or Country level",
    )
    b_group = p_b_eval.add_mutually_exclusive_group(required=True)
    b_group.add_argument("--uuid", default=None, help="Stored basket UUID")
    b_group.add_argument("--file", type=Path, default=None, help="Direct YAML basket path")
    p_b_eval.add_argument("--date", type=date.fromisoformat, default=date.today())
    p_b_eval.add_argument(
        "--geo-level",
        choices=("city", "province", "region", "country"),
        default="country",
    )
    p_b_eval.add_argument("--geo-code", default="AR")
    p_b_eval.add_argument(
        "--weighting",
        choices=("expenditure", "population", "unweighted"),
        default="expenditure",
    )

    # 8. rollup
    p_rollup = sub.add_parser(
        "rollup",
        help="Compute Stage 1 Jevons variety rollups and Stage 2 Regional/National CPI series",
    )
    p_rollup.add_argument("--from-date", type=date.fromisoformat, required=True)
    p_rollup.add_argument("--to-date", type=date.fromisoformat, required=True)
    p_rollup.add_argument("--frequency", choices=("monthly", "daily"), default="monthly")
    p_rollup.add_argument("--channel-scope", default="all")

    # 9. report
    p_report = sub.add_parser(
        "report",
        help="Generate national CPI & cross-government reports in HTML, Markdown, CSV, & Parquet",
    )
    p_report.add_argument(
        "--title", default="Informe Nacional de Precios e Inflación — ArgenPrecios"
    )
    p_report.add_argument("--stem", default="informe_nacional")
    p_report.add_argument("--formats", default="html,md,csv,parquet")
    p_report.add_argument("--output-dir", type=Path, default=None)
    p_report.add_argument("--frequency", choices=("monthly", "daily"), default="monthly")
    p_report.add_argument(
        "--weighting",
        choices=("expenditure", "population", "unweighted"),
        default="expenditure",
    )

    # 10. partition-maintenance
    p_part = sub.add_parser(
        "partition-maintenance",
        help="Manage quarterly price_spans partitions, fillfactor sealing, and 500 GB budget",
    )
    part_sub = p_part.add_subparsers(dest="partition_action", required=True)

    p_pm_ensure = part_sub.add_parser("ensure", help="Pre-create contiguous quarterly partitions")
    p_pm_ensure.add_argument("--date", type=date.fromisoformat, default=date.today())
    p_pm_ensure.add_argument("--quarters-back", type=int, default=1)
    p_pm_ensure.add_argument("--quarters-ahead", type=int, default=2)

    p_pm_seal = part_sub.add_parser("seal", help="Set fillfactor=100 on closed historical quarter")
    p_pm_seal.add_argument("--year", type=int, required=True)
    p_pm_seal.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), required=True)
    p_pm_seal.add_argument("--vacuum-freeze", action="store_true")

    p_pm_compact = part_sub.add_parser(
        "compact-cold",
        help="Compact cold branch spans into chain_city_spans safety-valve table",
    )
    p_pm_compact.add_argument("--year", type=int, required=True)
    p_pm_compact.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), required=True)
    p_pm_compact.add_argument("--drop-branch-rows", action="store_true")

    part_sub.add_parser("status", help="Inspect current PostgreSQL disk footprint vs 500 GB budget")

    # 11. serve
    p_serve = sub.add_parser(
        "serve",
        help="Launch FastAPI Intranet Dashboard, Catalog Editor, & OpenAPI v1 server (default :8080)",
    )
    p_serve.add_argument("--host", default=None, help="Override bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=None, help="Override bind port (default 8080)")

    return parser


def _write_default_config_if_absent(cfg: AppConfig) -> Path:
    target = cfg.workdir / "config.yaml"
    if not target.exists():
        safe_payload = {
            "workdir": str(cfg.workdir),
            "database": cfg.database.model_dump(mode="json"),
            "sources": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in cfg.sources.model_dump().items()
            },
            "spans": cfg.spans.model_dump(mode="json"),
            "analytics": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in cfg.analytics.model_dump().items()
            },
            "web": cfg.web.model_dump(mode="json"),
        }
        target.write_text(yaml.safe_dump(safe_payload, sort_keys=False), encoding="utf-8")
    return target


def _handle_init(cfg: AppConfig, args: argparse.Namespace) -> int:
    cfg.ensure_owned_directories()
    cfg_file = _write_default_config_if_absent(cfg)
    if args.skip_db:
        _emit_json({"status": "initialized_filesystem", "config_file": str(cfg_file)})
        return 0

    upgrade_warehouse_head(database_url=cfg.database_url)
    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        session_factory = create_session_factory(engine)
        with engine.begin() as conn:
            partitions = ensure_partitions_window(
                conn,
                anchor_date=date.today(),
                quarters_back=1,
                quarters_ahead=cfg.spans.precreate_quarters_ahead,
                fillfactor=cfg.spans.hot_partition_fillfactor,
            )
        with session_factory() as session:
            tax_map = seed_default_taxonomy(session)
            periods_synced = sync_government_periods(
                session,
                cfg.analytics.government_periods_path,
            )
            session.commit()
    finally:
        engine.dispose()

    _emit_json(
        {
            "status": "initialized",
            "workdir": str(cfg.workdir),
            "config_file": str(cfg_file),
            "partitions_ensured": partitions,
            "subcategories_seeded": len(tax_map),
            "government_periods_synced": periods_synced,
        }
    )
    return 0


def _handle_create_admin(cfg: AppConfig, args: argparse.Namespace) -> int:
    password = args.password or os.getenv("ARGENPRECIOS_ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass("New administrator password (min 8 chars): ")

    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            user, raw_token = create_or_update_admin_user(
                session,
                username=args.username,
                plain_password=password,
                generate_api_token=not args.no_token,
            )
            _emit_json(
                {
                    "username": user.username,
                    "is_active": user.is_active,
                    "api_bearer_token": raw_token,
                }
            )
    finally:
        engine.dispose()
    return 0


def _handle_ingest(cfg: AppConfig, args: argparse.Namespace) -> int:
    cfg.ensure_owned_directories()
    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            ingestor = IngestSourceBatchUseCase(session, span_settings=cfg.spans)
            src = args.ingest_source

            if src == "sepa":
                sepa = SepaNationalAdapter(temp_extract_dir=cfg.sources.temp_extract_dir)
                if args.archive is not None:
                    archives = [resolve_user_path(args.archive)]
                else:
                    target_dt: date | None = args.date
                    archives = sepa.discover_archives(
                        primary_readonly_dir=cfg.sources.sur_en_precios_raw_dir,
                        fallback_dir=cfg.sources.fallback_sepa_raw_dir,
                        target_date=target_dt,
                    )
                    if not archives and args.download_if_missing:
                        dl_date = target_dt or date.today()
                        downloader = AiohttpSepaDownloader(
                            portal_url=cfg.sources.sepa_portal_url,
                            dest_root_dir=cfg.sources.fallback_sepa_raw_dir,
                        )
                        downloaded = asyncio.run(downloader.download_latest_if_missing(dl_date))
                        archives = [downloaded]
                    elif archives and target_dt is None:
                        archives = [archives[-1]]

                results = [
                    ingestor.ingest_from_adapter(
                        sepa, arch, historical_backfill=False, force=args.force
                    )
                    for arch in archives
                ]
                _emit_json([dataclasses.asdict(r) for r in results])
                return 0

            if src == "sepa-backfill":
                sepa = SepaNationalAdapter(temp_extract_dir=cfg.sources.temp_extract_dir)
                root_dir = (
                    resolve_user_path(args.dir)
                    if args.dir is not None
                    else cfg.sources.sur_en_precios_raw_dir
                )
                archives = sepa.discover_archives(
                    primary_readonly_dir=root_dir,
                    fallback_dir=cfg.sources.fallback_sepa_raw_dir,
                )
                backfill_dicts: list[dict[str, Any]] = []
                for arch in archives:
                    res = ingestor.ingest_from_adapter(
                        sepa,
                        arch,
                        historical_backfill=True,
                        force=args.force,
                    )
                    if args.from_date and res.data_date < args.from_date:
                        continue
                    if args.to_date and res.data_date > args.to_date:
                        continue
                    backfill_dicts.append(dataclasses.asdict(res))
                _emit_json(backfill_dicts)
                return 0

            if src == "web-scrape":
                vtex = WebScrapeVtexAdapter(
                    default_province_code=args.province,
                    default_city=args.city,
                )
                targets = (
                    [resolve_user_path(args.path)]
                    if args.path is not None
                    else vtex.discover_sample_runs(cfg.sources.pipeline_price_scrape_samples_dir)
                )
                web_dicts = [
                    dataclasses.asdict(
                        ingestor.ingest_from_adapter(
                            vtex, t, historical_backfill=False, force=args.force
                        )
                    )
                    for t in targets
                ]
                _emit_json(web_dicts)
                return 0

            if src == "web-jsonl":
                jsonl_adapter = WebScrapeJsonlAdapter()
                res = ingestor.ingest_from_adapter(
                    jsonl_adapter,
                    args.file,
                    historical_backfill=bool(args.backfill),
                    force=args.force,
                )
                _emit_json(res)
                return 0

            if src == "survey":
                survey_adapter = SurveyFileAdapter()
                res = ingestor.ingest_from_adapter(
                    survey_adapter,
                    args.file,
                    historical_backfill=bool(args.backfill),
                    force=args.force,
                )
                _emit_json(res)
                return 0

            if src == "external-db":
                profile = load_external_import_profile(args.profile)
                ext_adapter = ExternalDbAdapter(profile)
                res = ingestor.ingest_from_adapter(
                    ext_adapter,
                    args.file,
                    historical_backfill=True,
                    force=args.force,
                )
                _emit_json(res)
                return 0

            if src == "macro":
                synced = 0
                rows = 0
                if args.sync_periods:
                    synced = sync_government_periods(
                        session,
                        cfg.analytics.government_periods_path,
                    )
                if args.file is not None:
                    rows = ingest_macro_csv(session, args.file)
                _emit_json({"government_periods_synced": synced, "macro_rows_ingested": rows})
                return 0

            if src == "stage-captures":
                staged = stage_captures_file(session, args.file)
                _emit_json({"staged_observations_created": staged})
                return 0

            if src == "promote-staged":
                id_list = [int(tok.strip()) for tok in str(args.ids).split(",") if tok.strip()]
                res = promote_staged_observations(session, id_list, span_settings=cfg.spans)
                _emit_json(res)
                return 0

            raise ValueError(f"Unsupported ingest source: {src}")
    finally:
        engine.dispose()


def _handle_curate(cfg: AppConfig, args: argparse.Namespace) -> int:
    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            curator = CurateCatalogUseCase(session)
            act = args.curate_action

            if act == "create-category":
                cat = curator.create_category(
                    name=args.name,
                    level=args.level,
                    parent_id=args.parent_id,
                    description=args.description,
                    actor=args.actor,
                )
                _emit_json(
                    {
                        "id": cat.id,
                        "name": cat.name,
                        "slug": cat.slug,
                        "level": cat.level,
                        "parent_id": cat.parent_id,
                        "is_manually_curated": cat.is_manually_curated,
                    }
                )
                return 0

            if act == "merge-subcategories":
                moved = curator.merge_subcategories(
                    source_subcategory_id=args.source_id,
                    target_subcategory_id=args.target_id,
                    actor=args.actor,
                )
                _emit_json({"articles_reassigned": moved, "target_id": args.target_id})
                return 0

            if act == "update-article":
                tags = (
                    [t.strip() for t in str(args.tags).split(",") if t.strip()]
                    if args.tags is not None
                    else None
                )
                art = curator.update_article(
                    args.article_id,
                    name=args.name,
                    clean_description=args.clean_description,
                    brand_name=args.brand,
                    package_amount=args.package_amount,
                    package_unit=args.package_unit,
                    category_id=args.category_id,
                    generic_variety_id=args.variety_id,
                    tag_names=tags,
                    actor=args.actor,
                )
                _emit_json(
                    {
                        "id": art.id,
                        "ean": art.ean,
                        "name": art.name,
                        "brand_name": art.brand_name,
                        "category_id": art.category_id,
                        "is_manually_curated": art.is_manually_curated,
                    }
                )
                return 0

            if act == "undo":
                log_entry = curator.undo_edit(args.log_id, actor=args.actor)
                _emit_json(
                    {
                        "undone_log_id": log_entry.id,
                        "entity_type": log_entry.entity_type,
                        "entity_id": log_entry.entity_id,
                        "undone_at": log_entry.undone_at,
                    }
                )
                return 0

            raise ValueError(f"Unsupported curate action: {act}")
    finally:
        engine.dispose()


def _handle_basket(cfg: AppConfig, args: argparse.Namespace) -> int:
    weights = load_regional_weights(cfg.analytics.regional_weights_path)
    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            uc = EvaluateBasketUseCase(session, weights)
            act = args.basket_action

            if act == "import-yaml":
                b_def = load_basket_yaml_file(args.file)
                row = uc.save_basket(b_def)
                _emit_json(
                    {"id": row.id, "uuid": row.uuid, "name": row.name, "items": len(b_def.items)}
                )
                return 0

            if act == "export-yaml":
                b_def = uc.load_basket_definition(args.uuid)
                out = save_basket_yaml_file(b_def, args.output)
                _emit_json({"uuid": b_def.uuid, "exported_to": str(out)})
                return 0

            if act == "evaluate":
                b_def = (
                    load_basket_yaml_file(args.file)
                    if args.file is not None
                    else uc.load_basket_definition(args.uuid)
                )
                ev = uc.evaluate_on_date(
                    basket=b_def,
                    evaluated_date=args.date,
                    geo_level=GeoLevel(args.geo_level),
                    geo_code=args.geo_code,
                    weighting=WeightingScheme(args.weighting),
                    persist=args.uuid is not None,
                )
                _emit_json(ev)
                return 0

            raise ValueError(f"Unsupported basket action: {act}")
    finally:
        engine.dispose()


def _handle_partition_maintenance(cfg: AppConfig, args: argparse.Namespace) -> int:
    engine = create_warehouse_engine(cfg.database_url, cfg.database)
    try:
        act = args.partition_action
        if act == "ensure":
            with engine.begin() as conn:
                parts = ensure_partitions_window(
                    conn,
                    anchor_date=args.date,
                    quarters_back=args.quarters_back,
                    quarters_ahead=args.quarters_ahead,
                    fillfactor=cfg.spans.hot_partition_fillfactor,
                )
            _emit_json({"partitions_ensured": parts})
            return 0

        if act == "seal":
            part_name = seal_historical_partition(
                engine,
                year=args.year,
                quarter=args.quarter,
                run_vacuum_freeze=bool(args.vacuum_freeze),
            )
            _emit_json({"sealed_partition": part_name})
            return 0

        session_factory = create_session_factory(engine)
        with session_factory() as session:
            if act == "compact-cold":
                compacted = compact_cold_partition_to_chain_city(
                    session,
                    year=args.year,
                    quarter=args.quarter,
                    drop_branch_rows_after_compact=bool(args.drop_branch_rows),
                )
                session.commit()
                _emit_json({"chain_city_spans_upserted": compacted})
                return 0

            if act == "status":
                used_bytes = get_database_size_bytes(session)
                used_gb = used_bytes / (1024**3)
                _emit_json(
                    {
                        "used_bytes": used_bytes,
                        "used_gb": round(used_gb, 3),
                        "budget_gb": cfg.spans.storage_budget_gb,
                        "warning_threshold_gb": cfg.spans.compaction_warning_threshold_gb,
                        "compaction_recommended": used_gb
                        >= cfg.spans.compaction_warning_threshold_gb,
                    }
                )
                return 0

            raise ValueError(f"Unsupported partition action: {act}")
    finally:
        engine.dispose()


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Execute a parsed CLI invocation and return an exit status code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    cfg = load_config(config_path=args.config, workdir=args.workdir)
    configure_logging(level=args.log_level, log_dir=cfg.logs_dir)

    cmd = args.command
    if cmd == "init":
        return _handle_init(cfg, args)

    if cmd == "create-admin":
        return _handle_create_admin(cfg, args)

    if cmd == "seed-hermes":
        hermes_path = (
            resolve_user_path(args.hermes_db)
            if args.hermes_db is not None
            else cfg.sources.hermes_clean_db_path
        )
        engine = create_warehouse_engine(cfg.database_url, cfg.database)
        try:
            session_factory = create_session_factory(engine)
            with session_factory() as session:
                summary = seed_from_hermes_sqlite(session, hermes_path)
                _emit_json(summary)
        finally:
            engine.dispose()
        return 0

    if cmd == "ingest":
        return _handle_ingest(cfg, args)

    if cmd == "search":
        with WarehouseClient(cfg) as client:
            page = client.search_articles(
                args.query,
                department_id=args.dept,
                subcategory_id=args.subcat,
                min_active_provinces=args.min_provinces,
                page_size=args.limit,
            )
            _emit_json(page)
        return 0

    if cmd == "curate":
        return _handle_curate(cfg, args)

    if cmd == "basket":
        return _handle_basket(cfg, args)

    if cmd == "rollup":
        weights = load_regional_weights(cfg.analytics.regional_weights_path)
        engine = create_warehouse_engine(cfg.database_url, cfg.database)
        try:
            session_factory = create_session_factory(engine)
            with session_factory() as session:
                uc = ComputeRollupsAndCpiUseCase(
                    session,
                    weights,
                    base_index_value=cfg.analytics.base_index_value,
                )
                daily_cells = uc.compute_daily_rollups(
                    start_date=args.from_date,
                    end_date=args.to_date,
                    channel_scopes=(args.channel_scope,),
                )
                cpi_pts = uc.compute_monthly_rollups_and_cpi(
                    channel_scope=args.channel_scope,
                    frequency=args.frequency,
                )
                _emit_json({"daily_variety_cells": daily_cells, "cpi_index_points": cpi_pts})
        finally:
            engine.dispose()
        return 0

    if cmd == "report":
        out_dir = (
            resolve_user_path(args.output_dir) if args.output_dir is not None else cfg.reports_dir
        )
        scheme = WeightingScheme(args.weighting)
        with WarehouseClient(cfg) as client:
            cpi_pts_list = client.get_cpi_series(
                geo_level=GeoLevel.COUNTRY,
                geo_code="AR",
                frequency=args.frequency,
                weighting=scheme,
            )
            periods = client.compare_government_periods(
                frequency=args.frequency,
                weighting=scheme,
            )
        formats = [f.strip() for f in str(args.formats).split(",") if f.strip()]
        written = export_report_bundle(
            output_dir=out_dir,
            stem=args.stem,
            title=args.title,
            formats=formats,
            cpi_points=cpi_pts_list,
            period_comparisons=periods,
        )
        _emit_json({"files_written": [str(p) for p in written]})
        return 0

    if cmd == "partition-maintenance":
        return _handle_partition_maintenance(cfg, args)

    if cmd == "serve":
        host = args.host or cfg.web.host
        port = args.port or cfg.web.port
        app = create_web_app(cfg)
        uvicorn.run(app, host=host, port=port, log_config=None)
        return 0

    raise ValueError(f"Unknown command: {cmd}")


def main() -> None:
    """Console-script boundary catching unexpected exceptions cleanly after logging tracebacks."""
    try:
        exit_code = run_cli()
    except Exception:
        logger.exception("Unhandled error while executing ArgenPrecios CLI command")
        sys.exit(1)
    if exit_code != 0:
        sys.exit(exit_code)


__all__ = [
    "build_parser",
    "main",
    "run_cli",
]
