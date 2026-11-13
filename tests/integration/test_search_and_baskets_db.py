"""Integration tests for Spanish accent/trigram search, basket DB valuation, CPI rollups, & reports."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from argenprecios.adapters.db.repositories_search import PostgresCatalogSearchRepository
from argenprecios.adapters.exporters.reports import export_report_bundle
from argenprecios.adapters.sources.offline_macro import (
    ingest_macro_csv,
    sync_government_periods,
)
from argenprecios.config import AppConfig, load_regional_weights
from argenprecios.domain.enums import GeoLevel, PosChannel, SourceChannel, WeightingScheme
from argenprecios.domain.models import (
    BasketDefinition,
    BasketItemSpec,
    RawPriceObservation,
    SearchFilter,
)
from argenprecios.use_cases.compute_rollups_and_cpi import (
    CompareGovernmentPeriodsUseCase,
    ComputeRollupsAndCpiUseCase,
)
from argenprecios.use_cases.evaluate_baskets import EvaluateBasketUseCase
from argenprecios.use_cases.ingest_observations import IngestSourceBatchUseCase


def test_accent_insensitive_search_multi_geo_basket_cpi_and_reports(
    pg_session: Session,
    isolated_app_config: AppConfig,
    tmp_path: Path,
) -> None:
    ingestor = IngestSourceBatchUseCase(pg_session, isolated_app_config.spans)
    observations = [
        RawPriceObservation(
            observed_date=date(2026, 2, 10),
            source_channel=SourceChannel.SEPA,
            pos_code="SEPA:99-1-1",
            pos_channel=PosChannel.PHYSICAL_SUPERMARKET,
            chain_name="Cadena Sur",
            branch_name="Sucursal Rawson",
            city="Rawson",
            province_code="AR-U",
            ean="7798888000012",
            raw_description="YERBA MATE TRADICIONAL TARAGÜÍ 1 KG",
            raw_brand="Taragüí",
            raw_package="1 kg",
            package_amount=1.0,
            package_unit="kg",
            list_price_cents=420_000,
            promo_price_cents=390_000,
        ),
        RawPriceObservation(
            observed_date=date(2026, 2, 10),
            source_channel=SourceChannel.SEPA,
            pos_code="SEPA:99-1-2",
            pos_channel=PosChannel.PHYSICAL_SUPERMARKET,
            chain_name="Cadena Norte",
            branch_name="Sucursal Córdoba",
            city="Córdoba",
            province_code="AR-X",
            ean="7798888000012",
            raw_description="YERBA MATE TRADICIONAL TARAGÜÍ 1 KG",
            raw_brand="Taragüí",
            raw_package="1 kg",
            package_amount=1.0,
            package_unit="kg",
            list_price_cents=380_000,
            promo_price_cents=None,
        ),
        RawPriceObservation(
            observed_date=date(2026, 3, 10),
            source_channel=SourceChannel.SEPA,
            pos_code="SEPA:99-1-1",
            pos_channel=PosChannel.PHYSICAL_SUPERMARKET,
            chain_name="Cadena Sur",
            branch_name="Sucursal Rawson",
            city="Rawson",
            province_code="AR-U",
            ean="7798888000012",
            raw_description="YERBA MATE TRADICIONAL TARAGÜÍ 1 KG",
            raw_brand="Taragüí",
            raw_package="1 kg",
            package_amount=1.0,
            package_unit="kg",
            list_price_cents=460_000,
            promo_price_cents=None,
        ),
        RawPriceObservation(
            observed_date=date(2026, 3, 10),
            source_channel=SourceChannel.SEPA,
            pos_code="SEPA:99-1-2",
            pos_channel=PosChannel.PHYSICAL_SUPERMARKET,
            chain_name="Cadena Norte",
            branch_name="Sucursal Córdoba",
            city="Córdoba",
            province_code="AR-X",
            ean="7798888000012",
            raw_description="YERBA MATE TRADICIONAL TARAGÜÍ 1 KG",
            raw_brand="Taragüí",
            raw_package="1 kg",
            package_amount=1.0,
            package_unit="kg",
            list_price_cents=418_000,
            promo_price_cents=None,
        ),
    ]
    ingestor.ingest_stream(
        source_channel=SourceChannel.SEPA,
        source_uri="synthetic:search_and_cpi",
        sha256_digest="a" * 64,
        observations=observations,
        historical_backfill=True,
        force=True,
    )

    # 1. Accent-insensitive search (`taragui` matches `Taragüí`) + coverage counts
    search_repo = PostgresCatalogSearchRepository(pg_session)
    res = search_repo.search_articles(
        SearchFilter(
            query="yerba taragui", min_active_provinces=2, reference_date=date(2026, 3, 10)
        )
    )
    assert res.total_count >= 1
    hit = next(h for h in res.items if h.ean == "7798888000012")
    assert hit.active_provinces_count == 2
    assert hit.active_chains_count == 2

    # 2. Basket persistence and Country-level evaluation
    weights = load_regional_weights(isolated_app_config.analytics.regional_weights_path)
    basket_uc = EvaluateBasketUseCase(pg_session, weights)
    b_def = BasketDefinition(
        name="Canasta Infusiones",
        uuid="canasta-infusiones-test",
        is_public=True,
        items=(BasketItemSpec(ean="7798888000012", quantity=2, name="Yerba 1kg"),),
    )
    basket_uc.save_basket(b_def)
    country_eval = basket_uc.evaluate_on_date(
        basket=b_def,
        evaluated_date=date(2026, 3, 10),
        geo_level=GeoLevel.COUNTRY,
        geo_code="AR",
        weighting=WeightingScheme.EXPENDITURE,
    )
    assert country_eval.matched_items_count == 1
    assert country_eval.min_total_cents == 836_000  # 2 * $4,180 in Córdoba

    # 3. Stage 1 & Stage 2 CPI rollups + Cross-government periods + Multi-format Report export
    sync_government_periods(pg_session, isolated_app_config.analytics.government_periods_path)
    macro_csv = tmp_path / "macro_series.csv"
    macro_csv.write_text(
        "series_code,series_name,geo_code,period_date,value,unit,source_label\n"
        "INDEC_IPC_GENERAL,IPC Nacional,AR,2026-02-01,100.0,index,INDEC\n"
        "INDEC_IPC_GENERAL,IPC Nacional,AR,2026-03-01,108.5,index,INDEC\n"
        "RIPTE,Remuneración Imponible,AR,2026-02-01,1000000.0,ARS,Secretaría de Trabajo\n"
        "RIPTE,Remuneración Imponible,AR,2026-03-01,1110000.0,ARS,Secretaría de Trabajo\n",
        encoding="utf-8",
    )
    assert ingest_macro_csv(pg_session, macro_csv) == 4

    cpi_uc = ComputeRollupsAndCpiUseCase(pg_session, weights)
    assert cpi_uc.compute_daily_rollups(date(2026, 2, 10), date(2026, 2, 10)) > 0
    assert cpi_uc.compute_daily_rollups(date(2026, 3, 10), date(2026, 3, 10)) > 0
    pts_persisted = cpi_uc.compute_monthly_rollups_and_cpi(frequency="monthly")
    assert pts_persisted > 0

    comparisons = CompareGovernmentPeriodsUseCase(pg_session).compare_periods()
    assert len(comparisons) >= 2

    out_files = export_report_bundle(
        output_dir=tmp_path / "reports_out",
        stem="briefing_test",
        title="Informe Nacional Test",
        formats=("html", "md", "csv", "parquet"),
        cpi_points=[],
        period_comparisons=comparisons,
        basket_evaluations=[country_eval],
    )
    assert any(p.suffix == ".html" and p.stat().st_size > 0 for p in out_files)
    assert any(p.suffix == ".md" and p.stat().st_size > 0 for p in out_files)
    assert any(p.suffix == ".parquet" and p.stat().st_size > 0 for p in out_files)
