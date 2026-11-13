"""Offline street/field price survey reader supporting CSV, Excel (.xlsx), and YAML."""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl
import yaml

from argenprecios.config import resolve_user_path
from argenprecios.domain.enums import PosChannel, SourceChannel
from argenprecios.domain.models import RawPriceObservation
from argenprecios.domain.promo_parser import evaluate_promotion, parse_ars_to_cents
from argenprecios.domain.unit_normalizer import slugify_text

logger = logging.getLogger(__name__)

_POS_CHANNEL_ALIASES: dict[str, PosChannel] = {
    "physical_supermarket": PosChannel.PHYSICAL_SUPERMARKET,
    "supermercado": PosChannel.PHYSICAL_SUPERMARKET,
    "hipermercado": PosChannel.PHYSICAL_SUPERMARKET,
    "online_storefront": PosChannel.ONLINE_STOREFRONT,
    "online": PosChannel.ONLINE_STOREFRONT,
    "web": PosChannel.ONLINE_STOREFRONT,
    "neighborhood_shop": PosChannel.NEIGHBORHOOD_SHOP,
    "almacen": PosChannel.NEIGHBORHOOD_SHOP,
    "autoservicio": PosChannel.NEIGHBORHOOD_SHOP,
    "despensa": PosChannel.NEIGHBORHOOD_SHOP,
    "kiosco": PosChannel.NEIGHBORHOOD_SHOP,
    "cercania": PosChannel.NEIGHBORHOOD_SHOP,
    "butchery": PosChannel.BUTCHERY,
    "carniceria": PosChannel.BUTCHERY,
    "greengrocer": PosChannel.GREENGROCER,
    "verduleria": PosChannel.GREENGROCER,
    "fruteria": PosChannel.GREENGROCER,
    "bakery": PosChannel.BAKERY,
    "panaderia": PosChannel.BAKERY,
}


def _pick_field(row: Mapping[str, Any], *aliases: str) -> Any:
    lower_map = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    for alias in aliases:
        if alias.lower() in lower_map and lower_map[alias.lower()] not in (None, ""):
            return lower_map[alias.lower()]
    return None


def _resolve_pos_channel(raw_val: Any) -> PosChannel:
    if not raw_val:
        return PosChannel.NEIGHBORHOOD_SHOP
    key = slugify_text(str(raw_val)).replace("-", "_")
    if key in _POS_CHANNEL_ALIASES:
        return _POS_CHANNEL_ALIASES[key]
    first_word = key.split("_")[0]
    return _POS_CHANNEL_ALIASES.get(first_word, PosChannel.NEIGHBORHOOD_SHOP)


