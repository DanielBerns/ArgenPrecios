"""Read-only catalog seeder from hermes_reborn's hermes_clean.db respecting manual curation locks."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hermes_reborn.taxonomy.categories import DEFAULT_TAXONOMY
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_catalog import (
    ArticleModel,
    BrandModel,
    CategoryModel,
    GenericVarietyModel,
    TagModel,
    article_tags,
)
from argenprecios.config import resolve_user_path
from argenprecios.domain.unit_normalizer import (
    build_variety_slug,
    resolve_unit_conversion,
    slugify_text,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HermesSeedSummary:
    """Summary counts from seeding or syncing the catalog from hermes_clean.db."""

    categories_upserted: int
    brands_upserted: int
    tags_upserted: int
    varieties_upserted: int
    articles_upserted: int
    curated_articles_skipped: int


def seed_default_taxonomy(session: Session) -> dict[tuple[str, str], int]:
    """Ensure the 2-level DEFAULT_TAXONOMY from hermes_reborn exists without touching curated rows."""
    for dept_def in DEFAULT_TAXONOMY:
        dept_slug = dept_def.slug or slugify_text(dept_def.name)
        stmt = (
            pg_insert(CategoryModel)
            .values(
                name=dept_def.name,
                slug=dept_slug,
                level=1,
                description=dept_def.description,
                parent_id=None,
                is_manually_curated=False,
            )
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        session.execute(stmt)
        session.flush()

        dept_id = session.scalar(select(CategoryModel.id).where(CategoryModel.slug == dept_slug))
        if dept_id is None:
            continue

        for sub_def in dept_def.subcategories:
            sub_slug = sub_def.slug or slugify_text(f"{dept_def.name}-{sub_def.name}")
            sub_stmt = (
                pg_insert(CategoryModel)
                .values(
                    name=sub_def.name,
                    slug=sub_slug,
                    level=2,
                    description=sub_def.description,
                    parent_id=dept_id,
                    is_manually_curated=False,
                )
                .on_conflict_do_nothing(index_elements=["slug"])
            )
            session.execute(sub_stmt)

    session.flush()
    return load_category_lookup(session)


def load_category_lookup(session: Session) -> dict[tuple[str, str], int]:
    """Return {(DepartmentName, SubcategoryName): subcategory_id} for all Level 2 categories."""
    depts = {
        row.id: row.name
        for row in session.scalars(select(CategoryModel).where(CategoryModel.level == 1))
    }
    lookup: dict[tuple[str, str], int] = {}
    for sub in session.scalars(select(CategoryModel).where(CategoryModel.level == 2)):
        parent_name = depts.get(sub.parent_id or -1, "Almacén")
        lookup[(parent_name, sub.name)] = sub.id
    return lookup


def seed_from_hermes_sqlite(
    session: Session,
    hermes_db_path: Path,
    batch_size: int = 5_000,
) -> HermesSeedSummary:
    """Read hermes_clean.db strictly read-only (?mode=ro) and upsert non-curated catalog entities."""
    resolved_db = resolve_user_path(hermes_db_path)
    if not resolved_db.is_file():
        raise FileNotFoundError(f"Hermes master catalog SQLite file not found: {resolved_db}")

    ro_uri = f"{resolved_db.as_uri()}?mode=ro"
    logger.info("Opening hermes_clean.db in strict read-only URI mode: %s", ro_uri)

    sqlite_conn = sqlite3.connect(ro_uri, uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    try:
        # 1. Categories (Level 1 first, then Level 2)
        src_cats = sqlite_conn.execute(
            "SELECT id, name, slug, level, description, parent_id FROM categories ORDER BY level, id"
        ).fetchall()

        hermes_cat_id_to_slug: dict[int, str] = {}
        cats_count = 0
        for row in src_cats:
            slug = str(row["slug"])
            hermes_cat_id_to_slug[int(row["id"])] = slug
            parent_pg_id: int | None = None
            if row["parent_id"] is not None:
                parent_slug = hermes_cat_id_to_slug.get(int(row["parent_id"]))
                if parent_slug:
                    parent_pg_id = session.scalar(
                        select(CategoryModel.id).where(CategoryModel.slug == parent_slug)
                    )

            cat_insert = pg_insert(CategoryModel).values(
                name=str(row["name"]),
                slug=slug,
                level=int(row["level"]),
                description=row["description"],
                parent_id=parent_pg_id,
                is_manually_curated=False,
            )
            cat_upsert = cat_insert.on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "name": cat_insert.excluded.name,
                    "description": cat_insert.excluded.description,
                    "parent_id": cat_insert.excluded.parent_id,
                },
                where=CategoryModel.is_manually_curated.is_(False),
            )
            session.execute(cat_upsert)
            cats_count += 1
        session.flush()

        pg_slug_to_cat_id = {c.slug: c.id for c in session.scalars(select(CategoryModel))}
        hermes_cat_to_pg: dict[int, int] = {
            h_id: pg_slug_to_cat_id[slug]
            for h_id, slug in hermes_cat_id_to_slug.items()
            if slug in pg_slug_to_cat_id
        }

        # 2. Brands
        src_brands = sqlite_conn.execute("SELECT id, name, slug FROM brands ORDER BY id").fetchall()
        hermes_brand_id_to_slug: dict[int, str] = {}
        if src_brands:
            for i in range(0, len(src_brands), batch_size):
                chunk = src_brands[i : i + batch_size]
                values = []
                for b in chunk:
                    hermes_brand_id_to_slug[int(b["id"])] = str(b["slug"])
                    values.append(
                        {
                            "name": str(b["name"]),
                            "slug": str(b["slug"]),
                            "is_manually_curated": False,
                        }
                    )
                b_stmt = (
                    pg_insert(BrandModel)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=["slug"])
                )
                session.execute(b_stmt)
        session.flush()
        pg_slug_to_brand_id = {b.slug: b.id for b in session.scalars(select(BrandModel))}
        hermes_brand_to_pg: dict[int, int] = {
            h_id: pg_slug_to_brand_id[slug]
            for h_id, slug in hermes_brand_id_to_slug.items()
            if slug in pg_slug_to_brand_id
        }

        # 3. Tags
        src_tags = sqlite_conn.execute("SELECT id, name, slug FROM tags ORDER BY id").fetchall()
        hermes_tag_id_to_slug: dict[int, str] = {}
        if src_tags:
            for i in range(0, len(src_tags), batch_size):
                chunk = src_tags[i : i + batch_size]
                values = []
                for t in chunk:
                    hermes_tag_id_to_slug[int(t["id"])] = str(t["slug"])
                    values.append({"name": str(t["name"]), "slug": str(t["slug"])})
                t_stmt = (
                    pg_insert(TagModel)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=["slug"])
                )
                session.execute(t_stmt)
        session.flush()
        pg_slug_to_tag_id = {t.slug: t.id for t in session.scalars(select(TagModel))}
        hermes_tag_to_pg: dict[int, int] = {
            h_id: pg_slug_to_tag_id[slug]
            for h_id, slug in hermes_tag_id_to_slug.items()
            if slug in pg_slug_to_tag_id
        }

        # 4. Articles & Baseline Generic Varieties (skipping is_manually_curated = True)
        curated_eans: set[str] = {
            ean
            for ean in session.scalars(
                select(ArticleModel.ean).where(
                    ArticleModel.is_manually_curated.is_(True),
                    ArticleModel.ean.is_not(None),
                )
            )
            if ean is not None
        }

        src_articles = sqlite_conn.execute(
            """
            SELECT id, ean, name, clean_description, raw_description,
                   brand_id, brand_name, package, package_amount, package_unit, category_id
            FROM articles
            ORDER BY id
            """
        ).fetchall()

        cat_id_to_slug = {v: k for k, v in pg_slug_to_cat_id.items()}
        variety_payloads: dict[str, dict[str, object]] = {}
        for art in src_articles:
            ean = str(art["ean"]).strip()
            if not ean or ean in curated_eans:
                continue
            pg_cat_id = (
                hermes_cat_to_pg.get(int(art["category_id"]))
                if art["category_id"] is not None
                else None
            )
            pkg_amt = float(art["package_amount"]) if art["package_amount"] is not None else None
            pkg_unit = str(art["package_unit"]) if art["package_unit"] is not None else None
            c_unit, _ = resolve_unit_conversion(pkg_amt, pkg_unit)
            cat_slug = cat_id_to_slug.get(pg_cat_id or -1, "almacen")
            clean_desc = str(art["clean_description"] or art["name"])
            v_slug = build_variety_slug(cat_slug, clean_desc, c_unit)
            if v_slug not in variety_payloads:
                variety_payloads[v_slug] = {
                    "slug": v_slug,
                    "name": f"{clean_desc.title()} ({c_unit.value})"[:180],
                    "category_id": pg_cat_id,
                    "canonical_unit": c_unit.value,
                    "is_manually_curated": False,
                }

        variety_list = list(variety_payloads.values())
        for i in range(0, len(variety_list), batch_size):
            v_chunk = variety_list[i : i + batch_size]
            v_stmt = (
                pg_insert(GenericVarietyModel)
                .values(v_chunk)
                .on_conflict_do_nothing(index_elements=["slug"])
            )
            session.execute(v_stmt)
        session.flush()

        pg_slug_to_variety_id = {v.slug: v.id for v in session.scalars(select(GenericVarietyModel))}

        curated_skipped = 0
        articles_upserted = 0
        hermes_art_id_to_ean: dict[int, str] = {}

        for i in range(0, len(src_articles), batch_size):
            a_chunk = src_articles[i : i + batch_size]
            art_rows: list[dict[str, object]] = []
            for art in a_chunk:
                ean = str(art["ean"]).strip()
                if not ean:
                    continue
                hermes_art_id_to_ean[int(art["id"])] = ean
                if ean in curated_eans:
                    curated_skipped += 1
                    continue

                pg_cat_id = (
                    hermes_cat_to_pg.get(int(art["category_id"]))
                    if art["category_id"] is not None
                    else None
                )
                pg_brand_id = (
                    hermes_brand_to_pg.get(int(art["brand_id"]))
                    if art["brand_id"] is not None
                    else None
                )
                pkg_amt = (
                    float(art["package_amount"]) if art["package_amount"] is not None else None
                )
                pkg_unit = str(art["package_unit"]) if art["package_unit"] is not None else None
                c_unit, conv_factor = resolve_unit_conversion(pkg_amt, pkg_unit)
                cat_slug = cat_id_to_slug.get(pg_cat_id or -1, "almacen")
                clean_desc = str(art["clean_description"] or art["name"])
                v_slug = build_variety_slug(cat_slug, clean_desc, c_unit)

                art_rows.append(
                    {
                        "ean": ean,
                        "name": str(art["name"])[:255],
                        "clean_description": clean_desc[:255],
                        "raw_description": art["raw_description"],
                        "brand_id": pg_brand_id,
                        "brand_name": art["brand_name"],
                        "package": art["package"],
                        "package_amount": pkg_amt,
                        "package_unit": pkg_unit,
                        "unit_conversion_factor": conv_factor,
                        "category_id": pg_cat_id,
                        "generic_variety_id": pg_slug_to_variety_id.get(v_slug),
                        "is_reviewed": True,
                        "is_manually_curated": False,
                    }
                )

            if art_rows:
                a_ins = pg_insert(ArticleModel).values(art_rows)
                a_up = a_ins.on_conflict_do_update(
                    index_elements=["ean"],
                    set_={
                        "name": a_ins.excluded.name,
                        "clean_description": a_ins.excluded.clean_description,
                        "raw_description": a_ins.excluded.raw_description,
                        "brand_id": a_ins.excluded.brand_id,
                        "brand_name": a_ins.excluded.brand_name,
                        "package": a_ins.excluded.package,
                        "package_amount": a_ins.excluded.package_amount,
                        "package_unit": a_ins.excluded.package_unit,
                        "unit_conversion_factor": a_ins.excluded.unit_conversion_factor,
                        "category_id": a_ins.excluded.category_id,
                        "generic_variety_id": a_ins.excluded.generic_variety_id,
                        "is_reviewed": True,
                    },
                    where=ArticleModel.is_manually_curated.is_(False),
                )
                session.execute(a_up)
                articles_upserted += len(art_rows)

        session.flush()

        # 5. Article <-> Tag links (only for non-curated articles)
        ean_to_pg_art_id = {
            a.ean: a.id
            for a in session.scalars(select(ArticleModel).where(ArticleModel.ean.is_not(None)))
            if a.ean is not None
        }
        src_art_tags = sqlite_conn.execute("SELECT article_id, tag_id FROM article_tags").fetchall()
        tag_links: list[dict[str, int]] = []
        for link in src_art_tags:
            ean_opt = hermes_art_id_to_ean.get(int(link["article_id"]))
            if not ean_opt or ean_opt in curated_eans:
                continue
            pg_art_id = ean_to_pg_art_id.get(ean_opt)
            pg_tag_id = hermes_tag_to_pg.get(int(link["tag_id"]))
            if pg_art_id is not None and pg_tag_id is not None:
                tag_links.append({"article_id": pg_art_id, "tag_id": pg_tag_id})

        for i in range(0, len(tag_links), batch_size):
            link_chunk = tag_links[i : i + batch_size]
            link_stmt = pg_insert(article_tags).values(link_chunk).on_conflict_do_nothing()
            session.execute(link_stmt)

        session.commit()

        summary = HermesSeedSummary(
            categories_upserted=cats_count,
            brands_upserted=len(src_brands),
            tags_upserted=len(src_tags),
            varieties_upserted=len(variety_list),
            articles_upserted=articles_upserted,
            curated_articles_skipped=curated_skipped,
        )
        logger.info("Completed Hermes catalog seed: %s", summary)
        return summary
    finally:
        sqlite_conn.close()


__all__ = [
    "HermesSeedSummary",
    "load_category_lookup",
    "seed_default_taxonomy",
    "seed_from_hermes_sqlite",
]
