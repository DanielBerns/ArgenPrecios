"""PostgreSQL hybrid Spanish full-text + trigram + geographic coverage search repository."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import Subquery, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from argenprecios.adapters.db.models_catalog import (
    ArticleModel,
    BrandModel,
    CategoryModel,
    GenericVarietyModel,
    SourceSkuMappingModel,
    TagModel,
    article_tags,
)
from argenprecios.adapters.db.models_facts import PriceSpanModel
from argenprecios.adapters.db.models_geo import PointOfSaleModel
from argenprecios.domain.enums import CurationStatus
from argenprecios.domain.models import (
    ArticleSearchHit,
    FacetBucket,
    SearchFilter,
    SearchPageResult,
)

logger = logging.getLogger(__name__)


class PostgresCatalogSearchRepository:
    """Executes accent- and typo-tolerant article searches with 2-level facets & geo coverage."""

    def __init__(self, session: Session, coverage_lookback_days: int = 45) -> None:
        self._session = session
        self._coverage_lookback_days = coverage_lookback_days

    def search_articles(self, filters: SearchFilter) -> SearchPageResult:
        """Search catalog articles with facets and active provincial/chain availability counts."""
        subcat = aliased(CategoryModel, name="subcat")
        dept = aliased(CategoryModel, name="dept")

        # Determine reference date to anchor active coverage metrics
        anchor_date = filters.reference_date or self._session.scalar(
            select(func.max(PriceSpanModel.last_seen))
        )
        cutoff_date = (
            anchor_date - timedelta(days=self._coverage_lookback_days)
            if anchor_date is not None
            else date(1970, 1, 1)
        )

        cov_conditions = [
            PriceSpanModel.last_seen >= cutoff_date,
            PriceSpanModel.in_stock.is_(True),
        ]
        if filters.reference_date is not None:
            cov_conditions.append(PriceSpanModel.valid_from <= filters.reference_date)

        coverage_subq = (
            select(
                PriceSpanModel.article_id.label("article_id"),
                func.count(func.distinct(PointOfSaleModel.province_id)).label("prov_cnt"),
                func.count(func.distinct(PointOfSaleModel.city_id)).label("city_cnt"),
                func.count(func.distinct(PointOfSaleModel.chain_id)).label("chain_cnt"),
                func.round(
                    func.exp(
                        func.avg(
                            func.ln(
                                func.greatest(
                                    func.coalesce(
                                        PriceSpanModel.promo_price_cents,
                                        PriceSpanModel.list_price_cents,
                                    ),
                                    1,
                                )
                            )
                        )
                    )
                ).label("ref_price_cents"),
            )
            .join(PointOfSaleModel, PointOfSaleModel.id == PriceSpanModel.pos_id)
            .where(*cov_conditions)
            .group_by(PriceSpanModel.article_id)
            .subquery("cov")
        )

        conditions = []
        rank_expr = func.coalesce(coverage_subq.c.prov_cnt, 0).desc()

        q_clean = filters.query.strip()
        if q_clean:
            sku_subq = select(SourceSkuMappingModel.article_id).where(
                SourceSkuMappingModel.external_sku == q_clean
            )
            unaccent_target = func.argenprecios_unaccent(
                func.coalesce(ArticleModel.name, "")
                + " "
                + func.coalesce(ArticleModel.clean_description, "")
                + " "
                + func.coalesce(ArticleModel.brand_name, "")
            )
            unaccent_q = func.argenprecios_unaccent(q_clean)
            ts_query = func.websearch_to_tsquery("spanish", unaccent_q)

            conditions.append(
                or_(
                    ArticleModel.ean == q_clean,
                    ArticleModel.id.in_(sku_subq),
                    ArticleModel.search_vector.op("@@")(ts_query),
                    func.word_similarity(unaccent_q, unaccent_target) >= 0.30,
                    unaccent_target.ilike(f"%{q_clean}%"),
                )
            )
            rank_expr = (
                func.ts_rank_cd(ArticleModel.search_vector, ts_query)
                + func.word_similarity(unaccent_q, unaccent_target)
            ).desc()

        if filters.subcategory_id is not None:
            conditions.append(ArticleModel.category_id == filters.subcategory_id)
        elif filters.department_id is not None:
            conditions.append(
                or_(
                    ArticleModel.category_id == filters.department_id,
                    subcat.parent_id == filters.department_id,
                )
            )

        if filters.brand_id is not None:
            conditions.append(ArticleModel.brand_id == filters.brand_id)

        if filters.package_unit:
            conditions.append(func.lower(ArticleModel.package_unit) == filters.package_unit.lower())

        if filters.generic_variety_id is not None:
            conditions.append(ArticleModel.generic_variety_id == filters.generic_variety_id)

        if filters.tag_slug:
            tag_art_ids = (
                select(article_tags.c.article_id)
                .join(TagModel, TagModel.id == article_tags.c.tag_id)
                .where(TagModel.slug == filters.tag_slug)
            )
            conditions.append(ArticleModel.id.in_(tag_art_ids))

        if filters.curation_status == CurationStatus.UNREVIEWED:
            conditions.append(ArticleModel.is_reviewed.is_(False))
        elif filters.curation_status == CurationStatus.MANUALLY_CURATED:
            conditions.append(ArticleModel.is_manually_curated.is_(True))
        elif filters.curation_status == CurationStatus.AUTO:
            conditions.append(ArticleModel.is_reviewed.is_(True))
            conditions.append(ArticleModel.is_manually_curated.is_(False))

        if filters.min_active_provinces > 0:
            conditions.append(
                func.coalesce(coverage_subq.c.prov_cnt, 0) >= filters.min_active_provinces
            )

        base_from = (
            select(ArticleModel.id)
            .outerjoin(subcat, subcat.id == ArticleModel.category_id)
            .outerjoin(dept, dept.id == subcat.parent_id)
            .outerjoin(coverage_subq, coverage_subq.c.article_id == ArticleModel.id)
        )
        if conditions:
            base_from = base_from.where(*conditions)

        total_count = int(
            self._session.scalar(select(func.count()).select_from(base_from.subquery())) or 0
        )

        page = max(1, filters.page)
        page_size = max(1, min(filters.page_size, 200))
        offset = (page - 1) * page_size

        rows_stmt = (
            select(
                ArticleModel,
                subcat.id.label("subcat_id"),
                subcat.name.label("subcat_name"),
                subcat.level.label("cat_level"),
                dept.id.label("dept_id"),
                dept.name.label("dept_name"),
                GenericVarietyModel.slug.label("variety_slug"),
                BrandModel.name.label("joined_brand_name"),
                func.coalesce(coverage_subq.c.prov_cnt, 0).label("prov_cnt"),
                func.coalesce(coverage_subq.c.city_cnt, 0).label("city_cnt"),
                func.coalesce(coverage_subq.c.chain_cnt, 0).label("chain_cnt"),
                coverage_subq.c.ref_price_cents.label("ref_price_cents"),
            )
            .outerjoin(subcat, subcat.id == ArticleModel.category_id)
            .outerjoin(dept, dept.id == subcat.parent_id)
            .outerjoin(
                GenericVarietyModel,
                GenericVarietyModel.id == ArticleModel.generic_variety_id,
            )
            .outerjoin(BrandModel, BrandModel.id == ArticleModel.brand_id)
            .outerjoin(coverage_subq, coverage_subq.c.article_id == ArticleModel.id)
        )
        if conditions:
            rows_stmt = rows_stmt.where(*conditions)

        rows_stmt = (
            rows_stmt.order_by(
                rank_expr,
                func.coalesce(coverage_subq.c.prov_cnt, 0).desc(),
                ArticleModel.name.asc(),
            )
            .offset(offset)
            .limit(page_size)
        )

        fetched = self._session.execute(rows_stmt).all()
        article_ids = [r[0].id for r in fetched]
        tags_by_article: dict[int, list[str]] = {}
        if article_ids:
            tag_rows = self._session.execute(
                select(article_tags.c.article_id, TagModel.name)
                .join(TagModel, TagModel.id == article_tags.c.tag_id)
                .where(article_tags.c.article_id.in_(article_ids))
                .order_by(TagModel.name.asc())
            ).all()
            for a_id, t_name in tag_rows:
                tags_by_article.setdefault(int(a_id), []).append(str(t_name))

        hits: list[ArticleSearchHit] = []
        for row in fetched:
            art: ArticleModel = row[0]
            cat_lvl = row.cat_level
            if cat_lvl == 1:
                resolved_dept_id = row.subcat_id
                resolved_dept_name = row.subcat_name
                resolved_sub_id = None
                resolved_sub_name = None
            else:
                resolved_dept_id = row.dept_id
                resolved_dept_name = row.dept_name
                resolved_sub_id = row.subcat_id
                resolved_sub_name = row.subcat_name

            hits.append(
                ArticleSearchHit(
                    article_id=art.id,
                    ean=art.ean,
                    name=art.name,
                    clean_description=art.clean_description,
                    brand_id=art.brand_id,
                    brand_name=row.joined_brand_name or art.brand_name,
                    package=art.package,
                    package_amount=art.package_amount,
                    package_unit=art.package_unit,
                    unit_conversion_factor=art.unit_conversion_factor,
                    department_id=resolved_dept_id,
                    department_name=resolved_dept_name,
                    subcategory_id=resolved_sub_id,
                    subcategory_name=resolved_sub_name,
                    generic_variety_id=art.generic_variety_id,
                    generic_variety_slug=row.variety_slug,
                    is_reviewed=art.is_reviewed,
                    is_manually_curated=art.is_manually_curated,
                    tags=tuple(tags_by_article.get(art.id, [])),
                    active_provinces_count=int(row.prov_cnt or 0),
                    active_cities_count=int(row.city_cnt or 0),
                    active_chains_count=int(row.chain_cnt or 0),
                    latest_ref_price_cents=(
                        int(row.ref_price_cents) if row.ref_price_cents is not None else None
                    ),
                )
            )

        matched_ids_subq = base_from.subquery("matched_arts")
        dept_facets, subcat_facets = self._compute_category_facets(matched_ids_subq)
        brand_facets = self._compute_brand_facets(matched_ids_subq)
        tag_facets = self._compute_tag_facets(matched_ids_subq)

        return SearchPageResult(
            items=tuple(hits),
            total_count=total_count,
            page=page,
            page_size=page_size,
            department_facets=dept_facets,
            subcategory_facets=subcat_facets,
            brand_facets=brand_facets,
            tag_facets=tag_facets,
        )

    def _compute_category_facets(
        self,
        matched_ids_subq: Subquery,
    ) -> tuple[tuple[FacetBucket, ...], tuple[FacetBucket, ...]]:
        subcat = aliased(CategoryModel, name="f_subcat")
        dept = aliased(CategoryModel, name="f_dept")

        sub_rows = self._session.execute(
            select(
                subcat.id,
                subcat.name,
                subcat.parent_id,
                subcat.level,
                dept.id.label("p_id"),
                dept.name.label("p_name"),
                func.count(ArticleModel.id).label("cnt"),
            )
            .join(matched_ids_subq, ArticleModel.id == matched_ids_subq.c.id)
            .join(subcat, subcat.id == ArticleModel.category_id)
            .outerjoin(dept, dept.id == subcat.parent_id)
            .group_by(subcat.id, subcat.name, subcat.parent_id, subcat.level, dept.id, dept.name)
            .order_by(text("cnt DESC"))
        ).all()

        dept_totals: dict[int, tuple[str, int]] = {}
        sub_buckets: list[FacetBucket] = []
        for s_id, s_name, s_parent, s_lvl, p_id, p_name, cnt in sub_rows:
            c_int = int(cnt)
            if s_lvl == 1:
                prev_lbl, prev_c = dept_totals.get(int(s_id), (str(s_name), 0))
                dept_totals[int(s_id)] = (prev_lbl, prev_c + c_int)
            else:
                sub_buckets.append(
                    FacetBucket(
                        id_or_slug=int(s_id),
                        label=str(s_name),
                        count=c_int,
                        parent_id=int(s_parent) if s_parent is not None else None,
                    )
                )
                if p_id is not None:
                    prev_lbl, prev_c = dept_totals.get(int(p_id), (str(p_name), 0))
                    dept_totals[int(p_id)] = (prev_lbl, prev_c + c_int)

        dept_buckets = [
            FacetBucket(id_or_slug=d_id, label=lbl, count=cnt)
            for d_id, (lbl, cnt) in sorted(
                dept_totals.items(),
                key=lambda kv: (-kv[1][1], kv[1][0]),
            )
        ]
        return (tuple(dept_buckets), tuple(sub_buckets))

    def _compute_brand_facets(self, matched_ids_subq: Subquery) -> tuple[FacetBucket, ...]:
        rows = self._session.execute(
            select(BrandModel.id, BrandModel.name, func.count(ArticleModel.id).label("cnt"))
            .join(ArticleModel, ArticleModel.brand_id == BrandModel.id)
            .join(matched_ids_subq, ArticleModel.id == matched_ids_subq.c.id)
            .group_by(BrandModel.id, BrandModel.name)
            .order_by(text("cnt DESC"), BrandModel.name.asc())
            .limit(30)
        ).all()
        return tuple(
            FacetBucket(id_or_slug=int(b_id), label=str(b_name), count=int(cnt))
            for b_id, b_name, cnt in rows
        )

    def _compute_tag_facets(self, matched_ids_subq: Subquery) -> tuple[FacetBucket, ...]:
        rows = self._session.execute(
            select(TagModel.slug, TagModel.name, func.count(article_tags.c.article_id).label("cnt"))
            .join(article_tags, article_tags.c.tag_id == TagModel.id)
            .join(matched_ids_subq, article_tags.c.article_id == matched_ids_subq.c.id)
            .group_by(TagModel.slug, TagModel.name)
            .order_by(text("cnt DESC"), TagModel.name.asc())
            .limit(30)
        ).all()
        return tuple(
            FacetBucket(id_or_slug=str(slug), label=str(name), count=int(cnt))
            for slug, name, cnt in rows
        )


__all__ = [
    "PostgresCatalogSearchRepository",
]
