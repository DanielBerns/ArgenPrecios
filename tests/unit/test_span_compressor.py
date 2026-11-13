"""Unit tests for temporal price_spans compression (`max_bridge_gap_days = 2`) and backfill merging."""

from __future__ import annotations

from datetime import date

from argenprecios.domain.models import IncomingPricePoint, OpenSpanState, SpanKey
from argenprecios.domain.span_compressor import (
    compute_incremental_transitions,
    rebuild_timeline_with_backfill,
)


def test_two_day_bridge_gap_vs_three_day_stockout_split() -> None:
    key = SpanKey(article_id=10, pos_id=20, source_id=1)
    open_span = OpenSpanState(
        article_id=10,
        pos_id=20,
        source_id=1,
        valid_from=date(2026, 4, 1),
        last_seen=date(2026, 4, 2),
        observed_days=2,
        list_price_cents=150_000,
        promo_price_cents=None,
        promo_rule_id=None,
        in_stock=True,
    )

    # Gap = 2 days (2026-04-02 -> 2026-04-04, missing April 3 only): MUST extend existing span!
    batch_bridge = compute_incremental_transitions(
        open_spans={key: open_span},
        incoming=[
            IncomingPricePoint(
                article_id=10,
                pos_id=20,
                source_id=1,
                observed_date=date(2026, 4, 4),
                list_price_cents=150_000,
                promo_price_cents=None,
                promo_rule_id=None,
                in_stock=True,
            )
        ],
        max_bridge_gap_days=2,
    )
    assert len(batch_bridge.extensions) == 1
    assert len(batch_bridge.inserts) == 0
    assert batch_bridge.extensions[0].new_last_seen == date(2026, 4, 4)
    assert batch_bridge.extensions[0].new_observed_days == 3

    # Gap = 3 days (2026-04-02 -> 2026-04-05, missing 2 full days): MUST open a new span!
    batch_split = compute_incremental_transitions(
        open_spans={key: open_span},
        incoming=[
            IncomingPricePoint(
                article_id=10,
                pos_id=20,
                source_id=1,
                observed_date=date(2026, 4, 5),
                list_price_cents=150_000,
                promo_price_cents=None,
                promo_rule_id=None,
                in_stock=True,
            )
        ],
        max_bridge_gap_days=2,
    )
    assert len(batch_split.extensions) == 0
    assert len(batch_split.inserts) == 1
    assert batch_split.inserts[0].valid_from == date(2026, 4, 5)
    assert batch_split.inserts[0].observed_days == 1


def test_price_or_promo_change_opens_new_span() -> None:
    key = SpanKey(article_id=10, pos_id=20, source_id=1)
    open_span = OpenSpanState(
        article_id=10,
        pos_id=20,
        source_id=1,
        valid_from=date(2026, 4, 1),
        last_seen=date(2026, 4, 5),
        observed_days=5,
        list_price_cents=150_000,
        promo_price_cents=None,
        promo_rule_id=None,
        in_stock=True,
    )
    batch = compute_incremental_transitions(
        open_spans={key: open_span},
        incoming=[
            IncomingPricePoint(
                article_id=10,
                pos_id=20,
                source_id=1,
                observed_date=date(2026, 4, 6),
                list_price_cents=165_000,
                promo_price_cents=140_000,
                promo_rule_id=7,
                in_stock=True,
            )
        ],
        max_bridge_gap_days=2,
    )
    assert len(batch.extensions) == 0
    assert len(batch.inserts) == 1
    assert batch.inserts[0].valid_from == date(2026, 4, 6)
    assert batch.inserts[0].list_price_cents == 165_000
    assert batch.inserts[0].promo_price_cents == 140_000


def test_out_of_order_historical_backfill_bridges_adjacent_spans() -> None:
    # Suppose Jan 1-2 and Jan 5-6 existed separately because Jan 3-4 was missing,
    # and we later backfill Jan 3 at the same price -> merges into a single continuous span!
    existing = [
        OpenSpanState(
            article_id=1,
            pos_id=1,
            source_id=1,
            valid_from=date(2024, 1, 1),
            last_seen=date(2024, 1, 2),
            observed_days=2,
            list_price_cents=80_000,
            promo_price_cents=None,
            promo_rule_id=None,
            in_stock=True,
        ),
        OpenSpanState(
            article_id=1,
            pos_id=1,
            source_id=1,
            valid_from=date(2024, 1, 5),
            last_seen=date(2024, 1, 6),
            observed_days=2,
            list_price_cents=80_000,
            promo_price_cents=None,
            promo_rule_id=None,
            in_stock=True,
        ),
    ]
    rebuilt = rebuild_timeline_with_backfill(
        existing_spans=existing,
        backfill_points=[
            IncomingPricePoint(
                article_id=1,
                pos_id=1,
                source_id=1,
                observed_date=date(2024, 1, 3),
                list_price_cents=80_000,
                promo_price_cents=None,
                promo_rule_id=None,
                in_stock=True,
            )
        ],
        max_bridge_gap_days=2,
    )
    assert len(rebuilt.deletes) == 2
    assert len(rebuilt.inserts) == 1
    merged = rebuilt.inserts[0]
    assert merged.valid_from == date(2024, 1, 1)
    assert merged.last_seen == date(2024, 1, 6)
    assert merged.observed_days == 5
