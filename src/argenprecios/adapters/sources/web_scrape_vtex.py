"""Read-only supermarket web-scraper adapters: VTEX HTML JSON-LD (`mpn` EAN recovery) & JSONL."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from argenprecios.config import resolve_user_path
from argenprecios.domain.enums import PosChannel, PromoMechanism, SourceChannel
from argenprecios.domain.models import RawPriceObservation
from argenprecios.domain.promo_parser import evaluate_promotion, parse_ars_to_cents
from argenprecios.domain.unit_normalizer import slugify_text

logger = logging.getLogger(__name__)

_TIMESTAMP_14_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})\d{6}$")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_JSON_LD_SCRIPT_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class VtexBrand(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str | None = None


class VtexSeller(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str | None = None


class VtexOffer(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    price: float | int | Decimal | str | None = None
    price_currency: str | None = Field(default="ARS", alias="priceCurrency")
    availability: str | None = None
    sku: str | None = None
    seller: VtexSeller | None = None


class VtexAggregateOffer(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    low_price: float | int | Decimal | str | None = Field(default=None, alias="lowPrice")
    high_price: float | int | Decimal | str | None = Field(default=None, alias="highPrice")
    price: float | int | Decimal | str | None = None
    price_currency: str | None = Field(default="ARS", alias="priceCurrency")
    availability: str | None = None
    offers: list[VtexOffer] = Field(default_factory=list)


class VtexProduct(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    schema_type: str = Field(default="Product", alias="@type")
    name: str | None = None
    brand: VtexBrand | str | None = None
    description: str | None = None
    mpn: str | None = None
    gtin13: str | None = None
    gtin: str | None = None
    sku: str | None = None
    offers: VtexAggregateOffer | VtexOffer | None = None


def infer_scrape_date(path: Path) -> date:
    """Infer scrape date from `<YYYYMMDDHHMMSS>` folder or `YYYY-MM-DD` path segment."""
    for part in reversed(path.parts):
        m14 = _TIMESTAMP_14_RE.match(part)
        if m14:
            return date(int(m14.group(1)), int(m14.group(2)), int(m14.group(3)))
        m_iso = _ISO_DATE_RE.search(part)
        if m_iso:
            return date.fromisoformat(m_iso.group(1))
    raise ValueError(f"Could not infer scrape date from path: {path}")


def _is_valid_ean_or_plu(candidate: str | None) -> bool:
    if not candidate:
        return False
    stripped = candidate.strip()
    return stripped.isdigit() and 8 <= len(stripped) <= 14


def _resolve_availability(offers: VtexAggregateOffer | VtexOffer | None) -> bool:
    if offers is None:
        return True
    avail_strings: list[str] = []
    if isinstance(offers, VtexOffer) and offers.availability:
        avail_strings.append(offers.availability)
    elif isinstance(offers, VtexAggregateOffer):
        if offers.availability:
            avail_strings.append(offers.availability)
        for sub in offers.offers:
            if sub.availability:
                avail_strings.append(sub.availability)
    if not avail_strings:
        return True
    return any("instock" in s.lower() for s in avail_strings)


class WebScrapeVtexAdapter:
    """Reads `pipeline_price_scrape` `samples/<timestamp>/<chain>/*.html` strictly read-only."""

    def __init__(
        self,
        default_province_code: str = "AR-C",
        default_city: str = "Tienda Online",
    ) -> None:
        self._default_province_code = default_province_code
        self._default_city = default_city

    @property
    def source_channel(self) -> SourceChannel:
        return SourceChannel.WEB_SCRAPE

    def discover_sample_runs(self, samples_root: Path) -> list[Path]:
        """Return sorted `<samples>/<timestamp>/<chain>` directories containing HTML captures."""
        resolved = resolve_user_path(samples_root)
        if not resolved.is_dir():
            return []
        runs: list[Path] = []
        for ts_dir in sorted(p for p in resolved.iterdir() if p.is_dir()):
            for chain_dir in sorted(p for p in ts_dir.iterdir() if p.is_dir()):
                if any(chain_dir.glob("*.html")):
                    runs.append(chain_dir)
        return runs

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]:
        """Parse all `*.html` files in a chain run directory or single HTML file read-only."""
        resolved = resolve_user_path(source_path)
        if not resolved.exists():
            raise FileNotFoundError(f"Web scrape path not found: {resolved}")

        html_files = [resolved] if resolved.is_file() else sorted(resolved.rglob("*.html"))
        if not html_files:
            return

        observed_date = infer_scrape_date(resolved)
        chain_folder = resolved.parent.name if resolved.is_file() else resolved.name
        chain_name = chain_folder.replace("_", " ").replace("-", " ").title()
        chain_slug = slugify_text(chain_name)

        # Optional sidecar geo metadata if present in the scrape folder
        prov_code = self._default_province_code
        city_name = self._default_city
        meta_file = (resolved.parent if resolved.is_file() else resolved) / "scrape_meta.json"
        if meta_file.is_file():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            prov_code = str(meta.get("province_code") or prov_code)
            city_name = str(meta.get("city") or city_name)
            chain_name = str(meta.get("chain_name") or chain_name)
            chain_slug = slugify_text(chain_name)

        pos_code = f"WEB:{chain_slug}:{prov_code.upper()}"
        seen_keys: set[str] = set()

        for html_file in html_files:
            raw_html = html_file.read_text(encoding="utf-8", errors="replace")
            for product in self._extract_products_from_html(raw_html):
                obs = self._product_to_observation(
                    product=product,
                    observed_date=observed_date,
                    pos_code=pos_code,
                    chain_name=chain_name,
                    city_name=city_name,
                    prov_code=prov_code,
                )
                if obs is None:
                    continue
                dedup_id = obs.ean or obs.external_sku or obs.raw_description
                if dedup_id in seen_keys:
                    continue
                seen_keys.add(dedup_id)
                yield obs

    def _extract_products_from_html(self, raw_html: str) -> list[VtexProduct]:
        products: list[VtexProduct] = []
        for match in _JSON_LD_SCRIPT_RE.finditer(raw_html):
            block = match.group(1).strip()
            if not block:
                continue
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            products.extend(self._collect_products_from_jsonld(data))
        return products

    def _collect_products_from_jsonld(self, node: Any) -> list[VtexProduct]:
        found: list[VtexProduct] = []
        if isinstance(node, list):
            for elem in node:
                found.extend(self._collect_products_from_jsonld(elem))
            return found

        if not isinstance(node, dict):
            return found

        node_type = str(node.get("@type") or "")
        if node_type == "Product":
            found.append(VtexProduct.model_validate(node))
        elif "itemListElement" in node and isinstance(node["itemListElement"], list):
            for li in node["itemListElement"]:
                if isinstance(li, dict) and isinstance(li.get("item"), dict):
                    found.extend(self._collect_products_from_jsonld(li["item"]))
        elif "@graph" in node and isinstance(node["@graph"], list):
            found.extend(self._collect_products_from_jsonld(node["@graph"]))

        return found

    def _product_to_observation(
        self,
        product: VtexProduct,
        observed_date: date,
        pos_code: str,
        chain_name: str,
        city_name: str,
        prov_code: str,
    ) -> RawPriceObservation | None:
        if not product.name or product.offers is None:
            return None

        high_cents: int | None = None
        low_cents: int | None = None
        offer_sku: str | None = None

        if isinstance(product.offers, VtexAggregateOffer):
            high_cents = parse_ars_to_cents(product.offers.high_price)
            low_cents = parse_ars_to_cents(product.offers.low_price)
            if high_cents is None and product.offers.price is not None:
                high_cents = parse_ars_to_cents(product.offers.price)
            if product.offers.offers:
                first_sub = product.offers.offers[0]
                offer_sku = first_sub.sku
                if high_cents is None:
                    high_cents = parse_ars_to_cents(first_sub.price)
        elif isinstance(product.offers, VtexOffer):
            high_cents = parse_ars_to_cents(product.offers.price)
            offer_sku = product.offers.sku

        list_cents = high_cents or low_cents
        if list_cents is None:
            return None

        promo_cents: int | None = None
        promo_legend: str | None = None
        promo_mech = PromoMechanism.NONE
        if low_cents is not None and 0 < low_cents < list_cents:
            eval_p = evaluate_promotion(
                list_price_cents=list_cents,
                explicit_promo_cents=low_cents,
                raw_legend="Oferta Online",
            )
            promo_cents = eval_p.promo_price_cents
            promo_legend = eval_p.raw_legend
            promo_mech = eval_p.mechanism

        barcode: str | None = None
        for candidate in (product.gtin13, product.gtin, product.mpn):
            if _is_valid_ean_or_plu(candidate):
                barcode = candidate.strip()  # type: ignore[union-attr]
                break

        ext_sku = (product.sku or offer_sku or product.mpn or "").strip() or None
        raw_brand: str | None = None
        if isinstance(product.brand, VtexBrand):
            raw_brand = product.brand.name
        elif isinstance(product.brand, str):
            raw_brand = product.brand

        in_stock = _resolve_availability(product.offers)

        return RawPriceObservation(
            observed_date=observed_date,
            source_channel=SourceChannel.WEB_SCRAPE,
            pos_code=pos_code,
            pos_channel=PosChannel.ONLINE_STOREFRONT,
            chain_name=chain_name,
            branch_name=f"{chain_name} Online ({city_name})",
            city=city_name,
            province_code=prov_code,
            ean=barcode,
            external_sku=ext_sku,
            raw_description=product.name.strip(),
            raw_brand=raw_brand.strip() if raw_brand else None,
            list_price_cents=list_cents,
            promo_price_cents=promo_cents,
            promo_legend=promo_legend,
            promo_mechanism=promo_mech,
            in_stock=in_stock,
        )


class JsonlScrapedRow(BaseModel):
    """Standardized drop-zone JSONL schema for future non-VTEX supermarket scrapers."""

    model_config = ConfigDict(extra="ignore")

    observed_date: date
    chain_name: str
    province_code: str = "AR-C"
    city: str = "Tienda Online"
    branch_name: str | None = None
    ean: str | None = None
    external_sku: str | None = None
    raw_description: str
    raw_brand: str | None = None
    raw_package: str | None = None
    list_price: float | int | Decimal | str
    promo_price: float | int | Decimal | str | None = None
    promo_legend: str | None = None
    in_stock: bool = True


class WebScrapeJsonlAdapter:
    """Streams canonical `.jsonl` web-scraper captures."""

    @property
    def source_channel(self) -> SourceChannel:
        return SourceChannel.WEB_SCRAPE

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]:
        resolved = resolve_user_path(source_path)
        with resolved.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                item = JsonlScrapedRow.model_validate_json(stripped)
                list_cents = parse_ars_to_cents(item.list_price)
                if list_cents is None:
                    continue
                promo = evaluate_promotion(
                    list_price_cents=list_cents,
                    explicit_promo_cents=parse_ars_to_cents(item.promo_price),
                    raw_legend=item.promo_legend,
                )
                chain_slug = slugify_text(item.chain_name)
                yield RawPriceObservation(
                    observed_date=item.observed_date,
                    source_channel=SourceChannel.WEB_SCRAPE,
                    pos_code=f"WEB:{chain_slug}:{item.province_code.upper()}",
                    pos_channel=PosChannel.ONLINE_STOREFRONT,
                    chain_name=item.chain_name,
                    branch_name=item.branch_name or f"{item.chain_name} Online",
                    city=item.city,
                    province_code=item.province_code,
                    ean=item.ean,
                    external_sku=item.external_sku,
                    raw_description=item.raw_description,
                    raw_brand=item.raw_brand,
                    raw_package=item.raw_package,
                    list_price_cents=list_cents,
                    promo_price_cents=promo.promo_price_cents,
                    promo_legend=promo.raw_legend,
                    promo_mechanism=promo.mechanism,
                    in_stock=item.in_stock,
                )


__all__ = [
    "JsonlScrapedRow",
    "VtexAggregateOffer",
    "VtexBrand",
    "VtexOffer",
    "VtexProduct",
    "VtexSeller",
    "WebScrapeJsonlAdapter",
    "WebScrapeVtexAdapter",
    "infer_scrape_date",
]
