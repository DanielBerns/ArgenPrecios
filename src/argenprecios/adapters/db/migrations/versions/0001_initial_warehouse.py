"""Initial PostgreSQL warehouse schema, search indexes, quarterly partitions, and seed data.

Revision ID: 0001_initial_warehouse
Revises: None
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from argenprecios.adapters.db.partition_manager import ensure_quarterly_partition
from argenprecios.config import (
    AnalyticsSettings,
    load_government_periods,
    load_regional_weights,
)

revision: str = "0001_initial_warehouse"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. PostgreSQL Spanish search & trigram extensions + immutable unaccent wrapper
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION argenprecios_unaccent(input_text text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE PARALLEL SAFE STRICT
        AS $$
            SELECT unaccent('unaccent', input_text);
        $$;
        """
    )

    # 2. Geography & POS tables
    op.create_table(
        "statistical_regions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("expenditure_weight", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_statistical_regions"),
        sa.UniqueConstraint("code", name="uq_statistical_regions_code"),
    )
    op.create_index("ix_statistical_regions_code", "statistical_regions", ["code"])

    op.create_table(
        "provinces",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=False),
        sa.Column("population_share", sa.Float(), nullable=False),
        sa.Column("expenditure_share_within_region", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["region_id"],
            ["statistical_regions.id"],
            name="fk_provinces_region_id_statistical_regions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provinces"),
        sa.UniqueConstraint("code", name="uq_provinces_code"),
    )
    op.create_index("ix_provinces_code", "provinces", ["code"])
    op.create_index("ix_provinces_region_id", "provinces", ["region_id"])

    op.create_table(
        "cities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("province_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.ForeignKeyConstraint(
            ["province_id"],
            ["provinces.id"],
            name="fk_cities_province_id_provinces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cities"),
        sa.UniqueConstraint("province_id", "slug", name="uq_cities_province_slug"),
    )
    op.create_index("ix_cities_province_id", "cities", ["province_id"])
    op.create_index("ix_cities_slug", "cities", ["slug"])

    op.create_table(
        "chains",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("holding_group", sa.String(length=120), nullable=True),
        sa.Column("default_pos_channel", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_chains"),
        sa.UniqueConstraint("slug", name="uq_chains_slug"),
    )
    op.create_index("ix_chains_slug", "chains", ["slug"])

    op.create_table(
        "points_of_sale",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("sepa_id", sa.String(length=40), nullable=True),
        sa.Column("comercio_id", sa.Integer(), nullable=True),
        sa.Column("bandera_id", sa.Integer(), nullable=True),
        sa.Column("sucursal_id", sa.Integer(), nullable=True),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("province_id", sa.Integer(), nullable=False),
        sa.Column("pos_channel", sa.String(length=40), nullable=False),
        sa.Column("branch_name", sa.String(length=160), nullable=False),
        sa.Column("address", sa.String(length=220), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(
            ["chain_id"],
            ["chains.id"],
            name="fk_points_of_sale_chain_id_chains",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["city_id"],
            ["cities.id"],
            name="fk_points_of_sale_city_id_cities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["province_id"],
            ["provinces.id"],
            name="fk_points_of_sale_province_id_provinces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_points_of_sale"),
        sa.UniqueConstraint("code", name="uq_points_of_sale_code"),
    )
    op.create_index("ix_points_of_sale_code", "points_of_sale", ["code"])
    op.create_index("ix_points_of_sale_sepa_id", "points_of_sale", ["sepa_id"])
    op.create_index("ix_points_of_sale_chain_id", "points_of_sale", ["chain_id"])
    op.create_index("ix_points_of_sale_city_id", "points_of_sale", ["city_id"])
    op.create_index("ix_points_of_sale_province_id", "points_of_sale", ["province_id"])
    op.create_index("ix_points_of_sale_pos_channel", "points_of_sale", ["pos_channel"])
    op.create_index("ix_pos_province_channel", "points_of_sale", ["province_id", "pos_channel"])
    op.create_index("ix_pos_chain_city", "points_of_sale", ["chain_id", "city_id"])

    # 3. 2-Level Catalog, Varieties & Curation tables
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=140), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_manually_curated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["categories.id"],
            name="fk_categories_parent_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("slug", name="uq_categories_slug"),
    )
    op.create_index("ix_categories_name", "categories", ["name"])
    op.create_index("ix_categories_slug", "categories", ["slug"])
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])

    op.create_table(
        "brands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column(
            "is_manually_curated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_brands"),
        sa.UniqueConstraint("name", name="uq_brands_name"),
        sa.UniqueConstraint("slug", name="uq_brands_slug"),
    )
    op.create_index("ix_brands_name", "brands", ["name"])
    op.create_index("ix_brands_slug", "brands", ["slug"])

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.UniqueConstraint("name", name="uq_tags_name"),
        sa.UniqueConstraint("slug", name="uq_tags_slug"),
    )
    op.create_index("ix_tags_name", "tags", ["name"])
    op.create_index("ix_tags_slug", "tags", ["slug"])

    op.create_table(
        "generic_varieties",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("canonical_unit", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "is_manually_curated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_generic_varieties_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generic_varieties"),
        sa.UniqueConstraint("slug", name="uq_generic_varieties_slug"),
    )
    op.create_index("ix_generic_varieties_slug", "generic_varieties", ["slug"])
    op.create_index("ix_generic_varieties_name", "generic_varieties", ["name"])
    op.create_index("ix_generic_varieties_category_id", "generic_varieties", ["category_id"])

    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ean", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("clean_description", sa.String(length=255), nullable=False),
        sa.Column("raw_description", sa.Text(), nullable=True),
        sa.Column("brand_id", sa.Integer(), nullable=True),
        sa.Column("brand_name", sa.String(length=120), nullable=True),
        sa.Column("package", sa.String(length=60), nullable=True),
        sa.Column("package_amount", sa.Float(), nullable=True),
        sa.Column("package_unit", sa.String(length=20), nullable=True),
        sa.Column("unit_conversion_factor", sa.Float(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("generic_variety_id", sa.Integer(), nullable=True),
        sa.Column("is_reviewed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "is_manually_curated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("curated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
            name="fk_articles_brand_id_brands",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_articles_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["generic_variety_id"],
            ["generic_varieties.id"],
            name="fk_articles_generic_variety_id_generic_varieties",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_articles"),
        sa.UniqueConstraint("ean", name="uq_articles_ean"),
    )
    op.create_index("ix_articles_ean", "articles", ["ean"])
    op.create_index("ix_articles_name", "articles", ["name"])
    op.create_index("ix_articles_clean_description", "articles", ["clean_description"])
    op.create_index("ix_articles_brand_id", "articles", ["brand_id"])
    op.create_index("ix_articles_package_unit", "articles", ["package_unit"])
    op.create_index("ix_articles_category_id", "articles", ["category_id"])
    op.create_index("ix_articles_generic_variety_id", "articles", ["generic_variety_id"])
    op.create_index(
        "ix_articles_curation_flags",
        "articles",
        ["is_manually_curated", "is_reviewed"],
    )
    op.create_index(
        "ix_articles_search_vector",
        "articles",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.execute(
        """
        CREATE INDEX ix_articles_trgm_text
        ON articles
        USING gin (
            argenprecios_unaccent(
                coalesce(name, '') || ' ' ||
                coalesce(clean_description, '') || ' ' ||
                coalesce(brand_name, '')
            ) gin_trgm_ops
        );
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION argenprecios_articles_tsvector_trg()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.search_vector :=
                setweight(
                    to_tsvector('spanish', argenprecios_unaccent(coalesce(NEW.name, ''))),
                    'A'
                ) ||
                setweight(
                    to_tsvector('spanish', argenprecios_unaccent(coalesce(NEW.brand_name, ''))),
                    'A'
                ) ||
                setweight(
                    to_tsvector(
                        'spanish',
                        argenprecios_unaccent(coalesce(NEW.clean_description, ''))
                    ),
                    'B'
                ) ||
                setweight(to_tsvector('simple', coalesce(NEW.ean, '')), 'A');
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_articles_search_vector
        BEFORE INSERT OR UPDATE OF name, clean_description, brand_name, ean
        ON articles
        FOR EACH ROW
        EXECUTE FUNCTION argenprecios_articles_tsvector_trg();
        """
    )

    op.create_table(
        "article_tags",
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_article_tags_article_id_articles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name="fk_article_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", "tag_id", name="pk_article_tags"),
    )
    op.create_index("ix_article_tags_tag_id", "article_tags", ["tag_id"])

    # 4. Provenance, Ingestion Batches, Promos, Regimes & Partitioned price_spans
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_data_sources"),
        sa.UniqueConstraint("code", name="uq_data_sources_code"),
    )
    op.create_index("ix_data_sources_code", "data_sources", ["code"])

    op.create_table(
        "source_sku_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("external_sku", sa.String(length=80), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column(
            "is_manually_curated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name="fk_source_sku_mappings_source_id_data_sources",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chain_id"],
            ["chains.id"],
            name="fk_source_sku_mappings_chain_id_chains",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_source_sku_mappings_article_id_articles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_sku_mappings"),
        sa.UniqueConstraint(
            "source_id",
            "chain_id",
            "external_sku",
            name="uq_source_sku_mappings_key",
        ),
    )
    op.create_index("ix_source_sku_mappings_source_id", "source_sku_mappings", ["source_id"])
    op.create_index("ix_source_sku_mappings_chain_id", "source_sku_mappings", ["chain_id"])
    op.create_index(
        "ix_source_sku_mappings_external_sku",
        "source_sku_mappings",
        ["external_sku"],
    )
    op.create_index("ix_source_sku_mappings_article_id", "source_sku_mappings", ["article_id"])

    op.create_table(
        "unreviewed_articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ean", sa.String(length=32), nullable=True),
        sa.Column("external_sku", sa.String(length=80), nullable=True),
        sa.Column("source_channel", sa.String(length=40), nullable=False),
        sa.Column("chain_slug", sa.String(length=120), nullable=True),
        sa.Column("raw_description", sa.Text(), nullable=False),
        sa.Column("suggested_brand", sa.String(length=120), nullable=True),
        sa.Column("suggested_package", sa.String(length=60), nullable=True),
        sa.Column("suggested_category_id", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("resolved_article_id", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggested_category_id"],
            ["categories.id"],
            name="fk_unreviewed_articles_suggested_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_article_id"],
            ["articles.id"],
            name="fk_unreviewed_articles_resolved_article_id_articles",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_unreviewed_articles"),
    )
    op.create_index("ix_unreviewed_articles_ean", "unreviewed_articles", ["ean"])
    op.create_index(
        "ix_unreviewed_articles_external_sku",
        "unreviewed_articles",
        ["external_sku"],
    )
    op.create_index("ix_unreviewed_articles_status", "unreviewed_articles", ["status"])

    op.create_table(
        "curation_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=140), nullable=False),
        sa.Column("pattern_regex", sa.String(length=255), nullable=False),
        sa.Column("match_brand", sa.String(length=120), nullable=True),
        sa.Column("target_category_id", sa.Integer(), nullable=True),
        sa.Column("target_brand_id", sa.Integer(), nullable=True),
        sa.Column("target_generic_variety_id", sa.Integer(), nullable=True),
        sa.Column("override_package_amount", sa.Float(), nullable=True),
        sa.Column("override_package_unit", sa.String(length=20), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_category_id"],
            ["categories.id"],
            name="fk_curation_rules_target_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_brand_id"],
            ["brands.id"],
            name="fk_curation_rules_target_brand_id_brands",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_generic_variety_id"],
            ["generic_varieties.id"],
            name="fk_curation_rules_target_generic_variety_id_generic_varieties",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_curation_rules"),
    )
    op.create_index("ix_curation_rules_priority", "curation_rules", ["priority"])

    op.create_table(
        "catalog_edit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("actor_username", sa.String(length=80), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_edit_logs"),
    )
    op.create_index("ix_catalog_edit_logs_entity_type", "catalog_edit_logs", ["entity_type"])
    op.create_index("ix_catalog_edit_logs_entity_id", "catalog_edit_logs", ["entity_id"])
    op.create_index("ix_catalog_edit_logs_created_at", "catalog_edit_logs", ["created_at"])

    op.create_table(
        "ingestion_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column("sha256_digest", sa.String(length=64), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("spans_extended", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("spans_inserted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("new_articles_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name="fk_ingestion_batches_source_id_data_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_batches"),
        sa.UniqueConstraint(
            "source_id",
            "sha256_digest",
            name="uq_ingestion_batches_source_sha256",
        ),
    )
    op.create_index("ix_ingestion_batches_source_id", "ingestion_batches", ["source_id"])
    op.create_index("ix_ingestion_batches_sha256_digest", "ingestion_batches", ["sha256_digest"])
    op.create_index("ix_ingestion_batches_data_date", "ingestion_batches", ["data_date"])
    op.create_index("ix_ingestion_batches_status", "ingestion_batches", ["status"])

    op.create_table(
        "promo_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("mechanism", sa.String(length=32), nullable=False),
        sa.Column("raw_legend", sa.String(length=255), nullable=False),
        sa.Column("discount_factor", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_promo_rules"),
        sa.UniqueConstraint("code_hash", name="uq_promo_rules_code_hash"),
    )
    op.create_index("ix_promo_rules_code_hash", "promo_rules", ["code_hash"])
    op.create_index("ix_promo_rules_mechanism", "promo_rules", ["mechanism"])

    op.create_table(
        "monetary_regimes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("currency_code", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("factor_to_base_ars", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_monetary_regimes"),
        sa.UniqueConstraint("currency_code", name="uq_monetary_regimes_currency_code"),
    )

    op.create_table(
        "price_spans",
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("pos_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("observed_days", sa.Integer(), nullable=False),
        sa.Column("list_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("promo_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("promo_rule_id", sa.Integer(), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_price_spans_article_id_articles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pos_id"],
            ["points_of_sale.id"],
            name="fk_price_spans_pos_id_points_of_sale",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name="fk_price_spans_source_id_data_sources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["promo_rule_id"],
            ["promo_rules.id"],
            name="fk_price_spans_promo_rule_id_promo_rules",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "article_id",
            "pos_id",
            "source_id",
            "valid_from",
            name="pk_price_spans",
        ),
        postgresql_partition_by="RANGE (valid_from)",
    )
    op.create_index(
        "ix_price_spans_article_valid_from",
        "price_spans",
        ["article_id", "valid_from"],
    )
    op.create_index(
        "ix_price_spans_pos_last_seen",
        "price_spans",
        ["pos_id", "last_seen"],
    )

    op.create_table(
        "chain_city_spans",
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("pos_channel", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("observed_days", sa.Integer(), nullable=False),
        sa.Column("branch_count", sa.Integer(), nullable=False),
        sa.Column("list_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("promo_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_chain_city_spans_article_id_articles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chain_id"],
            ["chains.id"],
            name="fk_chain_city_spans_chain_id_chains",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["city_id"],
            ["cities.id"],
            name="fk_chain_city_spans_city_id_cities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name="fk_chain_city_spans_source_id_data_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "article_id",
            "chain_id",
            "city_id",
            "pos_channel",
            "source_id",
            "valid_from",
            name="pk_chain_city_spans",
        ),
    )
    op.create_index(
        "ix_chain_city_spans_article_valid_from",
        "chain_city_spans",
        ["article_id", "valid_from"],
    )

    # 5. Baskets, Rollups, CPI Series, Presidential Periods, Macro & Admin Auth
    op.create_table(
        "baskets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("uuid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_baskets"),
        sa.UniqueConstraint("uuid", name="uq_baskets_uuid"),
    )
    op.create_index("ix_baskets_uuid", "baskets", ["uuid"])
    op.create_index("ix_baskets_name", "baskets", ["name"])

    op.create_table(
        "basket_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("basket_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ean", sa.String(length=32), nullable=True),
        sa.Column("article_id", sa.Integer(), nullable=True),
        sa.Column("fallback_generic_variety_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("numerator", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("denominator", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("target_amount", sa.Float(), nullable=True),
        sa.Column("target_unit", sa.String(length=16), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("display_brand", sa.String(length=120), nullable=True),
        sa.Column("display_category", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["baskets.id"],
            name="fk_basket_items_basket_id_baskets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name="fk_basket_items_article_id_articles",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["fallback_generic_variety_id"],
            ["generic_varieties.id"],
            name="fk_basket_items_fallback_generic_variety_id_generic_varieties",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_basket_items"),
    )
    op.create_index("ix_basket_items_basket_id", "basket_items", ["basket_id"])
    op.create_index("ix_basket_items_ean", "basket_items", ["ean"])
    op.create_index("ix_basket_items_article_id", "basket_items", ["article_id"])

    op.create_table(
        "basket_item_substitutes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("basket_item_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("substitute_ean", sa.String(length=32), nullable=False),
        sa.Column("substitute_article_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["basket_item_id"],
            ["basket_items.id"],
            name="fk_basket_item_substitutes_basket_item_id_basket_items",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["substitute_article_id"],
            ["articles.id"],
            name="fk_basket_item_substitutes_substitute_article_id_articles",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_basket_item_substitutes"),
    )
    op.create_index(
        "ix_basket_item_substitutes_basket_item_id",
        "basket_item_substitutes",
        ["basket_item_id"],
    )

    op.create_table(
        "basket_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("basket_id", sa.Integer(), nullable=False),
        sa.Column("evaluated_date", sa.Date(), nullable=False),
        sa.Column("geo_level", sa.String(length=24), nullable=False),
        sa.Column("geo_code", sa.String(length=40), nullable=False),
        sa.Column("geo_name", sa.String(length=120), nullable=False),
        sa.Column("weighting", sa.String(length=24), nullable=False),
        sa.Column("total_items_count", sa.Integer(), nullable=False),
        sa.Column("matched_items_count", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Float(), nullable=False),
        sa.Column("min_total_cents", sa.BigInteger(), nullable=True),
        sa.Column("geo_mean_reference_total_cents", sa.BigInteger(), nullable=True),
        sa.Column("max_total_cents", sa.BigInteger(), nullable=True),
        sa.Column("best_single_store_cents", sa.BigInteger(), nullable=True),
        sa.Column("best_single_store_code", sa.String(length=80), nullable=True),
        sa.Column("worst_single_store_cents", sa.BigInteger(), nullable=True),
        sa.Column("complete_stores_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["baskets.id"],
            name="fk_basket_evaluations_basket_id_baskets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_basket_evaluations"),
        sa.UniqueConstraint(
            "basket_id",
            "evaluated_date",
            "geo_level",
            "geo_code",
            "weighting",
            name="uq_basket_evaluations_scope",
        ),
    )
    op.create_index("ix_basket_evaluations_basket_id", "basket_evaluations", ["basket_id"])
    op.create_index(
        "ix_basket_eval_date_geo",
        "basket_evaluations",
        ["evaluated_date", "geo_level", "geo_code"],
    )

    op.create_table(
        "basket_evaluation_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", sa.Integer(), nullable=False),
        sa.Column("requested_ean", sa.String(length=32), nullable=True),
        sa.Column("resolved_via", sa.String(length=32), nullable=False),
        sa.Column("resolved_identifier", sa.String(length=160), nullable=True),
        sa.Column("effective_multiplier", sa.Float(), nullable=False),
        sa.Column("min_line_cents", sa.BigInteger(), nullable=True),
        sa.Column("geo_mean_line_cents", sa.BigInteger(), nullable=True),
        sa.Column("max_line_cents", sa.BigInteger(), nullable=True),
        sa.Column("observing_pos_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["basket_evaluations.id"],
            name="fk_basket_evaluation_items_evaluation_id_basket_evaluations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_basket_evaluation_items"),
    )
    op.create_index(
        "ix_basket_evaluation_items_evaluation_id",
        "basket_evaluation_items",
        ["evaluation_id"],
    )

    op.create_table(
        "daily_geo_variety_rollups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rollup_date", sa.Date(), nullable=False),
        sa.Column("geo_level", sa.String(length=24), nullable=False),
        sa.Column("geo_code", sa.String(length=40), nullable=False),
        sa.Column("generic_variety_id", sa.Integer(), nullable=False),
        sa.Column("channel_scope", sa.String(length=40), nullable=False),
        sa.Column("active_pos_count", sa.Integer(), nullable=False),
        sa.Column("in_stock_pos_count", sa.Integer(), nullable=False),
        sa.Column("distinct_articles_count", sa.Integer(), nullable=False),
        sa.Column("geo_mean_unit_list_cents", sa.BigInteger(), nullable=False),
        sa.Column("geo_mean_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("min_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("median_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("max_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("iqr_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("promo_pos_count", sa.Integer(), nullable=False),
        sa.Column("promo_gap_pct", sa.Float(), nullable=False),
        sa.Column("stockout_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generic_variety_id"],
            ["generic_varieties.id"],
            name="fk_daily_geo_variety_rollups_generic_variety_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_daily_geo_variety_rollups"),
        sa.UniqueConstraint(
            "rollup_date",
            "geo_level",
            "geo_code",
            "generic_variety_id",
            "channel_scope",
            name="uq_daily_geo_variety_rollups_cell",
        ),
    )
    op.create_index(
        "ix_daily_rollup_geo_date",
        "daily_geo_variety_rollups",
        ["geo_level", "geo_code", "rollup_date"],
    )
    op.create_index(
        "ix_daily_rollup_variety_date",
        "daily_geo_variety_rollups",
        ["generic_variety_id", "rollup_date"],
    )

    op.create_table(
        "monthly_geo_variety_rollups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("geo_level", sa.String(length=24), nullable=False),
        sa.Column("geo_code", sa.String(length=40), nullable=False),
        sa.Column("generic_variety_id", sa.Integer(), nullable=False),
        sa.Column("channel_scope", sa.String(length=40), nullable=False),
        sa.Column("days_observed", sa.Integer(), nullable=False),
        sa.Column("avg_active_pos_count", sa.Float(), nullable=False),
        sa.Column("geo_mean_unit_list_cents", sa.BigInteger(), nullable=False),
        sa.Column("geo_mean_unit_effective_cents", sa.BigInteger(), nullable=False),
        sa.Column("avg_promo_gap_pct", sa.Float(), nullable=False),
        sa.Column("avg_stockout_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generic_variety_id"],
            ["generic_varieties.id"],
            name="fk_monthly_geo_variety_rollups_generic_variety_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monthly_geo_variety_rollups"),
        sa.UniqueConstraint(
            "month_start",
            "geo_level",
            "geo_code",
            "generic_variety_id",
            "channel_scope",
            name="uq_monthly_geo_variety_rollups_cell",
        ),
    )
    op.create_index(
        "ix_monthly_rollup_geo_month",
        "monthly_geo_variety_rollups",
        ["geo_level", "geo_code", "month_start"],
    )

    op.create_table(
        "price_index_series",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_date", sa.Date(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("geo_level", sa.String(length=24), nullable=False),
        sa.Column("geo_code", sa.String(length=40), nullable=False),
        sa.Column("category_scope", sa.String(length=140), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("channel_scope", sa.String(length=40), nullable=False),
        sa.Column("weighting", sa.String(length=24), nullable=False),
        sa.Column("index_value", sa.Float(), nullable=False),
        sa.Column("period_pct_change", sa.Float(), nullable=True),
        sa.Column("yoy_pct_change", sa.Float(), nullable=True),
        sa.Column("overlapping_varieties_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_price_index_series_category_id_categories",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_index_series"),
        sa.UniqueConstraint(
            "period_date",
            "frequency",
            "geo_level",
            "geo_code",
            "category_scope",
            "channel_scope",
            "weighting",
            name="uq_price_index_series_point",
        ),
    )
    op.create_index(
        "ix_price_index_geo_date",
        "price_index_series",
        ["geo_level", "geo_code", "period_date"],
    )

    op.create_table(
        "government_periods",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("administration_label", sa.String(length=120), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("period_kind", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_government_periods"),
        sa.UniqueConstraint("slug", name="uq_government_periods_slug"),
    )
    op.create_index("ix_government_periods_slug", "government_periods", ["slug"])
    op.create_index("ix_government_periods_start_date", "government_periods", ["start_date"])
    op.create_index("ix_government_periods_end_date", "government_periods", ["end_date"])

    op.create_table(
        "macro_reference_series",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("series_code", sa.String(length=64), nullable=False),
        sa.Column("series_name", sa.String(length=160), nullable=False),
        sa.Column("geo_code", sa.String(length=40), nullable=False),
        sa.Column("period_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("source_label", sa.String(length=120), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_macro_reference_series"),
        sa.UniqueConstraint(
            "series_code",
            "geo_code",
            "period_date",
            name="uq_macro_reference_series_obs",
        ),
    )
    op.create_index(
        "ix_macro_series_code_date",
        "macro_reference_series",
        ["series_code", "period_date"],
    )

    op.create_table(
        "staged_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("source_channel", sa.String(length=40), nullable=False),
        sa.Column("province_code", sa.String(length=10), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("pos_name", sa.String(length=160), nullable=False),
        sa.Column("pos_channel", sa.String(length=40), nullable=False),
        sa.Column("ean_or_sku", sa.String(length=64), nullable=True),
        sa.Column("raw_description", sa.Text(), nullable=False),
        sa.Column("list_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("promo_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_staged_observations"),
    )
    op.create_index(
        "ix_staged_observations_observed_date",
        "staged_observations",
        ["observed_date"],
    )
    op.create_index("ix_staged_observations_status", "staged_observations", ["status"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("api_token_hash", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_admin_users"),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
    )
    op.create_index("ix_admin_users_username", "admin_users", ["username"])
    op.create_index("ix_admin_users_api_token_hash", "admin_users", ["api_token_hash"])

    # 6. Initial quarterly partitions for price_spans (2025 Q1 -> 2027 Q4)
    for yr in (2025, 2026, 2027):
        for qtr in (1, 2, 3, 4):
            ensure_quarterly_partition(bind, yr, qtr, fillfactor=85)

    # 7. Seed canonical data_sources, ARS monetary regime, 6 INDEC regions, 24 provinces, and periods
    data_sources_table = sa.table(
        "data_sources",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(
        data_sources_table,
        [
            {
                "code": "sepa",
                "name": "SEPA Precios Claros",
                "description": "Archivos diarios oficiales de supermercados (24 jurisdicciones)",
            },
            {
                "code": "web_scrape",
                "name": "Web Scraping Supermercados",
                "description": "Relevamiento automatizado de tiendas online (VTEX JSON-LD / JSONL)",
            },
            {
                "code": "hermes",
                "name": "Hermes Reborn Master Catalog",
                "description": "Catálogo maestro curado y clasificador taxonómico de dos niveles",
            },
            {
                "code": "offline_survey",
                "name": "Relevamiento de Campo Offline",
                "description": "Encuestas presenciales en comercios de cercanía, carnicerías, verdulerías y panaderías",
            },
            {
                "code": "offline_db_import",
                "name": "Importación de Bases Externas",
                "description": "Series históricas importadas desde SQLite, SQL o Parquet",
            },
            {
                "code": "offline_crowdsourced",
                "name": "Capturas Offline / Tickets",
                "description": "Extracciones semi-estructuradas de tickets, PDFs y listas de precios",
            },
        ],
    )

    monetary_table = sa.table(
        "monetary_regimes",
        sa.column("currency_code", sa.String),
        sa.column("name", sa.String),
        sa.column("valid_from", sa.Date),
        sa.column("valid_to", sa.Date),
        sa.column("factor_to_base_ars", sa.Float),
    )
    op.bulk_insert(
        monetary_table,
        [
            {
                "currency_code": "ARS",
                "name": "Peso Argentino (Decreto 2128/1991)",
                "valid_from": date(1992, 1, 1),
                "valid_to": None,
                "factor_to_base_ars": 1.0,
            }
        ],
    )

    analytics_defaults = AnalyticsSettings()
    if analytics_defaults.regional_weights_path.is_file():
        weights_cfg = load_regional_weights(analytics_defaults.regional_weights_path)
        for region_spec in weights_cfg.regions:
            reg_id = bind.execute(
                sa.text(
                    """
                    INSERT INTO statistical_regions (code, name, expenditure_weight)
                    VALUES (:code, :name, :weight)
                    RETURNING id
                    """
                ),
                {
                    "code": region_spec.code.upper(),
                    "name": region_spec.name,
                    "weight": region_spec.expenditure_weight,
                },
            ).scalar_one()

            for prov in region_spec.provinces:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO provinces (
                            code, name, region_id, population_share, expenditure_share_within_region
                        )
                        VALUES (:code, :name, :region_id, :pop_share, :exp_share)
                        """
                    ),
                    {
                        "code": prov.code.upper(),
                        "name": prov.name,
                        "region_id": reg_id,
                        "pop_share": prov.population_share,
                        "exp_share": prov.expenditure_share_within_region,
                    },
                )

    if analytics_defaults.government_periods_path.is_file():
        periods_cfg = load_government_periods(analytics_defaults.government_periods_path)
        for p in periods_cfg.periods:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO government_periods (
                        slug, name, administration_label, start_date, end_date, period_kind, notes
                    )
                    VALUES (:slug, :name, :label, :start_date, :end_date, :kind, :notes)
                    """
                ),
                {
                    "slug": p.slug,
                    "name": p.name,
                    "label": p.administration_label,
                    "start_date": p.start_date,
                    "end_date": p.end_date,
                    "kind": p.period_kind,
                    "notes": p.notes,
                },
            )


def downgrade() -> None:
    op.drop_table("admin_users")
    op.drop_table("staged_observations")
    op.drop_table("macro_reference_series")
    op.drop_table("government_periods")
    op.drop_table("price_index_series")
    op.drop_table("monthly_geo_variety_rollups")
    op.drop_table("daily_geo_variety_rollups")
    op.drop_table("basket_evaluation_items")
    op.drop_table("basket_evaluations")
    op.drop_table("basket_item_substitutes")
    op.drop_table("basket_items")
    op.drop_table("baskets")
    op.drop_table("chain_city_spans")
    op.drop_table("price_spans")
    op.drop_table("monetary_regimes")
    op.drop_table("promo_rules")
    op.drop_table("ingestion_batches")
    op.drop_table("catalog_edit_logs")
    op.drop_table("curation_rules")
    op.drop_table("unreviewed_articles")
    op.drop_table("source_sku_mappings")
    op.drop_table("data_sources")
    op.drop_table("article_tags")
    op.execute("DROP TRIGGER IF EXISTS trg_articles_search_vector ON articles")
    op.execute("DROP FUNCTION IF EXISTS argenprecios_articles_tsvector_trg()")
    op.drop_table("articles")
    op.drop_table("generic_varieties")
    op.drop_table("tags")
    op.drop_table("brands")
    op.drop_table("categories")
    op.drop_table("points_of_sale")
    op.drop_table("chains")
    op.drop_table("cities")
    op.drop_table("provinces")
    op.drop_table("statistical_regions")
    op.execute("DROP FUNCTION IF EXISTS argenprecios_unaccent(text)")
