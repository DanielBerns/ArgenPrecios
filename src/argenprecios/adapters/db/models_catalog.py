"""SQLAlchemy 2.0 catalog, 2-level taxonomy, generic varieties, search vectors, and curation logs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from argenprecios.adapters.db.base import WarehouseBase

article_tags = Table(
    "article_tags",
    WarehouseBase.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_article_tags_tag_id", "tag_id"),
)


class CategoryModel(WarehouseBase):
    """Two-level retail taxonomy: Level 1 Department -> Level 2 Subcategory."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(140), unique=True, nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    is_manually_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    parent: Mapped[CategoryModel | None] = relationship(
        "CategoryModel",
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list[CategoryModel]] = relationship(
        "CategoryModel",
        back_populates="parent",
        order_by="CategoryModel.name",
    )
    articles: Mapped[list[ArticleModel]] = relationship(
        "ArticleModel",
        back_populates="category",
    )
    varieties: Mapped[list[GenericVarietyModel]] = relationship(
        "GenericVarietyModel",
        back_populates="category",
    )


class BrandModel(WarehouseBase):
    """Cleaned manufacturer or private-label brand entity."""

    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    is_manually_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    articles: Mapped[list[ArticleModel]] = relationship("ArticleModel", back_populates="brand")


class TagModel(WarehouseBase):
    """Facet tag for fine-grained search filtering (e.g., 'sin tacc', 'descremada', 'malbec')."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    articles: Mapped[list[ArticleModel]] = relationship(
        "ArticleModel",
        secondary=article_tags,
        back_populates="tags",
    )


class GenericVarietyModel(WarehouseBase):
    """Homogeneous CPI variety surviving SKU churn and shrinkflation over 8+ years."""

    __tablename__ = "generic_varieties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    canonical_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="u")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_manually_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    category: Mapped[CategoryModel | None] = relationship(
        "CategoryModel",
        back_populates="varieties",
    )
    articles: Mapped[list[ArticleModel]] = relationship(
        "ArticleModel",
        back_populates="generic_variety",
    )


class ArticleModel(WarehouseBase):
    """Concrete barcoded or SKU-mapped retail article with curation lock and full-text search."""

    __tablename__ = "articles"
    __table_args__ = (
        Index("ix_articles_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_articles_curation_flags", "is_manually_curated", "is_reviewed"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ean: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    clean_description: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    raw_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    brand_id: Mapped[int | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    brand_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    package: Mapped[str | None] = mapped_column(String(60), nullable=True)
    package_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_unit: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    unit_conversion_factor: Mapped[float | None] = mapped_column(Float, nullable=True)

    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    generic_variety_id: Mapped[int | None] = mapped_column(
        ForeignKey("generic_varieties.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_manually_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    curated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)

    brand: Mapped[BrandModel | None] = relationship("BrandModel", back_populates="articles")
    category: Mapped[CategoryModel | None] = relationship(
        "CategoryModel",
        back_populates="articles",
    )
    generic_variety: Mapped[GenericVarietyModel | None] = relationship(
        "GenericVarietyModel",
        back_populates="articles",
    )
    tags: Mapped[list[TagModel]] = relationship(
        "TagModel",
        secondary=article_tags,
        back_populates="articles",
    )


class SourceSkuMappingModel(WarehouseBase):
    """Maps non-EAN external scraper SKUs or store PLUs deterministically to canonical articles."""

    __tablename__ = "source_sku_mappings"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "chain_id",
            "external_sku",
            name="uq_source_sku_mappings_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("chains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_sku: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_manually_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UnreviewedArticleModel(WarehouseBase):
    """Staging queue for novel, low-confidence, or unclassified items awaiting human review."""

    __tablename__ = "unreviewed_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ean: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    external_sku: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_channel: Mapped[str] = mapped_column(String(40), nullable=False, default="sepa")
    chain_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_description: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    suggested_package: Mapped[str | None] = mapped_column(String(60), nullable=True)
    suggested_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    resolved_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class CurationRuleModel(WarehouseBase):
    """Persistent editorial rule applied automatically to future unseen SKUs before classifier."""

    __tablename__ = "curation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    pattern_regex: Mapped[str] = mapped_column(String(255), nullable=False)
    match_brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_brand_id: Mapped[int | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_generic_variety_id: Mapped[int | None] = mapped_column(
        ForeignKey("generic_varieties.id", ondelete="SET NULL"),
        nullable=True,
    )
    override_package_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    override_package_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class CatalogEditLogModel(WarehouseBase):
    """Reversible audit log of every human curation edit across categories, brands, and articles."""

    __tablename__ = "catalog_edit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(80), nullable=False, default="cli")
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )


__all__ = [
    "ArticleModel",
    "BrandModel",
    "CatalogEditLogModel",
    "CategoryModel",
    "CurationRuleModel",
    "GenericVarietyModel",
    "SourceSkuMappingModel",
    "TagModel",
    "UnreviewedArticleModel",
    "article_tags",
]
