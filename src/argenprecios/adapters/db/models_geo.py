"""SQLAlchemy 2.0 geographic and retail point-of-sale models (6 INDEC Regions, 24 Provinces)."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from argenprecios.adapters.db.base import WarehouseBase


class StatisticalRegionModel(WarehouseBase):
    """INDEC statistical macro-region (GBA, PAMPEANA, NOA, NEA, CUYO, PATAGONIA)."""

    __tablename__ = "statistical_regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    expenditure_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0 / 6.0)

    provinces: Mapped[list[ProvinceModel]] = relationship(
        "ProvinceModel",
        back_populates="region",
        order_by="ProvinceModel.code",
    )


class ProvinceModel(WarehouseBase):
    """ISO 3166-2:AR jurisdiction (all 24 Argentine provinces + CABA)."""

    __tablename__ = "provinces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    region_id: Mapped[int] = mapped_column(
        ForeignKey("statistical_regions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    population_share: Mapped[float] = mapped_column(Float, nullable=False, default=1.0 / 24.0)
    expenditure_share_within_region: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )

    region: Mapped[StatisticalRegionModel] = relationship(
        "StatisticalRegionModel",
        back_populates="provinces",
    )
    cities: Mapped[list[CityModel]] = relationship(
        "CityModel",
        back_populates="province",
    )


class CityModel(WarehouseBase):
    """Municipality or locality within an Argentine province."""

    __tablename__ = "cities"
    __table_args__ = (UniqueConstraint("province_id", "slug", name="uq_cities_province_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    province_id: Mapped[int] = mapped_column(
        ForeignKey("provinces.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    province: Mapped[ProvinceModel] = relationship("ProvinceModel", back_populates="cities")
    points_of_sale: Mapped[list[PointOfSaleModel]] = relationship(
        "PointOfSaleModel",
        back_populates="city",
    )


class ChainModel(WarehouseBase):
    """Retail banner, supermarket chain, or independent merchant group."""

    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    holding_group: Mapped[str | None] = mapped_column(String(120), nullable=True)
    default_pos_channel: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="physical_supermarket",
    )

    points_of_sale: Mapped[list[PointOfSaleModel]] = relationship(
        "PointOfSaleModel",
        back_populates="chain",
    )


class PointOfSaleModel(WarehouseBase):
    """Physical branch, online storefront, or offline neighborhood store."""

    __tablename__ = "points_of_sale"
    __table_args__ = (
        Index("ix_pos_province_channel", "province_id", "pos_channel"),
        Index("ix_pos_chain_city", "chain_id", "city_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    sepa_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    comercio_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bandera_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sucursal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    chain_id: Mapped[int] = mapped_column(
        ForeignKey("chains.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    province_id: Mapped[int] = mapped_column(
        ForeignKey("provinces.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    pos_channel: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="physical_supermarket",
        index=True,
    )
    branch_name: Mapped[str] = mapped_column(String(160), nullable=False)
    address: Mapped[str | None] = mapped_column(String(220), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    chain: Mapped[ChainModel] = relationship("ChainModel", back_populates="points_of_sale")
    city: Mapped[CityModel] = relationship("CityModel", back_populates="points_of_sale")
    province: Mapped[ProvinceModel] = relationship("ProvinceModel")


__all__ = [
    "ChainModel",
    "CityModel",
    "PointOfSaleModel",
    "ProvinceModel",
    "StatisticalRegionModel",
]
