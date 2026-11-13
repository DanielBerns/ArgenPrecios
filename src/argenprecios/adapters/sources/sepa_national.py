"""National 24-province SEPA archive reader (strictly read-only on sur_en_precios) & aiohttp downloader."""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import shutil
import uuid
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import aiohttp

from argenprecios.adapters.db.repositories_catalog import normalize_province_code
from argenprecios.config import resolve_user_path
from argenprecios.domain.enums import PosChannel, SourceChannel
from argenprecios.domain.models import RawPriceObservation
from argenprecios.domain.promo_parser import evaluate_sepa_dual_promos, parse_ars_to_cents

logger = logging.getLogger(__name__)

_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_SEPA_ZIP_URL_RE = re.compile(r'https?://[^\s"\'<>]+sepa[^\s"\'<>]*\.zip', re.IGNORECASE)


def compute_file_sha256(file_path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    """Compute hex SHA-256 digest of a file without loading it completely into RAM."""
    resolved = resolve_user_path(file_path)
    hasher = hashlib.sha256()
    with resolved.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            hasher.update(chunk)
    return hasher.hexdigest()


def infer_sepa_archive_date(archive_path: Path) -> date:
    """Infer YYYY-MM-DD price date from SEPA filename, parent folder, or zip entries."""
    for candidate in (archive_path.name, archive_path.parent.name):
        match = _DATE_IN_NAME_RE.search(candidate)
        if match:
            return date.fromisoformat(match.group(1))

    if archive_path.is_file() and archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                match = _DATE_IN_NAME_RE.search(member)
                if match:
                    return date.fromisoformat(match.group(1))

    raise ValueError(f"Could not infer YYYY-MM-DD SEPA date from {archive_path}")


class SepaNationalAdapter:
    """Streams SEPA observations across ALL 24 provinces while treating source archives read-only."""

    def __init__(self, temp_extract_dir: Path) -> None:
        self._temp_extract_dir = resolve_user_path(temp_extract_dir)

    @property
    def source_channel(self) -> SourceChannel:
        return SourceChannel.SEPA

    def discover_archives(
        self,
        primary_readonly_dir: Path,
        fallback_dir: Path | None = None,
        target_date: date | None = None,
    ) -> list[Path]:
        """Discover daily `sepa_*.zip` archives, preferring sur_en_precios raw_dir read-only."""
        by_date: dict[date, Path] = {}
        search_roots = [resolve_user_path(primary_readonly_dir)]
        if fallback_dir is not None:
            search_roots.append(resolve_user_path(fallback_dir))

        for root in search_roots:
            if not root.is_dir():
                continue
            for zip_path in sorted(root.rglob("*.zip")):
                # Only top-level daily SEPA bundles or standalone chain archives when requested
                if "comercio" in zip_path.name.lower() and zip_path.parent != root:
                    continue
                try:
                    dt = infer_sepa_archive_date(zip_path)
                except ValueError:
                    continue
                if target_date is not None and dt != target_date:
                    continue
                by_date.setdefault(dt, zip_path)

        return [by_date[d] for d in sorted(by_date)]

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]:
        """Extract SEPA archive into ArgenPrecios's private temp_extract_dir and stream 24 provinces."""
        resolved_source = resolve_user_path(source_path)
        if not resolved_source.exists():
            raise FileNotFoundError(f"SEPA archive/directory not found: {resolved_source}")

        observed_date = infer_sepa_archive_date(resolved_source)
        run_temp = self._temp_extract_dir / f"sepa_run_{uuid.uuid4().hex}"
        run_temp.mkdir(parents=True, exist_ok=True)

        try:
            chain_sources = self._prepare_chain_sources(resolved_source, run_temp)
            for idx, chain_src in enumerate(chain_sources):
                chain_work = run_temp / f"chain_{idx}"
                chain_work.mkdir(parents=True, exist_ok=True)
                try:
                    yield from self._parse_single_chain(chain_src, chain_work, observed_date)
                finally:
                    shutil.rmtree(chain_work, ignore_errors=True)
        finally:
            shutil.rmtree(run_temp, ignore_errors=True)

    def _prepare_chain_sources(self, source_path: Path, run_temp: Path) -> list[Path]:
        if source_path.is_dir():
            inner_zips = [
                p for p in sorted(source_path.rglob("*.zip")) if "comercio" in p.name.lower()
            ]
            if inner_zips:
                return inner_zips
            return [source_path]

        outer_extract = run_temp / "outer"
        outer_extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source_path, "r") as zf:
            names = zf.namelist()
            has_direct_csv = any(Path(n).name.lower() == "productos.csv" for n in names)
            if has_direct_csv:
                return [source_path]
            for member in names:
                if member.lower().endswith(".zip"):
                    target_zip = outer_extract / Path(member).name
                    with zf.open(member, "r") as src_stream, target_zip.open("wb") as dst_stream:
                        shutil.copyfileobj(src_stream, dst_stream)

        return sorted(outer_extract.glob("*.zip"))

    def _extract_csv_member(self, chain_src: Path, csv_name: str, dest_dir: Path) -> Path | None:
        if chain_src.is_dir():
            candidate = chain_src / csv_name
            return candidate if candidate.is_file() else None

        with zipfile.ZipFile(chain_src, "r") as zf:
            for member in zf.namelist():
                if Path(member).name.lower() == csv_name.lower():
                    out_file = dest_dir / csv_name
                    with zf.open(member, "r") as src_f, out_file.open("wb") as dst_f:
                        shutil.copyfileobj(src_f, dst_f)
                    return out_file
        return None

    def _parse_single_chain(
        self,
        chain_src: Path,
        chain_work: Path,
        observed_date: date,
    ) -> Iterator[RawPriceObservation]:
        sucursales_csv = self._extract_csv_member(chain_src, "sucursales.csv", chain_work)
        productos_csv = self._extract_csv_member(chain_src, "productos.csv", chain_work)
        if sucursales_csv is None or productos_csv is None:
            return

        comercio_csv = self._extract_csv_member(chain_src, "comercio.csv", chain_work)
        banner_names: dict[tuple[str, str], str] = {}
        if comercio_csv is not None:
            with comercio_csv.open("r", encoding="utf-8-sig", errors="replace") as cf:
                for row in csv.DictReader(cf, delimiter="|"):
                    ic = (row.get("id_comercio") or "").strip()
                    ib = (row.get("id_bandera") or "").strip()
                    bname = (
                        row.get("comercio_bandera_nombre") or row.get("comercio_razon_social") or ""
                    ).strip()
                    if ic and ib and bname:
                        banner_names[(ic, ib)] = bname

        stores: dict[tuple[str, str, str], dict[str, object]] = {}
        with sucursales_csv.open("r", encoding="utf-8-sig", errors="replace") as sf:
            for row in csv.DictReader(sf, delimiter="|"):
                ic = (row.get("id_comercio") or "").strip()
                ib = (row.get("id_bandera") or "").strip()
                isuc = (row.get("id_sucursal") or "").strip()
                raw_prov = (row.get("sucursales_provincia") or "").strip()
                prov_iso = normalize_province_code(raw_prov)
                if not ic or not ib or not isuc or prov_iso is None:
                    continue

                calle = (row.get("sucursales_calle") or "").strip()
                numero = (row.get("sucursales_numero") or "").strip()
                addr = f"{calle} {numero}".strip() or None
                lat = _parse_optional_float(row.get("sucursales_latitud"))
                lon = _parse_optional_float(row.get("sucursales_longitud"))
                suc_tipo = (row.get("sucursales_tipo") or "").strip().lower()
                pos_chan = (
                    PosChannel.ONLINE_STOREFRONT
                    if "web" in suc_tipo or "online" in suc_tipo or "virtual" in suc_tipo
                    else PosChannel.PHYSICAL_SUPERMARKET
                )

                stores[(ic, ib, isuc)] = {
                    "pos_code": f"SEPA:{ic}-{ib}-{isuc}",
                    "pos_channel": pos_chan,
                    "chain_name": banner_names.get((ic, ib)) or f"Comercio {ic}",
                    "branch_name": (row.get("sucursales_nombre") or f"Sucursal {isuc}").strip(),
                    "city": (row.get("sucursales_localidad") or "General").strip(),
                    "province_code": prov_iso,
                    "address": addr,
                    "latitude": lat,
                    "longitude": lon,
                }

        if not stores:
            return

        seen_in_chain: set[tuple[str, str]] = set()
        with productos_csv.open("r", encoding="utf-8-sig", errors="replace") as pf:
            for row in csv.DictReader(pf, delimiter="|"):
                ic = (row.get("id_comercio") or "").strip()
                ib = (row.get("id_bandera") or "").strip()
                isuc = (row.get("id_sucursal") or "").strip()
                st_meta = stores.get((ic, ib, isuc))
                if st_meta is None:
                    continue

                list_cents = parse_ars_to_cents(row.get("productos_precio_lista"))
                if list_cents is None:
                    continue

                raw_id = (row.get("id_producto") or "").strip()
                if not raw_id:
                    continue

                pos_code_str = str(st_meta["pos_code"])
                dedup_key = (raw_id, pos_code_str)
                if dedup_key in seen_in_chain:
                    continue
                seen_in_chain.add(dedup_key)

                promo = evaluate_sepa_dual_promos(
                    list_price_cents=list_cents,
                    promo1_raw_price=row.get("productos_precio_unitario_promo1"),
                    promo1_legend=row.get("productos_leyenda_promo1"),
                    promo2_raw_price=row.get("productos_precio_unitario_promo2"),
                    promo2_legend=row.get("productos_leyenda_promo2"),
                )

                ean_Flag = (row.get("productos_ean") or "1").strip()
                is_barcode = ean_Flag in {"1", "true", "True", ""} and raw_id.isdigit()
                raw_qty = (row.get("productos_cantidad_presentacion") or "").strip()
                raw_unit = (row.get("productos_unidad_medida_presentacion") or "").strip()
                raw_pkg = f"{raw_qty} {raw_unit}".strip() or None

                yield RawPriceObservation(
                    observed_date=observed_date,
                    source_channel=SourceChannel.SEPA,
                    pos_code=pos_code_str,
                    pos_channel=st_meta["pos_channel"],  # type: ignore[arg-type]
                    chain_name=str(st_meta["chain_name"]),
                    branch_name=str(st_meta["branch_name"]),
                    city=str(st_meta["city"]),
                    province_code=str(st_meta["province_code"]),
                    address=st_meta["address"] if isinstance(st_meta["address"], str) else None,
                    latitude=st_meta["latitude"]
                    if isinstance(st_meta["latitude"], float)
                    else None,
                    longitude=(
                        st_meta["longitude"] if isinstance(st_meta["longitude"], float) else None
                    ),
                    ean=raw_id if is_barcode else None,
                    external_sku=None if is_barcode else raw_id,
                    raw_description=(row.get("productos_descripcion") or "").strip(),
                    raw_brand=(row.get("productos_marca") or "").strip() or None,
                    raw_package=raw_pkg,
                    package_amount=_parse_optional_float(raw_qty),
                    package_unit=raw_unit or None,
                    list_price_cents=list_cents,
                    promo_price_cents=promo.promo_price_cents,
                    promo_legend=promo.raw_legend,
                    promo_mechanism=promo.mechanism,
                    in_stock=True,
                )


