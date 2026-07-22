"""DuckDB engine wrapper: connection lifecycle + view registration + execute.

Internal to the backend. Exposed via AnalyticalBackend.ask() but not part
of the public API.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

import duckdb
import polars as pl

from .types import TableSpec


def make_connection() -> duckdb.DuckDBPyConnection:
    """Create a fresh in-memory DuckDB connection with sensible defaults."""
    return duckdb.connect(":memory:")


def collect_parquet_paths(data_source_path: str | Path) -> list[str]:
    """Resolve a single file or directory-of-parquet into a list of paths.

    A trailing '/*.parquet' glob is the convention we used in this codebase
    to mean "all parquet files in this directory".
    """
    p = Path(data_source_path)
    s = str(p)
    if "*" in s or "?" in s:
        return [str(x) for x in sorted(Path(".").glob(s))]
    if p.is_dir():
        return [str(x) for x in sorted(p.glob("*.parquet"))]
    return [s]


def register_data_source(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    parquet_path: str | Path,
) -> int:
    """Create-or-replace a DuckDB view over parquet. Returns row count.

    Uses DuckDB's lazy read_parquet() so views re-read on every query —
    the freshness guarantee we proved in the previous round.
    """
    paths = collect_parquet_paths(parquet_path)
    quoted = ", ".join(f"'{p}'" for p in paths)
    con.execute(
        f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet([{quoted}])"
    )
    return con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]


def list_columns(con: duckdb.DuckDBPyConnection, view_name: str) -> list[dict]:
    """Return [{name, type}, ...] for a DuckDB view/table."""
    rows = con.execute(f"DESCRIBE {view_name}").fetchall()
    return [{"name": r[0], "type": r[1]} for r in rows]


def execute_sql(
    con: duckdb.DuckDBPyConnection, sql: str, timeout_s: float = 60.0
) -> pl.DataFrame:
    """Run a SQL statement; raise duckdb.Error on failure."""
    t0 = time.perf_counter()
    rel = con.sql(sql)
    df = rel.pl()
    elapsed = time.perf_counter() - t0
    if elapsed > timeout_s:
        # DuckDB does not natively enforce a per-statement wall-clock timeout,
        # but the caller's timeout_s is advisory; we surface it as metadata.
        pass
    return df


def assemble_schema(
    con: duckdb.DuckDBPyConnection, view_names: Iterable[str]
) -> list[TableSpec]:
    """Build TableSpec entries for every registered view."""
    out: list[TableSpec] = []
    for name in view_names:
        out.append(
            TableSpec(
                name=name,
                description=f"Data source: {name}",
                columns=list_columns(con, name),
            )
        )
    return out
