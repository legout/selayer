"""Physical-data integration test for the shop-floor example source generator.

These tests verify that :func:`generate_shopfloor_data` materialises every
deterministic connector input (CSV, SQLite, DuckDB, Parquet, Delta Lake) with
the agreed row counts.  No external services are required.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest  # noqa: F401  - required by the pytest runner for this module
from deltalake import DeltaTable

from examples.shopfloor.generate_data import generate_shopfloor_data


def test_generate_shopfloor_data_writes_all_connector_inputs(tmp_path: Path) -> None:
    paths = generate_shopfloor_data(tmp_path / "data")

    with paths.customer_orders.open(newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 2
    with sqlite3.connect(paths.production_orders_db) as connection:
        assert connection.execute(
            "select count(*) from production_orders"
        ).fetchone() == (3,)
    with duckdb.connect(str(paths.shopfloor_db), read_only=True) as connection:
        assert connection.execute(
            "select count(*) from serialized_drives"
        ).fetchone() == (3,)

    assert pq.read_table(paths.component_consumption).num_rows == 6
    assert pq.read_table(paths.component_lot_inspections).num_rows == 5
    assert pq.read_table(paths.operation_executions).num_rows == 7
    assert pq.read_table(paths.machine_telemetry).num_rows == 4
    assert DeltaTable(paths.eol_test_runs).to_pyarrow_table().num_rows == 3
