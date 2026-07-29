"""Arrow adapter tests for parquet/csv/pyarrow sources and DuckDB pushdown.

These tests exercise the real :class:`~selayer.sources.adapters.arrow.ArrowDatasetAdapter`
and the registry-backed lifecycle for file-based and programmatic sources.
Fixtures create deterministic parquet/csv files in ``tmp_path`` so every test
is self-contained.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.adapters.arrow import ArrowDatasetAdapter
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import CsvConfig, ParquetConfig, PyArrowConfig
from selayer.sources.errors import SourceConnectionError
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.schema import (
    FieldSchema,
    ScalarType,
    TableSchema,
    compare_schemas,
    table_schema_from_arrow,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _orders_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("amount", ScalarType("int64"), False),
        )
    )


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles() -> RuntimeProfileResolver:
    return MappingProfileResolver({})


@pytest.fixture
def providers() -> ArrowProviderResolver:
    return MappingArrowProviderResolver({})


@pytest.fixture
def parquet_source(tmp_path: Path) -> ParsedSource:
    path = tmp_path / "orders.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "amount": pa.array([5, 15, 3], pa.int64()),
            }
        ),
        path,
    )
    return ParsedSource(
        name="orders",
        connector=ParquetConfig(str(path)),
        schema=_orders_schema(),
        grain=("id",),
    )


@pytest.fixture
def csv_source(tmp_path: Path) -> ParsedSource:
    path = tmp_path / "events.csv"
    path.write_text("id,amount\n1,10\n2,20\n3,30\n", encoding="utf-8")
    return ParsedSource(
        name="events",
        connector=CsvConfig(str(path)),
        schema=TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), False),
                FieldSchema("amount", ScalarType("int64"), False),
            )
        ),
        grain=("id",),
    )


# ---------------------------------------------------------------------------
# Projection and filter pushdown
# ---------------------------------------------------------------------------


def test_arrow_dataset_registration_preserves_projection_and_filter_pushdown(
    parquet_source: ParsedSource,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(parquet_source, profiles, providers)
    connection = duckdb.connect(":memory:")
    adapter.register(connection, "orders", handle)

    explain = "\n".join(
        row[1]
        for row in connection.execute(
            'EXPLAIN SELECT "id" FROM "orders" WHERE "amount" > 10'
        ).fetchall()
    )

    assert "ARROW_SCAN" in explain
    assert "id" in explain
    assert "amount" in explain
    assert connection.execute(
        'SELECT "id" FROM "orders" WHERE "amount" > 10'
    ).fetchall() == [(2,)]


# ---------------------------------------------------------------------------
# CSV declared schema
# ---------------------------------------------------------------------------


def test_csv_uses_declared_arrow_schema(
    csv_source: ParsedSource,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(csv_source, profiles, providers)
    observed = adapter.inspect_schema(handle)

    assert observed == csv_source.schema
    assert [field.name for field in observed.fields] == ["id", "amount"]

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)
    assert connection.execute('SELECT sum("amount") FROM "events"').fetchone() == (60,)


# ---------------------------------------------------------------------------
# Provider invocation on reload
# ---------------------------------------------------------------------------


def test_pyarrow_provider_is_invoked_again_on_reload() -> None:
    invoke_count = 0

    def provider() -> ArrowObject:
        nonlocal invoke_count
        invoke_count += 1
        schema = pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("value", pa.int64(), nullable=False),
            ]
        )
        return pa.Table.from_arrays(
            [pa.array([1], pa.int64()), pa.array([1], pa.int64())], schema=schema
        )

    providers = MappingArrowProviderResolver({"events": provider})
    layer = SemanticLayer(
        1,
        "test",
        "",
        "",
        {
            "events": DataSource(
                name="events",
                connector=PyArrowConfig("events"),
                schema=_events_schema(),
                grain=("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )
    connection = duckdb.connect(":memory:")
    engine = QueryEngine(layer, arrow_providers=providers)
    initial_invocations = invoke_count

    engine.reload_source("events")

    assert invoke_count == initial_invocations + 1
    engine.close()
    connection.close()


# ---------------------------------------------------------------------------
# Record batch reader is bound once per query
# ---------------------------------------------------------------------------


def test_record_batch_reader_is_bound_once_per_query() -> None:
    invoke_count = 0

    def reader_provider() -> ArrowObject:
        nonlocal invoke_count
        invoke_count += 1
        schema = pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("value", pa.int64(), nullable=False),
            ]
        )
        return pa.RecordBatchReader.from_batches(
            schema,
            [
                pa.RecordBatch.from_arrays(
                    [pa.array([1, 2], pa.int64()), pa.array([10, 20], pa.int64())],
                    names=["id", "value"],
                )
            ],
        )

    providers = MappingArrowProviderResolver({"events": reader_provider})
    layer = SemanticLayer(
        1,
        "test",
        "",
        "",
        {
            "events": DataSource(
                name="events",
                connector=PyArrowConfig("events"),
                schema=_events_schema(),
                grain=("id",),
            )
        },
        {},
        {
            "event_value": Fact.from_expression(
                "event_value", "events", "events.value", "integer"
            )
        },
        {"total_value": Measure("total_value", "event_value", "sum")},
        {"total": Metric.from_expression("total", "total_value", ("total_value",))},
        {},
    )
    engine = QueryEngine(layer, arrow_providers=providers)

    first = engine.query(["total"])
    first_count = invoke_count

    second = engine.query(["total"])

    assert first["total"].item() == 30
    assert second["total"].item() == 30
    # The reader provider is invoked once per query bind (plus the initial
    # creation during registry create).
    assert invoke_count == first_count + 1
    engine.close()


# ---------------------------------------------------------------------------
# Registry retains dataset until close
# ---------------------------------------------------------------------------


def test_registry_retains_dataset_until_close(
    parquet_source: ParsedSource,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(parquet_source, profiles, providers)
    connection = duckdb.connect(":memory:")
    adapter.register(connection, "orders", handle)

    # The dataset is queryable across multiple independent queries without
    # re-registration — it persists on the connection until explicitly closed.
    assert connection.execute('SELECT count(*) FROM "orders"').fetchone() == (3,)
    assert connection.execute('SELECT sum("amount") FROM "orders"').fetchone() == (23,)
    assert connection.execute('SELECT count(*) FROM "orders"').fetchone() == (3,)

    adapter.close(handle)


# ---------------------------------------------------------------------------
# Schema mismatch prevents register
# ---------------------------------------------------------------------------


def test_schema_mismatch_prevents_register() -> None:
    """An observed schema with an extra column is rejected before registration."""

    def drifted_provider() -> ArrowObject:
        return pa.table(
            {
                "id": pa.array([1], pa.int64()),
                "value": pa.array([1], pa.int64()),
                "extra": pa.array([1], pa.int64()),
            }
        )

    providers = MappingArrowProviderResolver({"events": drifted_provider})
    layer = SemanticLayer(
        1,
        "test",
        "",
        "",
        {
            "events": DataSource(
                name="events",
                connector=PyArrowConfig("events"),
                schema=_events_schema(),
                grain=("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )

    with pytest.raises(SourceConnectionError) as caught:
        QueryEngine(layer, arrow_providers=providers)

    assert caught.value.code == "source_initialization_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_compare_schemas_detects_extra_observed_field() -> None:
    declared = _events_schema()
    observed = table_schema_from_arrow(
        pa.schema(
            [
                ("id", pa.int64()),
                ("value", pa.int64()),
                ("extra", pa.int64()),
            ]
        )
    )
    mismatches = compare_schemas(declared, observed)
    assert any(mismatch.code == "extra_observed_field" for mismatch in mismatches)
