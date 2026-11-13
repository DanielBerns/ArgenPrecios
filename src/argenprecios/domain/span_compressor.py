"""Lossless temporal run-length compressor for price_spans (max_bridge_gap_days = 2)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta

from argenprecios.domain.models import (
    IncomingPricePoint,
    NewSpanRecord,
    OpenSpanState,
    SpanDeleteOp,
    SpanExtensionOp,
    SpanKey,
    SpanTransitionBatch,
)


def _same_pricing_state(
    a_list: int,
    a_promo: int | None,
    a_rule: int | None,
    a_stock: bool,
    b_list: int,
    b_promo: int | None,
    b_rule: int | None,
    b_stock: bool,
) -> bool:
    return a_list == b_list and a_promo == b_promo and a_rule == b_rule and a_stock == b_stock


def compute_incremental_transitions(
    open_spans: Mapping[SpanKey, OpenSpanState],
    incoming: Iterable[IncomingPricePoint],
    max_bridge_gap_days: int = 2,
) -> SpanTransitionBatch:
    """Compute HOT-friendly span extensions and new span inserts for chronological daily feeds.

    Bridging rule:
      - If incoming `(list_price_cents, promo_price_cents, promo_rule_id, in_stock)` matches
        the open span AND `0 < (observed_date - last_seen).days <= max_bridge_gap_days`,
        the open span is extended in place (`last_seen = observed_date`, `observed_days += 1`).
      - If `observed_date == last_seen` and pricing state matches, the observation is a no-op.
      - Any price change, promo change, stock flip, or gap `> max_bridge_gap_days` (e.g. a 3+ day
        stockout / missingness gap) closes the old span at its prior `last_seen` and inserts a
        new span starting at `observed_date`.
    """
    if max_bridge_gap_days < 0:
        raise ValueError(f"max_bridge_gap_days must be >= 0, got {max_bridge_gap_days}")

    grouped: dict[SpanKey, list[IncomingPricePoint]] = {}
    for pt in incoming:
        key = SpanKey(article_id=pt.article_id, pos_id=pt.pos_id, source_id=pt.source_id)
        grouped.setdefault(key, []).append(pt)

    extensions: dict[tuple[int, int, int, date], SpanExtensionOp] = {}
    inserts: dict[tuple[int, int, int, date], NewSpanRecord] = {}
    deletes: dict[tuple[int, int, int, date], SpanDeleteOp] = {}

    for key, raw_points in grouped.items():
        # Deduplicate same-date points within the batch (keep last seen in stream)
        by_date: dict[date, IncomingPricePoint] = {}
        for p in raw_points:
            by_date[p.observed_date] = p
        points = [by_date[d] for d in sorted(by_date)]

        current: OpenSpanState | None = open_spans.get(key)

        for pt in points:
            if current is None:
                rec = NewSpanRecord(
                    article_id=pt.article_id,
                    pos_id=pt.pos_id,
                    source_id=pt.source_id,
                    valid_from=pt.observed_date,
                    last_seen=pt.observed_date,
                    observed_days=1,
                    list_price_cents=pt.list_price_cents,
                    promo_price_cents=pt.promo_price_cents,
                    promo_rule_id=pt.promo_rule_id,
                    in_stock=pt.in_stock,
                )
                inserts[(pt.article_id, pt.pos_id, pt.source_id, pt.observed_date)] = rec
                current = OpenSpanState(
                    article_id=rec.article_id,
                    pos_id=rec.pos_id,
                    source_id=rec.source_id,
                    valid_from=rec.valid_from,
                    last_seen=rec.last_seen,
                    observed_days=rec.observed_days,
                    list_price_cents=rec.list_price_cents,
                    promo_price_cents=rec.promo_price_cents,
                    promo_rule_id=rec.promo_rule_id,
                    in_stock=rec.in_stock,
                )
                continue

            if pt.observed_date < current.last_seen:
                raise ValueError(
                    f"Out-of-order observation {pt.observed_date} < open span last_seen "
                    f"{current.last_seen} for {key}; use rebuild_timeline_with_backfill()."
                )

            same_state = _same_pricing_state(
                current.list_price_cents,
                current.promo_price_cents,
                current.promo_rule_id,
                current.in_stock,
                pt.list_price_cents,
                pt.promo_price_cents,
                pt.promo_rule_id,
                pt.in_stock,
            )

            if pt.observed_date == current.last_seen:
                if same_state:
                    continue
                # Same-day price revision
                span_pk = (
                    current.article_id,
                    current.pos_id,
                    current.source_id,
                    current.valid_from,
                )
                if current.valid_from == pt.observed_date:
                    if span_pk not in inserts:
                        deletes[span_pk] = SpanDeleteOp(*span_pk)
                    updated_rec = NewSpanRecord(
                        article_id=pt.article_id,
                        pos_id=pt.pos_id,
                        source_id=pt.source_id,
                        valid_from=pt.observed_date,
                        last_seen=pt.observed_date,
                        observed_days=1,
                        list_price_cents=pt.list_price_cents,
                        promo_price_cents=pt.promo_price_cents,
                        promo_rule_id=pt.promo_rule_id,
                        in_stock=pt.in_stock,
                    )
                    inserts[span_pk] = updated_rec
                    current = OpenSpanState(
                        article_id=updated_rec.article_id,
                        pos_id=updated_rec.pos_id,
                        source_id=updated_rec.source_id,
                        valid_from=updated_rec.valid_from,
                        last_seen=updated_rec.last_seen,
                        observed_days=updated_rec.observed_days,
                        list_price_cents=updated_rec.list_price_cents,
                        promo_price_cents=updated_rec.promo_price_cents,
                        promo_rule_id=updated_rec.promo_rule_id,
                        in_stock=updated_rec.in_stock,
                    )
                else:
                    # Roll back current span's last_seen by 1 day and start a new span today
                    prev_day = pt.observed_date - timedelta(days=1)
                    prev_obs_days = max(1, current.observed_days - 1)
                    if span_pk in inserts:
                        old_ins = inserts[span_pk]
                        inserts[span_pk] = NewSpanRecord(
                            article_id=old_ins.article_id,
                            pos_id=old_ins.pos_id,
                            source_id=old_ins.source_id,
                            valid_from=old_ins.valid_from,
                            last_seen=prev_day,
                            observed_days=prev_obs_days,
                            list_price_cents=old_ins.list_price_cents,
                            promo_price_cents=old_ins.promo_price_cents,
                            promo_rule_id=old_ins.promo_rule_id,
                            in_stock=old_ins.in_stock,
                        )
                    else:
                        extensions[span_pk] = SpanExtensionOp(
                            article_id=current.article_id,
                            pos_id=current.pos_id,
                            source_id=current.source_id,
                            valid_from=current.valid_from,
                            new_last_seen=prev_day,
                            new_observed_days=prev_obs_days,
                        )
                    new_pk = (pt.article_id, pt.pos_id, pt.source_id, pt.observed_date)
                    new_rec = NewSpanRecord(
                        article_id=pt.article_id,
                        pos_id=pt.pos_id,
                        source_id=pt.source_id,
                        valid_from=pt.observed_date,
                        last_seen=pt.observed_date,
                        observed_days=1,
                        list_price_cents=pt.list_price_cents,
                        promo_price_cents=pt.promo_price_cents,
                        promo_rule_id=pt.promo_rule_id,
                        in_stock=pt.in_stock,
                    )
                    inserts[new_pk] = new_rec
                    current = OpenSpanState(
                        article_id=new_rec.article_id,
                        pos_id=new_rec.pos_id,
                        source_id=new_rec.source_id,
                        valid_from=new_rec.valid_from,
                        last_seen=new_rec.last_seen,
                        observed_days=new_rec.observed_days,
                        list_price_cents=new_rec.list_price_cents,
                        promo_price_cents=new_rec.promo_price_cents,
                        promo_rule_id=new_rec.promo_rule_id,
                        in_stock=new_rec.in_stock,
                    )
                continue

            gap_days = (pt.observed_date - current.last_seen).days
            span_pk = (
                current.article_id,
                current.pos_id,
                current.source_id,
                current.valid_from,
            )

            if same_state and gap_days <= max_bridge_gap_days:
                new_obs_days = current.observed_days + 1
                if span_pk in inserts:
                    prior_ins = inserts[span_pk]
                    inserts[span_pk] = NewSpanRecord(
                        article_id=prior_ins.article_id,
                        pos_id=prior_ins.pos_id,
                        source_id=prior_ins.source_id,
                        valid_from=prior_ins.valid_from,
                        last_seen=pt.observed_date,
                        observed_days=new_obs_days,
                        list_price_cents=prior_ins.list_price_cents,
                        promo_price_cents=prior_ins.promo_price_cents,
                        promo_rule_id=prior_ins.promo_rule_id,
                        in_stock=prior_ins.in_stock,
                    )
                else:
                    extensions[span_pk] = SpanExtensionOp(
                        article_id=current.article_id,
                        pos_id=current.pos_id,
                        source_id=current.source_id,
                        valid_from=current.valid_from,
                        new_last_seen=pt.observed_date,
                        new_observed_days=new_obs_days,
                    )
                current = OpenSpanState(
                    article_id=current.article_id,
                    pos_id=current.pos_id,
                    source_id=current.source_id,
                    valid_from=current.valid_from,
                    last_seen=pt.observed_date,
                    observed_days=new_obs_days,
                    list_price_cents=current.list_price_cents,
                    promo_price_cents=current.promo_price_cents,
                    promo_rule_id=current.promo_rule_id,
                    in_stock=current.in_stock,
                )
            else:
                new_pk = (pt.article_id, pt.pos_id, pt.source_id, pt.observed_date)
                new_rec = NewSpanRecord(
                    article_id=pt.article_id,
                    pos_id=pt.pos_id,
                    source_id=pt.source_id,
                    valid_from=pt.observed_date,
                    last_seen=pt.observed_date,
                    observed_days=1,
                    list_price_cents=pt.list_price_cents,
                    promo_price_cents=pt.promo_price_cents,
                    promo_rule_id=pt.promo_rule_id,
                    in_stock=pt.in_stock,
                )
                inserts[new_pk] = new_rec
                current = OpenSpanState(
                    article_id=new_rec.article_id,
                    pos_id=new_rec.pos_id,
                    source_id=new_rec.source_id,
                    valid_from=new_rec.valid_from,
                    last_seen=new_rec.last_seen,
                    observed_days=new_rec.observed_days,
                    list_price_cents=new_rec.list_price_cents,
                    promo_price_cents=new_rec.promo_price_cents,
                    promo_rule_id=new_rec.promo_rule_id,
                    in_stock=new_rec.in_stock,
                )

    return SpanTransitionBatch(
        extensions=tuple(extensions.values()),
        inserts=tuple(inserts.values()),
        deletes=tuple(deletes.values()),
    )


def rebuild_timeline_with_backfill(
    existing_spans: Sequence[OpenSpanState],
    backfill_points: Iterable[IncomingPricePoint],
    max_bridge_gap_days: int = 2,
) -> SpanTransitionBatch:
    """Merge out-of-order historical backfill observations into existing spans losslessly."""
    by_key_existing: dict[SpanKey, list[OpenSpanState]] = {}
    for sp in existing_spans:
        k = SpanKey(article_id=sp.article_id, pos_id=sp.pos_id, source_id=sp.source_id)
        by_key_existing.setdefault(k, []).append(sp)

    by_key_incoming: dict[SpanKey, list[IncomingPricePoint]] = {}
    for pt in backfill_points:
        k = SpanKey(article_id=pt.article_id, pos_id=pt.pos_id, source_id=pt.source_id)
        by_key_incoming.setdefault(k, []).append(pt)

    all_deletes: list[SpanDeleteOp] = []
    all_inserts: list[NewSpanRecord] = []

    for key, incoming_list in by_key_incoming.items():
        prior_spans = sorted(by_key_existing.get(key, []), key=lambda s: s.valid_from)
        for sp in prior_spans:
            all_deletes.append(
                SpanDeleteOp(
                    article_id=sp.article_id,
                    pos_id=sp.pos_id,
                    source_id=sp.source_id,
                    valid_from=sp.valid_from,
                )
            )

        # Materialize daily observations for existing spans (bounding dates + interior days)
        daily_map: dict[date, IncomingPricePoint] = {}
        for sp in prior_spans:
            cursor = sp.valid_from
            while cursor <= sp.last_seen:
                daily_map[cursor] = IncomingPricePoint(
                    article_id=sp.article_id,
                    pos_id=sp.pos_id,
                    source_id=sp.source_id,
                    observed_date=cursor,
                    list_price_cents=sp.list_price_cents,
                    promo_price_cents=sp.promo_price_cents,
                    promo_rule_id=sp.promo_rule_id,
                    in_stock=sp.in_stock,
                )
                cursor += timedelta(days=1)

        # Overlay incoming backfill points
        for pt in incoming_list:
            daily_map[pt.observed_date] = pt

        ordered_points = [daily_map[d] for d in sorted(daily_map)]
        rebuilt = compute_incremental_transitions(
            open_spans={},
            incoming=ordered_points,
            max_bridge_gap_days=max_bridge_gap_days,
        )
        all_inserts.extend(rebuilt.inserts)

    return SpanTransitionBatch(
        extensions=(),
        inserts=tuple(all_inserts),
        deletes=tuple(all_deletes),
    )


__all__ = [
    "compute_incremental_transitions",
    "rebuild_timeline_with_backfill",
]