def _parse_optional_float(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        return None


class AiohttpSepaDownloader:
    """Independent fallback SEPA downloader writing ONLY to ArgenPrecios's owned directory."""

    def __init__(self, portal_url: str, dest_root_dir: Path) -> None:
        self._portal_url = portal_url
        self._dest_root_dir = resolve_user_path(dest_root_dir)

    async def download_latest_if_missing(self, expected_date: date) -> Path:
        """Download daily SEPA zip into ~/Info/ArgenPrecios/data/raw/sepa/YYYY-MM-DD/."""
        target_dir = self._dest_root_dir / expected_date.isoformat()
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_file = target_dir / f"sepa_{expected_date.isoformat()}.zip"
        if dest_file.is_file() and dest_file.stat().st_size > 0:
            return dest_file

        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._portal_url) as resp:
                resp.raise_for_status()
                html_text = await resp.text()

            matches = _SEPA_ZIP_URL_RE.findall(html_text)
            if not matches:
                raise RuntimeError(f"No SEPA .zip download URL found on {self._portal_url}")

            download_url = matches[0]
            for url_candidate in matches:
                if expected_date.isoformat() in url_candidate:
                    download_url = url_candidate
                    break

            logger.info("Downloading fallback SEPA archive from %s to %s", download_url, dest_file)
            part_file = target_dir / f"sepa_{expected_date.isoformat()}.zip.part"
            async with session.get(download_url) as zip_resp:
                zip_resp.raise_for_status()
                with part_file.open("wb") as out_f:
                    async for chunk in zip_resp.content.iter_chunked(256 * 1024):
                        out_f.write(chunk)
            part_file.replace(dest_file)
            return dest_file


__all__ = [
    "AiohttpSepaDownloader",
    "SepaNationalAdapter",
    "compute_file_sha256",
    "infer_sepa_archive_date",
]
