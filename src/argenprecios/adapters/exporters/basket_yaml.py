"""Round-trip YAML reader/writer 100% compatible with sur_en_precios basket files."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from argenprecios.config import resolve_user_path
from argenprecios.domain.enums import CanonicalUnit
from argenprecios.domain.models import BasketDefinition, BasketItemSpec


def parse_basket_yaml_str(yaml_content: str) -> BasketDefinition:
    """Parse a sur_en_precios or extended ArgenPrecios basket YAML string."""
    raw = yaml.safe_load(yaml_content)
    if not isinstance(raw, dict) or not isinstance(raw.get("basket"), dict):
        raise ValueError("Invalid basket YAML: expected top-level 'basket:' mapping")

    b_dict = raw["basket"]
    raw_items = b_dict.get("items") or []
    items: list[BasketItemSpec] = []

    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        raw_ean = entry.get("ean")
        ean_str = str(raw_ean).strip() if raw_ean not in (None, "") else None
        subs_raw = entry.get("substitute_eans") or []
        subs = tuple(str(s).strip() for s in subs_raw if str(s).strip())
        raw_t_unit = entry.get("target_unit")
        t_unit = (
            CanonicalUnit(str(raw_t_unit))
            if raw_t_unit and str(raw_t_unit) in CanonicalUnit._value2member_map_
            else None
        )

        items.append(
            BasketItemSpec(
                ean=ean_str,
                quantity=int(entry.get("quantity", 1)),
                selected=bool(entry.get("selected", True)),
                numerator=int(entry.get("numerator", 1)),
                denominator=int(entry.get("denominator", 1)),
                name=str(entry.get("name") or ""),
                brand=str(entry["brand"]) if entry.get("brand") is not None else None,
                category=str(entry["category"]) if entry.get("category") is not None else None,
                substitute_eans=subs,
                fallback_variety_slug=(
                    str(entry["fallback_variety_slug"]).strip()
                    if entry.get("fallback_variety_slug")
                    else None
                ),
                target_amount=(
                    float(entry["target_amount"])
                    if entry.get("target_amount") is not None
                    else None
                ),
                target_unit=t_unit,
            )
        )

    return BasketDefinition(
        name=str(b_dict.get("name") or "Canasta"),
        uuid=str(b_dict.get("uuid") or uuid.uuid4()),
        is_public=bool(b_dict.get("is_public", True)),
        created_at=str(b_dict.get("created_at") or datetime.now(UTC).isoformat()),
        items=tuple(items),
    )


def load_basket_yaml_file(file_path: Path) -> BasketDefinition:
    """Read and parse a basket YAML file from disk."""
    resolved = resolve_user_path(file_path)
    return parse_basket_yaml_str(resolved.read_text(encoding="utf-8"))


def serialize_basket_yaml_str(
    basket: BasketDefinition,
    strict_sur_en_precios_only: bool = False,
) -> str:
    """Serialize BasketDefinition to YAML preserving 100% sur_en_precios compatibility."""
    serialized_items: list[dict[str, Any]] = []
    for it in basket.items:
        item_dict: dict[str, Any] = {
            "ean": it.ean or "",
            "quantity": it.quantity,
            "selected": it.selected,
            "numerator": it.numerator,
            "denominator": it.denominator,
            "name": it.name,
        }
        if it.brand is not None:
            item_dict["brand"] = it.brand
        if it.category is not None:
            item_dict["category"] = it.category

        if not strict_sur_en_precios_only:
            if it.substitute_eans:
                item_dict["substitute_eans"] = list(it.substitute_eans)
            if it.fallback_variety_slug:
                item_dict["fallback_variety_slug"] = it.fallback_variety_slug
            if it.target_amount is not None:
                item_dict["target_amount"] = it.target_amount
            if it.target_unit is not None:
                item_dict["target_unit"] = it.target_unit.value

        serialized_items.append(item_dict)

    doc: dict[str, Any] = {
        "version": "1.0",
        "basket": {
            "name": basket.name,
            "uuid": basket.uuid,
            "is_public": basket.is_public,
            "created_at": basket.created_at or datetime.now(UTC).isoformat(),
            "items": serialized_items,
        },
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def save_basket_yaml_file(
    basket: BasketDefinition,
    target_path: Path,
    strict_sur_en_precios_only: bool = False,
) -> Path:
    """Write BasketDefinition YAML to disk."""
    resolved = resolve_user_path(target_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        serialize_basket_yaml_str(basket, strict_sur_en_precios_only=strict_sur_en_precios_only),
        encoding="utf-8",
    )
    return resolved


__all__ = [
    "load_basket_yaml_file",
    "parse_basket_yaml_str",
    "save_basket_yaml_file",
    "serialize_basket_yaml_str",
]
