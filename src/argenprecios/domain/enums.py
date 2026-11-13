"""Pure domain enums for provenance, channels, units, geography, promotions, and curation."""

from __future__ import annotations

from enum import StrEnum


class SourceChannel(StrEnum):
    """Canonical data source channels feeding the national price warehouse."""

    SEPA = "sepa"
    WEB_SCRAPE = "web_scrape"
    HERMES = "hermes"
    OFFLINE_SURVEY = "offline_survey"
    OFFLINE_DB_IMPORT = "offline_db_import"
    OFFLINE_CROWDSOURCED = "offline_crowdsourced"


class PosChannel(StrEnum):
    """Retail point-of-sale commercial format / channel."""

    PHYSICAL_SUPERMARKET = "physical_supermarket"
    ONLINE_STOREFRONT = "online_storefront"
    NEIGHBORHOOD_SHOP = "neighborhood_shop"
    BUTCHERY = "butchery"
    GREENGROCER = "greengrocer"
    BAKERY = "bakery"


class CanonicalUnit(StrEnum):
    """Normalized physical base units for homogeneous generic varieties and shrinkflation tracking."""

    KG = "kg"
    LITER = "L"
    UNIT = "u"
    METER = "m"
    HUNDRED_GRAMS = "100g"


class GeoLevel(StrEnum):
    """Geographic aggregation levels from municipality up to national Argentina."""

    CITY = "city"
    PROVINCE = "province"
    REGION = "region"
    COUNTRY = "country"


class CurationStatus(StrEnum):
    """Catalog editorial review state."""

    UNREVIEWED = "unreviewed"
    AUTO = "auto"
    MANUALLY_CURATED = "manually_curated"


class PromoMechanism(StrEnum):
    """Standardized promotional pricing mechanism categories."""

    NONE = "none"
    TWO_FOR_ONE = "2x1"
    THREE_FOR_TWO = "3x2"
    SECOND_UNIT_PCT = "2do_pct"
    MULTIPACK_DISCOUNT = "multipack"
    LOYALTY_CARD = "loyalty_card"
    BANK_DISCOUNT = "bank_discount"
    DIRECT_DISCOUNT = "direct_discount"
    OTHER = "other"


class WeightingScheme(StrEnum):
    """National/regional aggregation weighting mode."""

    EXPENDITURE = "expenditure"
    POPULATION = "population"
    UNWEIGHTED = "unweighted"


__all__ = [
    "CanonicalUnit",
    "CurationStatus",
    "GeoLevel",
    "PosChannel",
    "PromoMechanism",
    "SourceChannel",
    "WeightingScheme",
]
