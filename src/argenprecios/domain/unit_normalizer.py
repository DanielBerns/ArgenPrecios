"""Unit normalization and shrinkflation-aware conversion math for retail articles."""

from __future__ import annotations

import re
import unicodedata

from argenprecios.domain.enums import CanonicalUnit

_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def slugify_text(raw: str) -> str:
    """Convert arbitrary Spanish retail text into a clean ASCII kebab-case slug."""
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()
    slug = _SLUG_CLEAN_RE.sub("-", ascii_text).strip("-")
    return slug or "sin-clasificar"


def resolve_unit_conversion(
    package_amount: float | None,
    package_unit: str | None,
) -> tuple[CanonicalUnit, float | None]:
    """Resolve (canonical_unit, unit_conversion_factor) so unit_price = price / factor.

    Examples:
      - 500 g  -> (CanonicalUnit.KG, 0.5)     -> $1,000 pack = $2,000 / kg
      - 400 g  -> (CanonicalUnit.KG, 0.4)     -> $1,000 pack = $2,500 / kg (+25% shrinkflation!)
      - 900 ml -> (CanonicalUnit.LITER, 0.9)  -> $1,800 bottle = $2,000 / L
      - 1.5 L  -> (CanonicalUnit.LITER, 1.5)
      - 6 u    -> (CanonicalUnit.UNIT, 6.0)
    """
    if package_amount is None or package_amount <= 0 or not package_unit:
        return (CanonicalUnit.UNIT, None)

    unit = package_unit.strip().lower()
    if unit in {"kg", "kgs", "k", "kilo", "kilos"}:
        return (CanonicalUnit.KG, float(package_amount))
    if unit in {"g", "gr", "grs", "gramo", "gramos"}:
        return (CanonicalUnit.KG, float(package_amount) / 1000.0)
    if unit in {"mg"}:
        return (CanonicalUnit.KG, float(package_amount) / 1_000_000.0)
    if unit in {"l", "lt", "lts", "litro", "litros"}:
        return (CanonicalUnit.LITER, float(package_amount))
    if unit in {"ml", "cc", "cm3"}:
        return (CanonicalUnit.LITER, float(package_amount) / 1000.0)
    if unit in {"m", "mt", "mts", "metro", "metros"}:
        return (CanonicalUnit.METER, float(package_amount))
    if unit in {"cm"}:
        return (CanonicalUnit.METER, float(package_amount) / 100.0)
    if unit in {"u", "un", "und", "uni", "unid", "unidad", "unidades", "saq", "sob"}:
        return (CanonicalUnit.UNIT, float(package_amount))

    return (CanonicalUnit.UNIT, None)


def compute_unit_price_cents(
    price_cents: int,
    unit_conversion_factor: float | None,
) -> int:
    """Compute normalized price in centavos per canonical unit (falling back to pack price)."""
    if unit_conversion_factor is None or unit_conversion_factor <= 0:
        return price_cents
    return max(1, round(price_cents / unit_conversion_factor))


def build_variety_slug(
    subcategory_slug: str | None,
    clean_description: str,
    canonical_unit: CanonicalUnit,
) -> str:
    """Generate a stable homogeneous GenericVariety slug independent of brand or pack size."""
    tokens = slugify_text(clean_description).split("-")
    stem_tokens = [t for t in tokens if len(t) >= 2][:4]
    stem = "-".join(stem_tokens) if stem_tokens else "general"
    prefix = slugify_text(subcategory_slug) if subcategory_slug else "almacen"
    unit_suffix = canonical_unit.value.lower()
    raw_slug = f"{prefix}-{stem}-{unit_suffix}"
    return raw_slug[:155].strip("-")


__all__ = [
    "build_variety_slug",
    "compute_unit_price_cents",
    "resolve_unit_conversion",
    "slugify_text",
]
