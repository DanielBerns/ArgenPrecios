"""Interactive 2-level category, article, brand, and variety curation use case with undo logs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from argenprecios.adapters.db.models_catalog import (
    ArticleModel,
    BrandModel,
    CatalogEditLogModel,
    CategoryModel,
    CurationRuleModel,
    GenericVarietyModel,
    TagModel,
    UnreviewedArticleModel,
    article_tags,
)
from argenprecios.domain.enums import CanonicalUnit
from argenprecios.domain.unit_normalizer import resolve_unit_conversion, slugify_text

logger = logging.getLogger(__name__)


class CurateCatalogUseCase:
    """Manages human edits to the 2-level taxonomy, articles, brands, varieties, and undo logs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- 1. Two-Level Category Hierarchy (Level 1 Department -> Level 2 Subcategory) ---

    def create_category(
        self,
        name: str,
        level: int,
        parent_id: int | None = None,
        description: str | None = None,
        actor: str = "admin",
    ) -> CategoryModel:
        """Create a Level 1 Department or Level 2 Subcategory and mark it manually curated."""
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            raise ValueError("Category name cannot be empty")
        if level not in {1, 2}:
            raise ValueError("ArgenPrecios enforces a strict 2-level category hierarchy (1 or 2)")

        slug_prefix = ""
        if level == 1:
            if parent_id is not None:
                raise ValueError("Level 1 Department cannot have a parent_id")
        else:
            if parent_id is None:
                raise ValueError("Level 2 Subcategory requires a Level 1 parent_id")
            parent = self._session.get(CategoryModel, parent_id)
            if parent is None or parent.level != 1:
                raise ValueError(
                    f"Parent category {parent_id} must be an existing Level 1 Department"
                )
            slug_prefix = f"{parent.slug}-"

        slug = slugify_text(f"{slug_prefix}{clean_name}")[:140]
        cat = CategoryModel(
            name=clean_name[:120],
            slug=slug,
            level=level,
            parent_id=parent_id,
            description=description[:255] if description else None,
            is_manually_curated=True,
        )
        self._session.add(cat)
        self._session.flush()

        self._record_log(
            entity_type="category",
            entity_id=cat.id,
            action="create_category",
            actor=actor,
            before={},
            after=self._serialize_category(cat),
        )
        self._session.commit()
        return cat

    def update_category(
        self,
        category_id: int,
        name: str | None = None,
        description: str | None = None,
        new_parent_id: int | None = None,
        actor: str = "admin",
    ) -> CategoryModel:
        """Rename/update a Department or Subcategory or move a Subcategory across Departments."""
        cat = self._session.get(CategoryModel, category_id)
        if cat is None:
            raise LookupError(f"Category {category_id} not found")

        before = self._serialize_category(cat)

        if new_parent_id is not None and new_parent_id != cat.parent_id:
            if cat.level != 2:
                raise ValueError("Only Level 2 Subcategories can be moved across Departments")
            new_parent = self._session.get(CategoryModel, new_parent_id)
            if new_parent is None or new_parent.level != 1:
                raise ValueError(f"Target parent {new_parent_id} must be a Level 1 Department")
            cat.parent_id = new_parent_id

        if name is not None and name.strip():
            cat.name = " ".join(name.strip().split())[:120]

        if description is not None:
            cat.description = description.strip()[:255] or None

        cat.is_manually_curated = True
        self._session.flush()

        self._record_log(
            entity_type="category",
            entity_id=cat.id,
            action="update_category",
            actor=actor,
            before=before,
            after=self._serialize_category(cat),
        )
        self._session.commit()
        return cat

    def merge_subcategories(
        self,
        source_subcategory_id: int,
        target_subcategory_id: int,
        actor: str = "admin",
    ) -> int:
        """Merge a duplicate Level 2 Subcategory into target, reassigning articles & varieties."""
        if source_subcategory_id == target_subcategory_id:
            raise ValueError("Source and target subcategories must differ")

        src = self._session.get(CategoryModel, source_subcategory_id)
        tgt = self._session.get(CategoryModel, target_subcategory_id)
        if src is None or tgt is None:
            raise LookupError("Both source and target subcategories must exist")
        if src.level != 2 or tgt.level != 2:
            raise ValueError("merge_subcategories only operates on Level 2 Subcategories")

        affected_article_ids = list(
            self._session.scalars(
                select(ArticleModel.id).where(ArticleModel.category_id == source_subcategory_id)
            )
        )
        affected_variety_ids = list(
            self._session.scalars(
                select(GenericVarietyModel.id).where(
                    GenericVarietyModel.category_id == source_subcategory_id
                )
            )
        )

        now = datetime.now(UTC)
        if affected_article_ids:
            self._session.execute(
                update(ArticleModel)
                .where(ArticleModel.id.in_(affected_article_ids))
                .values(
                    category_id=target_subcategory_id,
                    is_manually_curated=True,
                    curated_at=now,
                )
            )

        if affected_variety_ids:
            self._session.execute(
                update(GenericVarietyModel)
                .where(GenericVarietyModel.id.in_(affected_variety_ids))
                .values(category_id=target_subcategory_id, is_manually_curated=True)
            )

        self._session.execute(
            update(UnreviewedArticleModel)
            .where(UnreviewedArticleModel.suggested_category_id == source_subcategory_id)
            .values(suggested_category_id=target_subcategory_id)
        )
        self._session.execute(
            update(CurationRuleModel)
            .where(CurationRuleModel.target_category_id == source_subcategory_id)
            .values(target_category_id=target_subcategory_id)
        )

        tgt.is_manually_curated = True
        before_snapshot = {
            "deleted_category": self._serialize_category(src),
            "affected_article_ids": affected_article_ids,
            "affected_variety_ids": affected_variety_ids,
        }
        self._session.delete(src)
        self._session.flush()

        self._record_log(
            entity_type="category",
            entity_id=target_subcategory_id,
            action="merge_subcategories",
            actor=actor,
            before=before_snapshot,
            after={"target_category_id": target_subcategory_id},
        )
        self._session.commit()
        return len(affected_article_ids)

    # --- 2. Article, Brand & Variety Curation ---

    def update_article(
        self,
        article_id: int,
        *,
        name: str | None = None,
        clean_description: str | None = None,
        brand_name: str | None = None,
        package: str | None = None,
        package_amount: float | None = None,
        package_unit: str | None = None,
        category_id: int | None = None,
        generic_variety_id: int | None = None,
        tag_names: Sequence[str] | None = None,
        actor: str = "admin",
    ) -> ArticleModel:
        """Apply human edits to an article, lock `is_manually_curated = True`, and log undo state."""
        art = self._session.get(ArticleModel, article_id)
        if art is None:
            raise LookupError(f"Article {article_id} not found")

        before = self._serialize_article(art)

        if name is not None and name.strip():
            art.name = " ".join(name.strip().split())[:255]
        if clean_description is not None and clean_description.strip():
            art.clean_description = " ".join(clean_description.strip().split())[:255]

        if brand_name is not None:
            stripped_brand = " ".join(brand_name.strip().split())
            if stripped_brand:
                brand_id = self._get_or_create_curated_brand(stripped_brand)
                art.brand_id = brand_id
                art.brand_name = stripped_brand[:120]
            else:
                art.brand_id = None
                art.brand_name = None

        recompute_unit = False
        if package is not None:
            art.package = package.strip()[:60] or None
        if package_amount is not None:
            art.package_amount = package_amount if package_amount > 0 else None
            recompute_unit = True
        if package_unit is not None:
            art.package_unit = package_unit.strip()[:20] or None
            recompute_unit = True

        if recompute_unit:
            _, conv_factor = resolve_unit_conversion(art.package_amount, art.package_unit)
            art.unit_conversion_factor = conv_factor
            if art.package_amount and art.package_unit and package is None:
                art.package = f"{art.package_amount:g} {art.package_unit}"[:60]

        if category_id is not None:
            cat = self._session.get(CategoryModel, category_id)
            if cat is None:
                raise LookupError(f"Category {category_id} not found")
            art.category_id = category_id

        if generic_variety_id is not None:
            variety = self._session.get(GenericVarietyModel, generic_variety_id)
            if variety is None:
                raise LookupError(f"GenericVariety {generic_variety_id} not found")
            art.generic_variety_id = generic_variety_id

        if tag_names is not None:
            self._replace_article_tags(art.id, tag_names)

        art.is_reviewed = True
        art.is_manually_curated = True
        art.curated_at = datetime.now(UTC)
        self._session.flush()

        self._record_log(
            entity_type="article",
            entity_id=art.id,
            action="update_article",
            actor=actor,
            before=before,
            after=self._serialize_article(art),
        )
        self._session.commit()
        return art

    def bulk_update_articles(
        self,
        article_ids: Sequence[int],
        *,
        category_id: int | None = None,
        brand_name: str | None = None,
        generic_variety_id: int | None = None,
        actor: str = "admin",
    ) -> int:
        """Apply bulk recategorization, brand, or variety assignment across selected articles."""
        updated = 0
        for aid in article_ids:
            self.update_article(
                aid,
                category_id=category_id,
                brand_name=brand_name,
                generic_variety_id=generic_variety_id,
                actor=actor,
            )
            updated += 1
        return updated

    def merge_brands(
        self,
        source_brand_id: int,
        target_brand_id: int,
        actor: str = "admin",
    ) -> int:
        """Merge a duplicate Brand into target_brand_id and lock reassigned articles."""
        if source_brand_id == target_brand_id:
            raise ValueError("Source and target brands must differ")
        src = self._session.get(BrandModel, source_brand_id)
        tgt = self._session.get(BrandModel, target_brand_id)
        if src is None or tgt is None:
            raise LookupError("Both source and target brands must exist")

        affected_ids = list(
            self._session.scalars(
                select(ArticleModel.id).where(ArticleModel.brand_id == source_brand_id)
            )
        )
        now = datetime.now(UTC)
        if affected_ids:
            self._session.execute(
                update(ArticleModel)
                .where(ArticleModel.id.in_(affected_ids))
                .values(
                    brand_id=target_brand_id,
                    brand_name=tgt.name,
                    is_manually_curated=True,
                    curated_at=now,
                )
            )
        tgt.is_manually_curated = True
        self._session.delete(src)
        self._session.flush()

        self._record_log(
            entity_type="brand",
            entity_id=target_brand_id,
            action="merge_brands",
            actor=actor,
            before={"deleted_brand_name": src.name, "affected_article_ids": affected_ids},
            after={"target_brand_id": target_brand_id, "target_brand_name": tgt.name},
        )
        self._session.commit()
        return len(affected_ids)

    def upsert_generic_variety(
        self,
        slug: str,
        name: str,
        category_id: int | None,
        canonical_unit: CanonicalUnit,
        description: str | None = None,
        actor: str = "admin",
    ) -> GenericVarietyModel:
        """Create or update a canonical GenericVariety and lock it as manually curated."""
        clean_slug = slugify_text(slug)[:160]
        existing = self._session.scalar(
            select(GenericVarietyModel).where(GenericVarietyModel.slug == clean_slug)
        )
        if existing is None:
            gv = GenericVarietyModel(
                slug=clean_slug,
                name=name.strip()[:180],
                category_id=category_id,
                canonical_unit=canonical_unit.value,
                description=description[:255] if description else None,
                is_manually_curated=True,
            )
            self._session.add(gv)
            self._session.flush()
            before: dict[str, Any] = {}
        else:
            gv = existing
            before = {
                "name": gv.name,
                "category_id": gv.category_id,
                "canonical_unit": gv.canonical_unit,
                "description": gv.description,
            }
            gv.name = name.strip()[:180]
            gv.category_id = category_id
            gv.canonical_unit = canonical_unit.value
            gv.description = description[:255] if description else None
            gv.is_manually_curated = True
            self._session.flush()

        self._record_log(
            entity_type="generic_variety",
            entity_id=gv.id,
            action="upsert_generic_variety",
            actor=actor,
            before=before,
            after={
                "slug": gv.slug,
                "name": gv.name,
                "category_id": gv.category_id,
                "canonical_unit": gv.canonical_unit,
            },
        )
        self._session.commit()
        return gv

    def resolve_unreviewed_item(
        self,
        unreviewed_id: int,
        *,
        approve: bool = True,
        category_id: int | None = None,
        brand_name: str | None = None,
        package_amount: float | None = None,
        package_unit: str | None = None,
        actor: str = "admin",
    ) -> UnreviewedArticleModel:
        """Approve or reject an entry in `unreviewed_articles` and lock the linked article."""
        item = self._session.get(UnreviewedArticleModel, unreviewed_id)
        if item is None:
            raise LookupError(f"UnreviewedArticle {unreviewed_id} not found")

        if not approve:
            item.status = "rejected"
            self._session.commit()
            return item

        item.status = "approved"
        if item.resolved_article_id is not None:
            self.update_article(
                item.resolved_article_id,
                category_id=category_id or item.suggested_category_id,
                brand_name=brand_name if brand_name is not None else item.suggested_brand,
                package_amount=package_amount,
                package_unit=package_unit,
                actor=actor,
            )
        self._session.commit()
        return item

    def create_curation_rule(
        self,
        *,
        name: str,
        pattern_regex: str,
        match_brand: str | None = None,
        target_category_id: int | None = None,
        target_brand_id: int | None = None,
        target_generic_variety_id: int | None = None,
        override_package_amount: float | None = None,
        override_package_unit: str | None = None,
        priority: int = 100,
    ) -> CurationRuleModel:
        """Persist a regex editorial rule automatically applied to future novel SKUs."""
        rule = CurationRuleModel(
            name=name.strip()[:140],
            pattern_regex=pattern_regex.strip()[:255],
            match_brand=match_brand.strip()[:120] if match_brand else None,
            target_category_id=target_category_id,
            target_brand_id=target_brand_id,
            target_generic_variety_id=target_generic_variety_id,
            override_package_amount=override_package_amount,
            override_package_unit=override_package_unit,
            priority=priority,
            is_active=True,
        )
        self._session.add(rule)
        self._session.commit()
        return rule

    # --- 3. Reversible Undo Support ---

    def undo_edit(self, log_id: int, actor: str = "admin") -> CatalogEditLogModel:
        """Revert an article or category edit recorded in `catalog_edit_logs`."""
        entry = self._session.get(CatalogEditLogModel, log_id)
        if entry is None:
            raise LookupError(f"CatalogEditLog {log_id} not found")
        if entry.undone_at is not None:
            raise ValueError(f"CatalogEditLog {log_id} was already undone at {entry.undone_at}")

        before = entry.before_json
        if entry.entity_type == "article" and entry.action == "update_article":
            art = self._session.get(ArticleModel, entry.entity_id)
            if art is None:
                raise LookupError(f"Article {entry.entity_id} no longer exists")
            art.name = str(before["name"])
            art.clean_description = str(before["clean_description"])
            art.brand_id = before.get("brand_id")
            art.brand_name = before.get("brand_name")
            art.package = before.get("package")
            art.package_amount = before.get("package_amount")
            art.package_unit = before.get("package_unit")
            art.unit_conversion_factor = before.get("unit_conversion_factor")
            art.category_id = before.get("category_id")
            art.generic_variety_id = before.get("generic_variety_id")
            art.is_reviewed = bool(before.get("is_reviewed", True))
            art.is_manually_curated = bool(before.get("is_manually_curated", False))
            if "tags" in before and isinstance(before["tags"], list):
                self._replace_article_tags(art.id, [str(t) for t in before["tags"]])
        elif entry.entity_type == "category" and entry.action == "update_category":
            cat = self._session.get(CategoryModel, entry.entity_id)
            if cat is None:
                raise LookupError(f"Category {entry.entity_id} no longer exists")
            cat.name = str(before["name"])
            cat.description = before.get("description")
            cat.parent_id = before.get("parent_id")
            cat.is_manually_curated = bool(before.get("is_manually_curated", False))
        elif entry.entity_type == "category" and entry.action == "merge_subcategories":
            deleted_info = before.get("deleted_category") or {}
            restored = CategoryModel(
                name=str(deleted_info["name"]),
                slug=str(deleted_info["slug"]),
                level=int(deleted_info.get("level", 2)),
                description=deleted_info.get("description"),
                parent_id=deleted_info.get("parent_id"),
                is_manually_curated=bool(deleted_info.get("is_manually_curated", False)),
            )
            self._session.add(restored)
            self._session.flush()
            art_ids = before.get("affected_article_ids") or []
            if art_ids:
                self._session.execute(
                    update(ArticleModel)
                    .where(ArticleModel.id.in_(art_ids))
                    .values(category_id=restored.id)
                )
            var_ids = before.get("affected_variety_ids") or []
            if var_ids:
                self._session.execute(
                    update(GenericVarietyModel)
                    .where(GenericVarietyModel.id.in_(var_ids))
                    .values(category_id=restored.id)
                )
        else:
            raise ValueError(f"Undo is not supported for action '{entry.action}'")

        entry.undone_at = datetime.now(UTC)
        self._record_log(
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            action=f"undo:{entry.action}",
            actor=actor,
            before=entry.after_json,
            after=entry.before_json,
        )
        self._session.commit()
        return entry

    # --- Helpers ---

    def _get_or_create_curated_brand(self, brand_name: str) -> int:
        slug = slugify_text(brand_name)[:120]
        stmt = (
            pg_insert(BrandModel)
            .values(name=brand_name[:120], slug=slug, is_manually_curated=True)
            .on_conflict_do_nothing()
        )
        self._session.execute(stmt)
        self._session.flush()
        b_id = self._session.scalar(select(BrandModel.id).where(BrandModel.slug == slug))
        if b_id is None:
            b_id = self._session.scalar(
                select(BrandModel.id).where(BrandModel.name == brand_name[:120])
            )
        if b_id is None:
            raise RuntimeError(f"Could not resolve brand '{brand_name}'")
        return b_id

    def _replace_article_tags(self, article_id: int, tag_names: Sequence[str]) -> None:
        self._session.execute(delete(article_tags).where(article_tags.c.article_id == article_id))
        links: list[dict[str, int]] = []
        for raw_t in tag_names:
            clean_t = raw_t.strip().lower()[:100]
            if not clean_t:
                continue
            slug = slugify_text(clean_t)[:100]
            self._session.execute(
                pg_insert(TagModel).values(name=clean_t, slug=slug).on_conflict_do_nothing()
            )
            self._session.flush()
            tid = self._session.scalar(select(TagModel.id).where(TagModel.slug == slug))
            if tid is not None:
                links.append({"article_id": article_id, "tag_id": tid})
        if links:
            self._session.execute(pg_insert(article_tags).values(links).on_conflict_do_nothing())

    def _serialize_category(self, cat: CategoryModel) -> dict[str, Any]:
        return {
            "id": cat.id,
            "name": cat.name,
            "slug": cat.slug,
            "level": cat.level,
            "description": cat.description,
            "parent_id": cat.parent_id,
            "is_manually_curated": cat.is_manually_curated,
        }

    def _serialize_article(self, art: ArticleModel) -> dict[str, Any]:
        tag_list = list(
            self._session.scalars(
                select(TagModel.name)
                .join(article_tags, article_tags.c.tag_id == TagModel.id)
                .where(article_tags.c.article_id == art.id)
                .order_by(TagModel.name.asc())
            )
        )
        return {
            "id": art.id,
            "ean": art.ean,
            "name": art.name,
            "clean_description": art.clean_description,
            "brand_id": art.brand_id,
            "brand_name": art.brand_name,
            "package": art.package,
            "package_amount": art.package_amount,
            "package_unit": art.package_unit,
            "unit_conversion_factor": art.unit_conversion_factor,
            "category_id": art.category_id,
            "generic_variety_id": art.generic_variety_id,
            "is_reviewed": art.is_reviewed,
            "is_manually_curated": art.is_manually_curated,
            "tags": tag_list,
        }

    def _record_log(
        self,
        *,
        entity_type: str,
        entity_id: int,
        action: str,
        actor: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self._session.add(
            CatalogEditLogModel(
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor_username=actor,
                before_json=before,
                after_json=after,
            )
        )


__all__ = [
    "CurateCatalogUseCase",
]
