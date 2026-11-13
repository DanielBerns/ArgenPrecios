"""Staging adapter and verification promoter for semi-structured receipts, PDFs, & crowdsourced captures."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_analytics import StagedObservationModel
from argenprecios.config import SpanSettings, resolve_user_path
from argenprecios.domain.enums import PosChannel, SourceChannel
from argenprecios.domain.models import IngestionRunResult, RawPriceObservation
from argenprecios.domain.promo_parser import parse_ars_to_cents
from argenprecios.domain.unit_normalizer import slugify_text

logger = logging.getLogger(__name__)


def stage_captures_file(session: Session, file_path: Path) -> int:
    """Load semi-structured JSON or CSV receipt/PDF price extractions into staged_observations."""
    resolved = resolve_user_path(file_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Staged capture file not found: {resolved}")

    raw_items: list[dict[str, Any]] = []
    if resolved.suffix.lower() == ".json":
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
        if isinstance(parsed, list):
            raw_items = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            raw_items = [x for x in parsed["items"] if isinstance(x, dict)]
    elif resolved.suffix.lower() == ".csv":
        with resolved.open("r", encoding="utf-8-sig", errors="replace") as handle:
            raw_items = [dict(r) for r in csv.DictReader(handle)]
    else:
        raise ValueError(f"Unsupported staged capture file format: {resolved.suffix}")

    staged_count = 0
    for item in raw_items:
        list_cents = parse_ars_to_cents(item.get("list_price") or item.get("price"))
        raw_dt = item.get("observed_date") or item.get("date")
        if list_cents is None or not raw_dt:
            continue
        obs_date = date.fromisoformat(str(raw_dt).strip()[:10])
        promo_cents = parse_ars_to_cents(item.get("promo_price"))
        conf = float(item.get("confidence") or 0.75)

        session.add(
            StagedObservationModel(
                observed_date=obs_date,
                source_channel=SourceChannel.OFFLINE_CROWDSOURCED.value,
                province_code=str(item.get("province_code") or "AR-U").strip().upper()[:10],
                city=str(item.get("city") or "General").strip()[:120],
                pos_name=str(item.get("pos_name") or "Ticket Capturado").strip()[:160],
                pos_channel=str(
                    item.get("pos_channel") or PosChannel.NEIGHBORHOOD_SHOP.value
                ).strip()[:40],
                ean_or_sku=(
                    str(item.get("ean") or item.get("sku")).strip()[:64]
                    if (item.get("ean") or item.get("sku"))
                    else None
                ),
                raw_description=str(item.get("raw_description") or "Ítem de ticket").strip(),
                list_price_cents=list_cents,
                promo_price_cents=promo_cents,
                confidence=conf,
                status="pending",
                payload_json=item,
            )
        )
        staged_count += 1

    session.commit()
    logger.info("Staged %d receipt/PDF captures from %s", staged_count, resolved)
    return staged_count


def promote_staged_observations(
    session: Session,
    staged_ids: Sequence[int],
    span_settings: SpanSettings | None = None,
) -> IngestionRunResult:
    """Promote human-verified staged_observations rows into canonical price_spans."""
    rows = session.scalars(
        select(StagedObservationModel).where(
            StagedObservationModel.id.in_(list(staged_ids)),
            StagedObservationModel.status == "pending",
        )
    ).all()

    observations: list[RawPriceObservation] = []
    for row in rows:
        code_token = row.ean_or_sku
        is_ean = bool(code_token and code_token.isdigit() and len(code_token) >= 8)
        pos_chan = (
            PosChannel(row.pos_channel)
            if row.pos_channel in PosChannel._value2member_map_
            else PosChannel.NEIGHBORHOOD_SHOP
        )
        observations.append(
            RawPriceObservation(
                observed_date=row.observed_date,
                source_channel=SourceChannel.OFFLINE_CROWDSOURCED,
                pos_code=f"STAGE:{row.province_code}:{slugify_text(row.city)}:{slugify_text(row.pos_name)}"[
                    :80
                ],
                pos_channel=pos_chan,
                chain_name=row.pos_name,
                branch_name=row.pos_name,
                city=row.city,
                province_code=row.province_code,
                ean=code_token if is_ean else None,
                external_sku=None if is_ean else code_token,
                raw_description=row.raw_description,
                list_price_cents=row.list_price_cents,
                promo_price_cents=row.promo_price_cents,
                in_stock=True,
            )
        )
        row.status = "promoted"

    digest = hashlib.sha256(
        ",".join(str(r.id) for r in sorted(rows, key=lambda x: x.id)).encode("ascii")
    ).hexdigest()
    from argenprecios.use_cases.ingest_observations import IngestSourceBatchUseCase

    ingestor = IngestSourceBatchUseCase(session, span_settings=span_settings)
    return ingestor.ingest_stream(
        source_channel=SourceChannel.OFFLINE_CROWDSOURCED,
        source_uri=f"staged_observations:{len(rows)}",
        sha256_digest=digest,
        observations=observations,
        historical_backfill=True,
        force=True,
    )


__all__ = [
    "promote_staged_observations",
    "stage_captures_file",
]
