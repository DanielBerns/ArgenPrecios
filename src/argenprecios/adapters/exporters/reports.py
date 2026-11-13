"""Multi-format report writers (HTML, Markdown, CSV, Parquet) for national CPI & baskets."""

from __future__ import annotations

import csv
import html
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from argenprecios.config import resolve_user_path
from argenprecios.domain.models import (
    BasketGeoEvaluation,
    PeriodComparisonMetrics,
    PriceIndexPoint,
)

logger = logging.getLogger(__name__)


def _fmt_pct(val: float | None) -> str:
    return f"{val:+.2f}%" if val is not None else "N/D"


def _fmt_ars(cents: int | None) -> str:
    if cents is None:
        return "N/D"
    return f"${cents / 100.0:,.2f}"


def _parquet_cell(val: Any) -> Any:
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, (Mapping, list, tuple)):
        return json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)
    return val


def write_records_csv(records: Sequence[Mapping[str, Any]], target_path: Path) -> Path:
    """Export tabular dictionary records to UTF-8 CSV."""
    resolved = resolve_user_path(target_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        resolved.write_text("", encoding="utf-8")
        return resolved

    headers = list(records[0].keys())
    with resolved.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    return resolved


def write_records_parquet(records: Sequence[Mapping[str, Any]], target_path: Path) -> Path:
    """Export tabular dictionary records to Apache Parquet via pyarrow."""
    resolved = resolve_user_path(target_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    serializable = [{k: _parquet_cell(v) for k, v in rec.items()} for rec in records]
    table = pa.Table.from_pylist(serializable) if serializable else pa.table({})
    pq.write_table(table, resolved, compression="zstd")
    return resolved


def render_national_markdown_report(
    *,
    title: str,
    cpi_points: Sequence[PriceIndexPoint],
    period_comparisons: Sequence[PeriodComparisonMetrics],
    basket_evaluations: Sequence[BasketGeoEvaluation] = (),
) -> str:
    """Render a concise GitHub-Flavored Markdown national inflation & basket briefing."""
    lines: list[str] = [f"# {title}", ""]

    if period_comparisons:
        lines.extend(
            [
                "## Comparación Inter-Gubernamental de Inflación",
                "",
                "| Período / Administración | Meses Obs. | Acumulada | CMGR Mensual | Anualizada | Volatilidad (σ) | Brecha vs INDEC | Poder Adq. RIPTE | Brecha Promo |",
                "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for p in period_comparisons:
            gap_str = (
                f"{p.official_gap_percentage_points:+.2f} pp"
                if p.official_gap_percentage_points is not None
                else "N/D"
            )
            lines.append(
                f"| **{p.administration_label}** ({p.start_date} a {p.end_date}) "
                f"| {p.months_observed:.1f} "
                f"| {_fmt_pct(p.cumulative_inflation_pct)} "
                f"| {_fmt_pct(p.cmgr_pct)} "
                f"| {_fmt_pct(p.annualized_inflation_pct)} "
                f"| {f'{p.monthly_volatility_pct:.2f}%' if p.monthly_volatility_pct is not None else 'N/D'} "
                f"| {gap_str} "
                f"| {_fmt_pct(p.ripte_real_purchasing_power_change_pct)} "
                f"| {f'{p.avg_promo_gap_pct:.2f}%' if p.avg_promo_gap_pct is not None else 'N/D'} |"
            )
        lines.append("")

    if cpi_points:
        lines.extend(
            [
                "## Serie del Índice Nacional de Precios (Chained Jevons / Laspeyres)",
                "",
                "| Fecha | Nivel Geo | Código | Ponderación | Índice | Var. Período | Var. Interanual | Variedades Solapadas |",
                "| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: |",
            ]
        )
        for pt in cpi_points[-24:]:
            lines.append(
                f"| {pt.period_date.isoformat()} | {pt.geo_level.value} | {pt.geo_code} "
                f"| {pt.weighting.value} | {pt.index_value:.2f} "
                f"| {_fmt_pct(pt.period_pct_change)} | {_fmt_pct(pt.yoy_pct_change)} "
                f"| {pt.overlapping_varieties_count} |"
            )
        lines.append("")

    if basket_evaluations:
        lines.extend(
            [
                "## Valuación Multi-Nivel de Canastas",
                "",
                "| Fecha | Canasta | Alcance | Cobertura | Total Mínimo | Total Ref. (GeoMean) | Mejor Sucursal Completa |",
                "| :--- | :--- | :--- | ---: | ---: | ---: | ---: |",
            ]
        )
        for bev in basket_evaluations:
            lines.append(
                f"| {bev.evaluated_date.isoformat()} | {bev.basket_name} "
                f"| {bev.geo_name} ({bev.geo_code}) | {bev.coverage_ratio * 100:.1f}% "
                f"| {_fmt_ars(bev.min_total_cents)} | {_fmt_ars(bev.geo_mean_reference_total_cents)} "
                f"| {_fmt_ars(bev.best_single_store_cents)} |"
            )
        lines.append("")

    return "\n".join(lines)


def render_national_html_report(
    *,
    title: str,
    cpi_points: Sequence[PriceIndexPoint],
    period_comparisons: Sequence[PeriodComparisonMetrics],
    basket_evaluations: Sequence[BasketGeoEvaluation] = (),
) -> str:
    """Render a self-contained HTML report with inline SVG index trajectory and summary tables."""
    esc_title = html.escape(title)
    svg_chart = _build_inline_svg_chart(cpi_points)

    period_rows_html = "".join(
        f"<tr>"
        f"<td><strong>{html.escape(p.administration_label)}</strong><br><small>{p.start_date} → {p.end_date}</small></td>"
        f"<td>{p.months_observed:.1f}</td>"
        f"<td>{_fmt_pct(p.cumulative_inflation_pct)}</td>"
        f"<td>{_fmt_pct(p.cmgr_pct)}</td>"
        f"<td>{_fmt_pct(p.annualized_inflation_pct)}</td>"
        f"<td>{_fmt_pct(p.ripte_real_purchasing_power_change_pct)}</td>"
        f"</tr>"
        for p in period_comparisons
    )

    cpi_rows_html = "".join(
        f"<tr>"
        f"<td>{pt.period_date.isoformat()}</td>"
        f"<td>{html.escape(pt.geo_code)}</td>"
        f"<td>{html.escape(pt.weighting.value)}</td>"
        f"<td>{pt.index_value:.2f}</td>"
        f"<td>{_fmt_pct(pt.period_pct_change)}</td>"
        f"<td>{pt.overlapping_varieties_count}</td>"
        f"</tr>"
        for pt in cpi_points[-36:]
    )

    basket_rows_html = "".join(
        f"<tr>"
        f"<td>{bev.evaluated_date.isoformat()}</td>"
        f"<td>{html.escape(bev.basket_name)}</td>"
        f"<td>{html.escape(bev.geo_name)} ({html.escape(bev.geo_code)})</td>"
        f"<td>{bev.coverage_ratio * 100:.1f}%</td>"
        f"<td>{_fmt_ars(bev.min_total_cents)}</td>"
        f"<td>{_fmt_ars(bev.geo_mean_reference_total_cents)}</td>"
        f"<td>{_fmt_ars(bev.best_single_store_cents)}</td>"
        f"</tr>"
        for bev in basket_evaluations
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc_title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 1120px; padding: 0 1rem; color: #1e293b; background: #f8fafc; }}
    h1, h2 {{ color: #0f172a; }}
    .card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.25rem; margin-bottom: 1.5rem; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{ padding: 0.6rem 0.75rem; border-bottom: 1px solid #e2e8f0; text-align: left; }}
    th {{ background: #f1f5f9; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>{esc_title}</h1>
  <div class="card">
    <h2>Trayectoria del Índice Nacional (Base = 100)</h2>
    {svg_chart}
  </div>
  <div class="card">
    <h2>Comparación entre Períodos Presidenciales</h2>
    <table>
      <thead><tr><th>Administración</th><th>Meses</th><th>Acumulada</th><th>CMGR</th><th>Anualizada</th><th>Poder Adq. RIPTE</th></tr></thead>
      <tbody>{period_rows_html or '<tr><td colspan="6">Sin datos suficientes en el período</td></tr>'}</tbody>
    </table>
  </div>
  <div class="card">
    <h2>Serie Reciente del Índice</h2>
    <table>
      <thead><tr><th>Fecha</th><th>Geografía</th><th>Ponderación</th><th>Índice</th><th>Var. %</th><th>Variedades</th></tr></thead>
      <tbody>{cpi_rows_html or '<tr><td colspan="6">Sin observaciones</td></tr>'}</tbody>
    </table>
  </div>
  <div class="card">
    <h2>Valuación de Canastas</h2>
    <table>
      <thead><tr><th>Fecha</th><th>Canasta</th><th>Alcance</th><th>Cobertura</th><th>Mínimo</th><th>Referencia</th><th>Mejor Sucursal</th></tr></thead>
      <tbody>{basket_rows_html or '<tr><td colspan="7">Sin valuaciones registradas</td></tr>'}</tbody>
    </table>
  </div>
</body>
</html>
"""


def _build_inline_svg_chart(points: Sequence[PriceIndexPoint]) -> str:
    if len(points) < 2:
        return "<p>Se requieren al menos 2 períodos para graficar la curva del índice.</p>"
    vals = [p.index_value for p in points]
    v_min = min(vals)
    v_max = max(vals)
    span = max(v_max - v_min, 1e-6)
    width = 960
    height = 220
    pad = 24
    coords: list[str] = []
    for idx, val in enumerate(vals):
        x = pad + (idx / (len(vals) - 1)) * (width - 2 * pad)
        y = (height - pad) - ((val - v_min) / span) * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc" rx="6"/>'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="3" points="{polyline}"/>'
        f"</svg>"
    )


def export_report_bundle(
    *,
    output_dir: Path,
    stem: str,
    title: str,
    formats: Sequence[str],
    cpi_points: Sequence[PriceIndexPoint],
    period_comparisons: Sequence[PeriodComparisonMetrics],
    basket_evaluations: Sequence[BasketGeoEvaluation] = (),
) -> list[Path]:
    """Write requested report formats (`html`, `md`, `csv`, `parquet`) into `output_dir`."""
    resolved_dir = resolve_user_path(output_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    cpi_dicts = [
        {
            "period_date": p.period_date.isoformat(),
            "geo_level": p.geo_level.value,
            "geo_code": p.geo_code,
            "weighting": p.weighting.value,
            "index_value": p.index_value,
            "period_pct_change": p.period_pct_change,
            "yoy_pct_change": p.yoy_pct_change,
            "overlapping_varieties_count": p.overlapping_varieties_count,
        }
        for p in cpi_points
    ]
    period_dicts = [asdict(pc) for pc in period_comparisons]

    for fmt in formats:
        norm = fmt.strip().lower()
        if norm == "md":
            md_path = resolved_dir / f"{stem}.md"
            md_path.write_text(
                render_national_markdown_report(
                    title=title,
                    cpi_points=cpi_points,
                    period_comparisons=period_comparisons,
                    basket_evaluations=basket_evaluations,
                ),
                encoding="utf-8",
            )
            written.append(md_path)
        elif norm == "html":
            html_path = resolved_dir / f"{stem}.html"
            html_path.write_text(
                render_national_html_report(
                    title=title,
                    cpi_points=cpi_points,
                    period_comparisons=period_comparisons,
                    basket_evaluations=basket_evaluations,
                ),
                encoding="utf-8",
            )
            written.append(html_path)
        elif norm == "csv":
            written.append(write_records_csv(cpi_dicts, resolved_dir / f"{stem}_cpi.csv"))
            if period_dicts:
                written.append(
                    write_records_csv(period_dicts, resolved_dir / f"{stem}_periods.csv")
                )
        elif norm == "parquet":
            written.append(write_records_parquet(cpi_dicts, resolved_dir / f"{stem}_cpi.parquet"))
            if period_dicts:
                written.append(
                    write_records_parquet(period_dicts, resolved_dir / f"{stem}_periods.parquet")
                )

    return written


__all__ = [
    "export_report_bundle",
    "render_national_html_report",
    "render_national_markdown_report",
    "write_records_csv",
    "write_records_parquet",
]
