"""Official macroeconomic reference series (INDEC IPC, CBA/CBT, RIPTE, FX) & government periods loader."""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_analytics import (
    GovernmentPeriodModel,
    MacroReferenceSeriesModel,
)
from argenprecios.config import load_government_periods, resolve_user_path

logger = logging.getLogger(__name__)


def sync_government_periods(session: Session, periods_yaml_path: Path) -> int:
    """Idempotently upsert presidential periods from YAML into government_periods."""
    cfg = load_government_periods(periods_yaml_path)
    count = 0
    for spec in cfg.periods:
        ins = pg_insert(GovernmentPeriodModel).values(
            slug=spec.slug,
            name=spec.name,
            administration_label=spec.administration_label,
            start_date=spec.start_date,
            end_date=spec.end_date,
            period_kind=spec.period_kind,
            notes=spec.notes,
        )
        upsert = ins.on_conflict_do_update(
            index_elements=["slug"],
            set_={
                "name": ins.excluded.name,
                "administration_label": ins.excluded.administration_label,
                "start_date": ins.excluded.start_date,
                "end_date": ins.excluded.end_date,
                "period_kind": ins.excluded.period_kind,
                "notes": ins.excluded.notes,
            },
        )
        session.execute(upsert)
        count += 1
    session.commit()
    logger.info("Synced %d government periods from %s", count, periods_yaml_path)
    return count


def ingest_macro_csv(session: Session, csv_path: Path) -> int:
    """Ingest official INDEC/RIPTE/FX benchmark CSV rows into macro_reference_series.

    Expected CSV columns:
      `series_code`, `series_name` (optional), `geo_code` (default `AR`),
      `period_date` (`YYYY-MM-DD`), `value`, `unit` (default `index`), `source_label` (default `INDEC`).
    """
    resolved = resolve_user_path(csv_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Macro reference CSV not found: {resolved}")

    rows_payload: list[dict[str, object]] = []
    with resolved.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("series_code") or "").strip().upper()
            raw_date = (row.get("period_date") or row.get("date") or "").strip()
            raw_val = (row.get("value") or "").strip().replace(",", ".")
            if not code or not raw_date or not raw_val:
                continue
            obs_date = date.fromisoformat(raw_date[:10])
            val = float(raw_val)
            rows_payload.append(
                {
                    "series_code": code[:64],
                    "series_name": (row.get("series_name") or code).strip()[:160],
                    "geo_code": (row.get("geo_code") or "AR").strip().upper()[:40],
                    "period_date": obs_date,
                    "value": val,
                    "unit": (row.get("unit") or "index").strip()[:32],
                    "source_label": (row.get("source_label") or "INDEC").strip()[:120],
                }
            )

    if not rows_payload:
        return 0

    for i in range(0, len(rows_payload), 2_000):
        chunk = rows_payload[i : i + 2_000]
        ins = pg_insert(MacroReferenceSeriesModel).values(chunk)
        upsert = ins.on_conflict_do_update(
            constraint="uq_macro_reference_series_obs",
            set_={
                "series_name": ins.excluded.series_name,
                "value": ins.excluded.value,
                "unit": ins.excluded.unit,
                "source_label": ins.excluded.source_label,
            },
        )
        session.execute(upsert)

    session.commit()
    logger.info("Ingested %d macro reference rows from %s", len(rows_payload), resolved)
    return len(rows_payload)


__all__ = [
    "ingest_macro_csv",
    "sync_government_periods",
]
