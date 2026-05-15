"""gSage AI — Mermaid chart builders for E-goi email reports.

Generates ``xychart-beta`` strings ready to embed in Markdown output.

Notes
-----
Mermaid's xychart-beta has no native stacked-bar support. To approximate
a stacked look we plot **cumulative** series (highest layer first), so
each subsequent bar visually sits "under" the previous one when the
client renders overlapping bars. Charts are kept short and focused —
this is a debugging/glance aid, not a full BI surface.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger(__name__)


def _escape_label(value: object) -> str:
    s = str(value)
    return s.replace('"', "'")


def _quote_labels(labels: Iterable[object]) -> str:
    return ", ".join(f'"{_escape_label(x)}"' for x in labels)


def _format_series(values: Iterable[float]) -> str:
    return "[" + ", ".join(f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}" for v in values) + "]"


def build_bar_by_metric(
    overall: dict,
    *,
    title: str = "Campaign overall metrics",
    metrics: Optional[list[str]] = None,
) -> Optional[str]:
    """Single-bar chart comparing top-level metrics from the report.

    ``overall`` is expected to be the ``totals`` / overall section of an
    EmailReport (e.g. ``{"sent": 1000, "opens": 250, ...}``).
    """
    if not isinstance(overall, dict):
        return None
    keys = metrics or [
        "sent",
        "delivered",
        "opens",
        "unique_opens",
        "clicks",
        "unique_clicks",
        "bounces",
        "unsubscribed",
    ]
    pairs = [(k, overall.get(k)) for k in keys if isinstance(overall.get(k), (int, float))]
    if not pairs:
        return None
    labels = [p[0] for p in pairs]
    values = [float(p[1]) for p in pairs]
    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'    title "{_escape_label(title)}"\n'
        f"    x-axis [{_quote_labels(labels)}]\n"
        '    y-axis "count"\n'
        f"    bar {_format_series(values)}\n"
        "```"
    )


def build_bar_daily(
    rows: list[dict],
    *,
    date_key: str = "date",
    metrics: Optional[list[str]] = None,
    title: str = "Daily metrics",
    max_points: int = 30,
) -> Optional[str]:
    """Bar chart with one metric per day (single series).

    Designed for the ``by_date`` breakdown of an EmailReport. Picks the
    first available metric in ``metrics`` per row.
    """
    if not rows:
        return None
    metrics = metrics or ["opens", "clicks", "sent", "delivered"]
    points = rows[-max_points:]
    labels: list[str] = []
    values: list[float] = []
    chosen_metric: Optional[str] = None
    for row in points:
        if not chosen_metric:
            for m in metrics:
                if isinstance(row.get(m), (int, float)):
                    chosen_metric = m
                    break
        if not chosen_metric:
            continue
        labels.append(str(row.get(date_key) or ""))
        values.append(float(row.get(chosen_metric) or 0))
    if not values or not chosen_metric:
        return None
    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'    title "{_escape_label(title)} — {chosen_metric}"\n'
        f"    x-axis [{_quote_labels(labels)}]\n"
        f'    y-axis "{chosen_metric}"\n'
        f"    bar {_format_series(values)}\n"
        "```"
    )


def build_stacked_daily(
    rows: list[dict],
    *,
    date_key: str = "date",
    series: Optional[list[str]] = None,
    title: str = "Daily metrics (cumulative)",
    max_points: int = 30,
) -> Optional[str]:
    """Multi-series chart approximating a stacked-bar look.

    Mermaid xychart-beta does not support true stacking, so we render
    each series as **cumulative** values (sum of itself plus all series
    listed before it). When the rendering client paints later series
    over earlier ones, the visual effect is similar to a stacked bar.
    """
    if not rows:
        return None
    series = series or ["delivered", "opens", "clicks"]
    points = rows[-max_points:]
    available = [
        s for s in series
        if any(isinstance(r.get(s), (int, float)) for r in points)
    ]
    if not available:
        return None
    labels = [str(r.get(date_key) or "") for r in points]
    # Build cumulative series in reverse order so the largest plots last.
    chart_lines: list[str] = [
        "```mermaid",
        "xychart-beta",
        f'    title "{_escape_label(title)}"',
        f"    x-axis [{_quote_labels(labels)}]",
        '    y-axis "count"',
    ]
    cumulative: list[float] = [0.0] * len(points)
    for s in available:
        for i, r in enumerate(points):
            v = r.get(s)
            cumulative[i] += float(v) if isinstance(v, (int, float)) else 0.0
        chart_lines.append(f"    bar {_format_series(cumulative)}")
    chart_lines.append("```")
    return "\n".join(chart_lines)
