"""Hexagonal port interfaces (Protocols) decoupling domain use cases from infrastructure adapters."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from argenprecios.domain.enums import SourceChannel
from argenprecios.domain.models import (
    IncomingPricePoint,
    OpenSpanState,
    RawPriceObservation,
    SearchFilter,
    SearchPageResult,
    SpanKey,
    SpanTransitionBatch,
)


class ObservationSourcePort(Protocol):
    """Port implemented by SEPA, Web Scrape, and Offline source readers."""

    @property
    def source_channel(self) -> SourceChannel: ...

    def iter_observations(self, source_path: Path) -> Iterator[RawPriceObservation]: ...


class SpanRepositoryPort(Protocol):
    """Port for reading open spans and applying HOT-friendly span transitions in batches."""

    def fetch_latest_spans(self, keys: Iterable[SpanKey]) -> dict[SpanKey, OpenSpanState]: ...

    def apply_transition_batch(
        self,
        batch: SpanTransitionBatch,
        chunk_size: int = 10_000,
    ) -> tuple[int, int]: ...

    def upsert_historical_points(
        self,
        points: Iterable[IncomingPricePoint],
        max_bridge_gap_days: int = 2,
    ) -> tuple[int, int]: ...


class CatalogSearchPort(Protocol):
    """Port for hybrid Spanish full-text + trigram + geographic coverage article search."""

    def search_articles(self, filters: SearchFilter) -> SearchPageResult: ...


__all__ = [
    "CatalogSearchPort",
    "ObservationSourcePort",
    "SpanRepositoryPort",
]
