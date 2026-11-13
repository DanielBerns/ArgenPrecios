"""Catalog, geographic, POS, and promo repositories enforcing manual curation locks."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from hermes_reborn.cleaner.article_cleaner import ArticleCleaner
from hermes_reborn.taxonomy.classifier import TaxonomyClassifier
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_catalog import (
    ArticleModel,
    BrandModel,
    CategoryModel,
    CurationRuleModel,
    GenericVarietyModel,
    SourceSkuMappingModel,
    TagModel,
    UnreviewedArticleModel,
    article_tags,
)
from argenprecios.adapters.db.models_facts import DataSourceModel, PromoRuleModel
from argenprecios.adapters.db.models_geo import (
    ChainModel,
    CityModel,
    PointOfSaleModel,
    ProvinceModel,
)
from argenprecios.domain.enums import CanonicalUnit
from argenprecios.domain.models import RawPriceObservation
from argenprecios.domain.promo_parser import evaluate_promotion
from argenprecios.domain.unit_normalizer import (
    build_variety_slug,
    resolve_unit_conversion,
    slugify_text,
)

logger = logging.getLogger(__name__)

# Maps common Argentine province names / non-standard SEPA strings to ISO 3166-2:AR codes
PROVINCE_ALIASES: dict[str, str] = {
    "AR-A": "AR-A",
    "SALTA": "AR-A",
    "AR-B": "AR-B",
    "BUENOS AIRES": "AR-B",
    "PROVINCIA DE BUENOS AIRES": "AR-B",
    "PBA": "AR-B",
    "AR-C": "AR-C",
    "CABA": "AR-C",
    "CAPITAL FEDERAL": "AR-C",
    "CIUDAD AUTONOMA DE BUENOS AIRES": "AR-C",
    "CF": "AR-C",
    "AR-D": "AR-D",
    "SAN LUIS": "AR-D",
    "AR-E": "AR-E",
    "ENTRE RIOS": "AR-E",
    "AR-F": "AR-F",
    "LA RIOJA": "AR-F",
    "AR-G": "AR-G",
    "SANTIAGO DEL ESTERO": "AR-G",
    "AR-H": "AR-H",
    "CHACO": "AR-H",
    "AR-J": "AR-J",
    "SAN JUAN": "AR-J",
    "AR-K": "AR-K",
    "CATAMARCA": "AR-K",
    "AR-L": "AR-L",
    "LA PAMPA": "AR-L",
    "AR-M": "AR-M",
    "MENDOZA": "AR-M",
    "AR-N": "AR-N",
    "MISIONES": "AR-N",
    "AR-P": "AR-P",
    "FORMOSA": "AR-P",
    "AR-Q": "AR-Q",
    "NEUQUEN": "AR-Q",
    "AR-R": "AR-R",
    "RIO NEGRO": "AR-R",
    "AR-S": "AR-S",
    "SANTA FE": "AR-S",
    "AR-T": "AR-T",
    "TUCUMAN": "AR-T",
    "AR-U": "AR-U",
    "CHUBUT": "AR-U",
    "AR-V": "AR-V",
    "TIERRA DEL FUEGO": "AR-V",
    "AR-W": "AR-W",
    "CORRIENTES": "AR-W",
    "AR-X": "AR-X",
    "CORDOBA": "AR-X",
    "AR-Y": "AR-Y",
    "JUJUY": "AR-Y",
    "AR-Z": "AR-Z",
    "SANTA CRUZ": "AR-Z",
}


def normalize_province_code(raw: str) -> str | None:
    """Normalize any ISO code or Spanish province name to canonical ISO 3166-2 `AR-*`."""
    cleaned = slugify_text(raw).replace("-", " ").upper()
    with_hyphen = raw.strip().upper()
    if with_hyphen in PROVINCE_ALIASES:
        return PROVINCE_ALIASES[with_hyphen]
    return PROVINCE_ALIASES.get(cleaned)


@dataclass
class CompiledCurationRule:
    """In-memory compiled regex editorial rule."""

    rule_id: int
    regex: re.Pattern[str]
    match_brand_lower: str | None
    target_category_id: int | None
    target_brand_id: int | None
    target_generic_variety_id: int | None
    override_package_amount: float | None
    override_package_unit: str | None


class CatalogAndGeoRepository:
    """Resolves and upserts geography, POS, promo rules, and catalog entities with caching."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cleaner = ArticleCleaner()
        self._classifier = TaxonomyClassifier()

        self._source_cache: dict[str, int] = {}
        self._province_cache: dict[str, int] = {}
        self._city_cache: dict[tuple[int, str], int] = {}
        self._chain_cache: dict[str, int] = {}
        self._pos_cache: dict[str, int] = {}
        self._promo_cache: dict[str, int] = {}
        self._brand_cache: dict[str, int] = {}
        self._tag_cache: dict[str, int] = {}
        self._variety_cache: dict[str, int] = {}
        self._ean_article_cache: dict[str, int] = {}
        self._sku_mapping_cache: dict[tuple[int, int, str], int] = {}
        self._category_lookup: dict[tuple[str, str], int] = {}
        self._cat_id_to_slug: dict[int, str] = {}
        self._compiled_rules: list[CompiledCurationRule] = []
        self.new_articles_created: int = 0

        self._warm_caches()

    def _warm_caches(self) -> None:
        from argenprecios.adapters.sources.hermes_seeder import seed_default_taxonomy

        self._category_lookup = seed_default_taxonomy(self._session)
        for cat in self._session.scalars(select(CategoryModel)):
            self._cat_id_to_slug[cat.id] = cat.slug

        for src in self._session.scalars(select(DataSourceModel)):
            self._source_cache[src.code] = src.id

        for prov in self._session.scalars(select(ProvinceModel)):
            self._province_cache[prov.code.upper()] = prov.id

        for city in self._session.scalars(select(CityModel)):
            self._city_cache[(city.province_id, city.slug)] = city.id

        for chain in self._session.scalars(select(ChainModel)):
            self._chain_cache[chain.slug] = chain.id

        for pos in self._session.scalars(select(PointOfSaleModel)):
            self._pos_cache[pos.code] = pos.id

        for pr in self._session.scalars(select(PromoRuleModel)):
            self._promo_cache[pr.code_hash] = pr.id

        for br in self._session.scalars(select(BrandModel)):
            self._brand_cache[br.slug] = br.id

        for tg in self._session.scalars(select(TagModel)):
            self._tag_cache[tg.slug] = tg.id

        for gv in self._session.scalars(select(GenericVarietyModel)):
            self._variety_cache[gv.slug] = gv.id

        for art_id, art_ean in self._session.execute(
            select(ArticleModel.id, ArticleModel.ean).where(ArticleModel.ean.is_not(None))
        ).all():
            if art_ean is not None:
                self._ean_article_cache[art_ean] = art_id

        for map_row in self._session.scalars(select(SourceSkuMappingModel)):
            self._sku_mapping_cache[(map_row.source_id, map_row.chain_id, map_row.external_sku)] = (
                map_row.article_id
            )

        rules = self._session.scalars(
            select(CurationRuleModel)
            .where(CurationRuleModel.is_active.is_(True))
            .order_by(CurationRuleModel.priority.asc(), CurationRuleModel.id.asc())
        ).all()
        for r in rules:
            self._compiled_rules.append(
                CompiledCurationRule(
                    rule_id=r.id,
                    regex=re.compile(r.pattern_regex, re.IGNORECASE),
                    match_brand_lower=r.match_brand.lower() if r.match_brand else None,
                    target_category_id=r.target_category_id,
                    target_brand_id=r.target_brand_id,
                    target_generic_variety_id=r.target_generic_variety_id,
                    override_package_amount=r.override_package_amount,
                    override_package_unit=r.override_package_unit,
                )
            )

    def get_or_create_source_id(self, source_code: str) -> int:
        if source_code in self._source_cache:
            return self._source_cache[source_code]
        stmt = (
            pg_insert(DataSourceModel)
            .values(code=source_code, name=source_code.replace("_", " ").title())
            .on_conflict_do_nothing(index_elements=["code"])
        )
        self._session.execute(stmt)
        self._session.flush()
        src_id = self._session.scalar(
            select(DataSourceModel.id).where(DataSourceModel.code == source_code)
        )
        if src_id is None:
            raise RuntimeError(f"Failed to resolve data_source '{source_code}'")
        self._source_cache[source_code] = src_id
        return src_id

    def resolve_province_id(self, raw_province: str) -> int | None:
        iso_code = normalize_province_code(raw_province)
        if iso_code is None:
            return None
        return self._province_cache.get(iso_code)

    def get_or_create_city_id(self, province_id: int, city_name: str) -> int:
        clean_name = " ".join(city_name.strip().split()).title() or "General"
        slug = slugify_text(clean_name)[:120]
        key = (province_id, slug)
        if key in self._city_cache:
            return self._city_cache[key]

        stmt = (
            pg_insert(CityModel)
            .values(province_id=province_id, name=clean_name[:120], slug=slug)
            .on_conflict_do_nothing(constraint="uq_cities_province_slug")
        )
        self._session.execute(stmt)
        self._session.flush()
        city_id = self._session.scalar(
            select(CityModel.id).where(
                CityModel.province_id == province_id,
                CityModel.slug == slug,
            )
        )
        if city_id is None:
            raise RuntimeError(f"Failed to resolve city ({province_id}, {slug})")
        self._city_cache[key] = city_id
        return city_id

    def get_or_create_chain_id(self, chain_name: str, default_pos_channel: str) -> int:
        clean_name = " ".join(chain_name.strip().split()) or "Comercio Independiente"
        slug = slugify_text(clean_name)[:120]
        if slug in self._chain_cache:
            return self._chain_cache[slug]

        stmt = (
            pg_insert(ChainModel)
            .values(
                name=clean_name[:120],
                slug=slug,
                default_pos_channel=default_pos_channel,
            )
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        self._session.execute(stmt)
        self._session.flush()
        chain_id = self._session.scalar(select(ChainModel.id).where(ChainModel.slug == slug))
        if chain_id is None:
            raise RuntimeError(f"Failed to resolve chain '{slug}'")
        self._chain_cache[slug] = chain_id
        return chain_id

    def get_or_create_pos_id(self, obs: RawPriceObservation, province_id: int) -> tuple[int, int]:
        """Resolve `(pos_id, chain_id)` for an observation."""
        chain_id = self.get_or_create_chain_id(obs.chain_name, obs.pos_channel.value)
        if obs.pos_code in self._pos_cache:
            return (self._pos_cache[obs.pos_code], chain_id)

        city_id = self.get_or_create_city_id(province_id, obs.city)
        stmt = (
            pg_insert(PointOfSaleModel)
            .values(
                code=obs.pos_code[:80],
                chain_id=chain_id,
                city_id=city_id,
                province_id=province_id,
                pos_channel=obs.pos_channel.value,
                branch_name=(obs.branch_name.strip() or obs.chain_name)[:160],
                address=obs.address[:220] if obs.address else None,
                latitude=obs.latitude,
                longitude=obs.longitude,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["code"])
        )
        self._session.execute(stmt)
        self._session.flush()
        pos_id = self._session.scalar(
            select(PointOfSaleModel.id).where(PointOfSaleModel.code == obs.pos_code[:80])
        )
        if pos_id is None:
            raise RuntimeError(f"Failed to resolve point_of_sale '{obs.pos_code}'")
        self._pos_cache[obs.pos_code] = pos_id
        return (pos_id, chain_id)

    def resolve_promo_rule(
        self,
        list_price_cents: int,
        promo_price_cents: int | None,
        promo_legend: str | None,
    ) -> tuple[int | None, int | None]:
        """Return `(effective_promo_price_cents, promo_rule_id)`."""
        parsed = evaluate_promotion(
            list_price_cents=list_price_cents,
            explicit_promo_cents=promo_price_cents,
            raw_legend=promo_legend,
        )
        if parsed.code_hash is None or parsed.raw_legend is None:
            return (parsed.promo_price_cents, None)

        if parsed.code_hash in self._promo_cache:
            return (parsed.promo_price_cents, self._promo_cache[parsed.code_hash])

        stmt = (
            pg_insert(PromoRuleModel)
            .values(
                code_hash=parsed.code_hash,
                mechanism=parsed.mechanism.value,
                raw_legend=parsed.raw_legend[:255],
                discount_factor=parsed.discount_factor,
            )
            .on_conflict_do_nothing(index_elements=["code_hash"])
        )
        self._session.execute(stmt)
        self._session.flush()
        rule_id = self._session.scalar(
            select(PromoRuleModel.id).where(PromoRuleModel.code_hash == parsed.code_hash)
        )
        if rule_id is not None:
            self._promo_cache[parsed.code_hash] = rule_id
        return (parsed.promo_price_cents, rule_id)

    def _get_or_create_brand(self, brand_name: str | None) -> int | None:
        if not brand_name or not brand_name.strip():
            return None
        clean_brand = " ".join(brand_name.strip().split())[:120]
        slug = slugify_text(clean_brand)[:120]
        if slug in self._brand_cache:
            return self._brand_cache[slug]

        stmt = (
            pg_insert(BrandModel)
            .values(name=clean_brand, slug=slug, is_manually_curated=False)
            .on_conflict_do_nothing()
        )
        self._session.execute(stmt)
        self._session.flush()
        brand_id = self._session.scalar(select(BrandModel.id).where(BrandModel.slug == slug))
        if brand_id is not None:
            self._brand_cache[slug] = brand_id
        return brand_id

    def _get_or_create_variety(
        self,
        slug: str,
        display_name: str,
        category_id: int | None,
        canonical_unit: CanonicalUnit,
    ) -> int:
        if slug in self._variety_cache:
            return self._variety_cache[slug]
        stmt = (
            pg_insert(GenericVarietyModel)
            .values(
                slug=slug[:160],
                name=display_name[:180],
                category_id=category_id,
                canonical_unit=canonical_unit.value,
                is_manually_curated=False,
            )
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        self._session.execute(stmt)
        self._session.flush()
        v_id = self._session.scalar(
            select(GenericVarietyModel.id).where(GenericVarietyModel.slug == slug[:160])
        )
        if v_id is None:
            raise RuntimeError(f"Failed to resolve generic_variety '{slug}'")
        self._variety_cache[slug] = v_id
        return v_id

    def _attach_tags(self, article_id: int, tag_names: Sequence[str]) -> None:
        links: list[dict[str, int]] = []
        for raw_t in tag_names:
            clean_t = raw_t.strip().lower()[:100]
            if len(clean_t) < 3:
                continue
            t_slug = slugify_text(clean_t)[:100]
            if t_slug not in self._tag_cache:
                t_stmt = (
                    pg_insert(TagModel).values(name=clean_t, slug=t_slug).on_conflict_do_nothing()
                )
                self._session.execute(t_stmt)
                self._session.flush()
                tid = self._session.scalar(select(TagModel.id).where(TagModel.slug == t_slug))
                if tid is not None:
                    self._tag_cache[t_slug] = tid
            tid_final = self._tag_cache.get(t_slug)
            if tid_final is not None:
                links.append({"article_id": article_id, "tag_id": tid_final})

        if links:
            self._session.execute(pg_insert(article_tags).values(links).on_conflict_do_nothing())

    def resolve_or_create_article_id(
        self,
        obs: RawPriceObservation,
        source_id: int,
        chain_id: int,
    ) -> int:
        """Resolve an observation to `article_id`, respecting curation rules and locks."""
        clean_ean = obs.ean.strip() if obs.ean and obs.ean.strip() else None
        clean_sku = (
            obs.external_sku.strip() if obs.external_sku and obs.external_sku.strip() else None
        )

        # 1. Fast path: known EAN
        if clean_ean and clean_ean in self._ean_article_cache:
            art_id = self._ean_article_cache[clean_ean]
            if clean_sku:
                self._ensure_sku_mapping(source_id, chain_id, clean_sku, art_id)
            return art_id

        # 2. Known (source_id, chain_id, external_sku) mapping
        if clean_sku:
            map_key = (source_id, chain_id, clean_sku)
            if map_key in self._sku_mapping_cache:
                return self._sku_mapping_cache[map_key]

        # 3. Fresh/weighable offline item keyed by generic_variety_slug without barcode
        synthetic_ean: str | None = clean_ean
        if synthetic_ean is None:
            if clean_sku:
                synthetic_ean = f"SKU:{source_id}:{chain_id}:{clean_sku}"[:32]
            elif obs.generic_variety_slug:
                synthetic_ean = f"VAR:{slugify_text(obs.generic_variety_slug)}"[:32]
            else:
                synthetic_ean = f"RAW:{slugify_text(obs.raw_description)[:26]}"[:32]

            if synthetic_ean in self._ean_article_cache:
                art_id = self._ean_article_cache[synthetic_ean]
                if clean_sku:
                    self._ensure_sku_mapping(source_id, chain_id, clean_sku, art_id)
                return art_id

        # 4. Novel item: clean via hermes_reborn + apply active CurationRules + classify
        raw_pkg = obs.raw_package
        if not raw_pkg and obs.package_amount and obs.package_unit:
            raw_pkg = f"{obs.package_amount:g} {obs.package_unit}"

        cleaned = self._cleaner.process(
            ean=synthetic_ean or "",
            raw_description=obs.raw_description or obs.generic_variety_slug or "Producto",
            raw_brand=obs.raw_brand,
            raw_package=raw_pkg,
        )
        classification = self._classifier.classify(cleaned)

        pkg_amt = obs.package_amount if obs.package_amount is not None else cleaned.package_amount
        pkg_unit = obs.package_unit if obs.package_unit is not None else cleaned.package_unit
        cat_id = self._category_lookup.get((classification.department, classification.subcategory))
        brand_id = self._get_or_create_brand(cleaned.brand_name)
        variety_id: int | None = None
        matched_curation_rule = False

        for rule in self._compiled_rules:
            if rule.match_brand_lower and (
                not cleaned.brand_name or rule.match_brand_lower not in cleaned.brand_name.lower()
            ):
                continue
            if rule.regex.search(cleaned.raw_description) or rule.regex.search(
                cleaned.clean_description
            ):
                matched_curation_rule = True
                if rule.target_category_id is not None:
                    cat_id = rule.target_category_id
                if rule.target_brand_id is not None:
                    brand_id = rule.target_brand_id
                if rule.target_generic_variety_id is not None:
                    variety_id = rule.target_generic_variety_id
                if rule.override_package_amount is not None:
                    pkg_amt = rule.override_package_amount
                if rule.override_package_unit is not None:
                    pkg_unit = rule.override_package_unit
                break

        c_unit, conv_factor = resolve_unit_conversion(pkg_amt, pkg_unit)

        if variety_id is None:
            if obs.generic_variety_slug:
                v_slug = slugify_text(obs.generic_variety_slug)[:160]
                v_name = f"{obs.generic_variety_slug.replace('-', ' ').title()} ({c_unit.value})"
            else:
                cat_slug = self._cat_id_to_slug.get(cat_id or -1, "almacen")
                v_slug = build_variety_slug(cat_slug, cleaned.clean_description, c_unit)
                v_name = f"{cleaned.clean_description.title()} ({c_unit.value})"
            variety_id = self._get_or_create_variety(v_slug, v_name, cat_id, c_unit)

        is_fallback_cat = (
            not matched_curation_rule
            and classification.department == "Almacén"
            and classification.subcategory == "Almacén General"
        )
        is_reviewed = matched_curation_rule or (clean_ean is not None and not is_fallback_cat)

        ins_stmt = (
            pg_insert(ArticleModel)
            .values(
                ean=synthetic_ean,
                name=cleaned.name[:255],
                clean_description=cleaned.clean_description[:255],
                raw_description=cleaned.raw_description,
                brand_id=brand_id,
                brand_name=cleaned.brand_name[:120] if cleaned.brand_name else None,
                package=cleaned.package[:60] if cleaned.package else None,
                package_amount=pkg_amt,
                package_unit=pkg_unit[:20] if pkg_unit else None,
                unit_conversion_factor=conv_factor,
                category_id=cat_id,
                generic_variety_id=variety_id,
                is_reviewed=is_reviewed,
                is_manually_curated=False,
            )
            .on_conflict_do_nothing(index_elements=["ean"])
        )
        res = self._session.execute(ins_stmt)
        self._session.flush()
        if int(getattr(res, "rowcount", 0) or 0) > 0:
            self.new_articles_created += 1

        article_id = self._session.scalar(
            select(ArticleModel.id).where(ArticleModel.ean == synthetic_ean)
        )
        if article_id is None:
            raise RuntimeError(f"Failed to persist article for key '{synthetic_ean}'")

        self._ean_article_cache[synthetic_ean] = article_id
        self._attach_tags(article_id, classification.tags)

        if clean_sku:
            self._ensure_sku_mapping(source_id, chain_id, clean_sku, article_id)

        if not is_reviewed:
            self._session.add(
                UnreviewedArticleModel(
                    ean=clean_ean,
                    external_sku=clean_sku,
                    source_channel=obs.source_channel.value,
                    chain_slug=slugify_text(obs.chain_name),
                    raw_description=cleaned.raw_description or obs.raw_description,
                    suggested_brand=cleaned.brand_name,
                    suggested_package=cleaned.package,
                    suggested_category_id=cat_id,
                    confidence=0.6 if clean_ean else 0.4,
                    reason="Novel unclassified SKU or missing barcode",
                    status="pending",
                    resolved_article_id=article_id,
                )
            )

        return article_id

    def _ensure_sku_mapping(
        self,
        source_id: int,
        chain_id: int,
        external_sku: str,
        article_id: int,
    ) -> None:
        key = (source_id, chain_id, external_sku)
        if key in self._sku_mapping_cache:
            return
        stmt = (
            pg_insert(SourceSkuMappingModel)
            .values(
                source_id=source_id,
                chain_id=chain_id,
                external_sku=external_sku[:80],
                article_id=article_id,
                confidence=1.0,
                is_manually_curated=False,
            )
            .on_conflict_do_nothing(constraint="uq_source_sku_mappings_key")
        )
        self._session.execute(stmt)
        self._sku_mapping_cache[key] = article_id


__all__ = [
    "PROVINCE_ALIASES",
    "CatalogAndGeoRepository",
    "CompiledCurationRule",
    "normalize_province_code",
]