class SurveyFileAdapter:
    """Loads field surveys from neighborhood shops, butcheries, bakeries, and greengrocers."""

    @property
    def source_channel(self) -> SourceChannel:
        return SourceChannel.OFFLINE_SURVEY

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]:
        resolved = resolve_user_path(source_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Survey file not found: {resolved}")

        suffix = resolved.suffix.lower()
        if suffix == ".csv":
            raw_rows = self._iter_csv_rows(resolved)
        elif suffix == ".xlsx":
            raw_rows = self._iter_xlsx_rows(resolved)
        elif suffix in {".yaml", ".yml"}:
            raw_rows = self._iter_yaml_rows(resolved)
        else:
            raise ValueError(f"Unsupported survey file extension '{suffix}' for {resolved}")

        for raw in raw_rows:
            obs = self._row_to_observation(raw)
            if obs is not None:
                yield obs

    def _iter_csv_rows(self, path: Path) -> Iterator[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
            for row in csv.DictReader(handle, delimiter=delimiter):
                yield dict(row)

    def _iter_xlsx_rows(self, path: Path) -> Iterator[dict[str, Any]]:
        wb = openpyxl.load_workbook(filename=path, read_only=True, data_only=True)
        try:
            sheet = wb.active
            if sheet is None:
                return
            rows_iter = sheet.iter_rows(values_only=True)
            header_tuple = next(rows_iter, None)
            if header_tuple is None:
                return
            headers = [str(col).strip() if col is not None else "" for col in header_tuple]
            for values in rows_iter:
                row_dict = {
                    headers[idx]: val
                    for idx, val in enumerate(values)
                    if idx < len(headers) and headers[idx]
                }
                if any(v is not None and str(v).strip() != "" for v in row_dict.values()):
                    yield row_dict
        finally:
            wb.close()

    def _iter_yaml_rows(self, path: Path) -> Iterator[dict[str, Any]]:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            for entry in loaded:
                if isinstance(entry, dict):
                    yield entry
        elif isinstance(loaded, dict):
            defaults = {k: v for k, v in loaded.items() if k != "observations"}
            items = loaded.get("observations") or loaded.get("items") or []
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict):
                        merged = dict(defaults)
                        merged.update(entry)
                        yield merged

    def _row_to_observation(self, row: Mapping[str, Any]) -> RawPriceObservation | None:
        raw_dt = _pick_field(row, "date", "observed_date", "fecha")
        if raw_dt is None:
            return None
        obs_date = (
            raw_dt if isinstance(raw_dt, date) else date.fromisoformat(str(raw_dt).strip()[:10])
        )

        raw_list = _pick_field(row, "list_price", "precio_lista", "price", "precio")
        list_cents = parse_ars_to_cents(raw_list)
        if list_cents is None:
            return None

        prov = str(_pick_field(row, "province_code", "provincia", "province") or "AR-U").strip()
        city = str(_pick_field(row, "city", "localidad", "ciudad") or "General").strip()
        pos_name = str(
            _pick_field(row, "pos_name", "comercio", "store", "tienda") or "Comercio"
        ).strip()
        pos_channel = _resolve_pos_channel(_pick_field(row, "pos_channel", "rubro", "canal"))
        pos_code = str(
            _pick_field(row, "pos_code", "codigo_comercio")
            or f"SURVEY:{prov.upper()}:{slugify_text(city)}:{slugify_text(pos_name)}"
        )[:80]

        raw_ean_or_var = _pick_field(
            row, "ean", "codigo_barras", "ean_or_variety", "variety", "variedad"
        )
        ean_val: str | None = None
        variety_slug: str | None = None
        if raw_ean_or_var is not None:
            token = str(raw_ean_or_var).strip()
            if token.isdigit() and len(token) >= 8:
                ean_val = token
            elif token:
                variety_slug = slugify_text(token)

        explicit_variety = _pick_field(row, "generic_variety_slug", "variedad_generica")
        if explicit_variety:
            variety_slug = slugify_text(str(explicit_variety))

        raw_desc = str(
            _pick_field(row, "raw_description", "descripcion", "description", "producto")
            or variety_slug
            or "Producto relevado"
        ).strip()
        raw_brand_val = _pick_field(row, "brand", "raw_brand", "marca")
        raw_qty = _pick_field(row, "quantity", "package_amount", "cantidad")
        raw_unit = _pick_field(row, "unit", "package_unit", "unidad")

        pkg_amount: float | None = None
        if raw_qty is not None:
            try:
                pkg_amount = float(str(raw_qty).replace(",", "."))
            except ValueError:
                pkg_amount = None

        pkg_unit = str(raw_unit).strip() if raw_unit else None
        raw_pkg = f"{pkg_amount:g} {pkg_unit}".strip() if pkg_amount and pkg_unit else None

        promo = evaluate_promotion(
            list_price_cents=list_cents,
            explicit_promo_cents=parse_ars_to_cents(
                _pick_field(row, "promo_price", "precio_promo")
            ),
            raw_legend=(
                str(_pick_field(row, "promo_legend", "leyenda_promo")).strip()
                if _pick_field(row, "promo_legend", "leyenda_promo")
                else None
            ),
        )

        raw_stock = _pick_field(row, "in_stock", "stock", "disponible")
        in_stock = True
        if raw_stock is not None and str(raw_stock).strip().lower() in {
            "0",
            "false",
            "no",
            "sin stock",
        }:
            in_stock = False

        return RawPriceObservation(
            observed_date=obs_date,
            source_channel=SourceChannel.OFFLINE_SURVEY,
            pos_code=pos_code,
            pos_channel=pos_channel,
            chain_name=pos_name,
            branch_name=pos_name,
            city=city,
            province_code=prov,
            ean=ean_val,
            generic_variety_slug=variety_slug,
            raw_description=raw_desc,
            raw_brand=str(raw_brand_val).strip() if raw_brand_val else None,
            raw_package=raw_pkg,
            package_amount=pkg_amount,
            package_unit=pkg_unit,
            list_price_cents=list_cents,
            promo_price_cents=promo.promo_price_cents,
            promo_legend=promo.raw_legend,
            promo_mechanism=promo.mechanism,
            in_stock=in_stock,
        )


__all__ = [
    "SurveyFileAdapter",
]
