"""Shared rendering logic for the four UI adapters.

Each UI adapter is intentionally thin; the heuristics live here so they
stay consistent across frameworks.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass
class ChartChoice:
    """A renderer picks one of these to display alongside the table."""

    kind: str  # "bar" | "line" | "scatter" | "area" | "none"
    x: str
    y: list[str]


def pick_chart(df: pl.DataFrame, override: str | None = None) -> ChartChoice:
    """Auto-detect an appropriate chart, or honour a manual override."""
    if df is None or df.height == 0:
        return ChartChoice("none", "", [])

    cols = df.columns
    if override and override != "auto":
        return ChartChoice(override, cols[0], cols[1:])

    numeric, datetime, other = [], [], []
    for c in cols:
        dt = df.schema[c]
        if dt.is_numeric():
            numeric.append(c)
        elif dt in (pl.Datetime, pl.Date):
            datetime.append(c)
        else:
            other.append(c)

    if datetime and numeric:
        return ChartChoice("line", datetime[0], numeric[:4])
    if other and numeric:
        return ChartChoice("bar", other[0], numeric[:4])
    if len(numeric) >= 2:
        return ChartChoice("scatter", numeric[0], numeric[1:2])
    return ChartChoice("none", cols[0], cols[1:])


def plotly_figure(df: pl.DataFrame, choice: ChartChoice):
    """Build a Plotly Express figure for the chosen chart kind. Lazy import."""
    import plotly.express as px

    if choice.kind == "none" or not choice.y:
        return None
    if choice.kind == "bar":
        return px.bar(df, x=choice.x, y=choice.y[0])
    if choice.kind == "line":
        return px.line(df, x=choice.x, y=choice.y)
    if choice.kind == "scatter":
        return px.scatter(df, x=choice.x, y=choice.y[0])
    if choice.kind == "area":
        return px.area(df, x=choice.x, y=choice.y)
    return None


def chart_kind_options() -> list[str]:
    return ["auto", "bar", "line", "scatter", "area", "none"]


def format_summary(result) -> str:
    """Standard one-line status string for the chat transcript."""
    if not getattr(result, "ok", False):
        return f"❌ {result.error or 'unknown error'}"
    return (
        f"✅ {result.rows} rows in {result.duration_ms:.0f} ms"
        f"  •  model: {result.model}  •  tokens: {result.tokens_used or '?'}"
    )


def format_sql_block(sql: str) -> str:
    """Wrap SQL in a markdown code fence."""
    return f"```sql\n{sql}\n```"


def history_cap() -> int:
    """Max entries kept in the per-session history list."""
    return 50


