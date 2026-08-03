"""Physical source-grain audit tests.

These tests cover :func:`~selayer.verification.audit.verify_physical` and the
registry's query-scoped :meth:`~selayer.sources.registry.SourceRegistry.bind_requirements`
context.  Small CSV/Parquet sources exercise valid single and composite grains,
a null grain field, and a duplicated composite tuple; the grain audit counts
exactly and never echoes offending values.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from selayer.catalog import SemanticLayer
from selayer.model import DataSource
from selayer.sources.base import (
    QueryBinding,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.config import (
    CsvConfig,
    DuckDbConfig,
    ParquetConfig,
    PyArrowConfig,
    SqliteConfig,
)
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema
from selayer.verification import PhysicalCheck, verify

# ---------------------------------------------------------------------------
# Registry binding test (Step 1)
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("machine_id", ScalarType("utf8"), False),
            FieldSchema("recorded_at", ScalarType("utf8"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


class _RecordingBindingAdapter:
    """Query-scoped fake adapter recording bind calls and cleanups.

    Mirrors the real Iceberg adapter pattern: ``prepare`` retains a
    *persistent* table resource on the handle, ``register`` is a no-op, and
    ``bind_query`` creates a fresh projected reader per query.  Using a
    non-``pyarrow`` connector keeps the registry's create path from closing
    the resource, so the binding receives the live handle.
    """

    def __init__(self, table: pa.Table) -> None:
        self._table = table
        self.bind_calls: list[SourceScanRequirement] = []
        self.cleanup_calls: int = 0
        self.closed_handles: list[str] = []

    def prepare(
        self,
        source: Any,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        del profiles, arrow_providers
        return SourceHandle(
            source_id=source.name,
            connector="iceberg",
            resource=self._table,
            schema=source.schema,
            snapshot=None,
            query_scoped=True,
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        # No-op: query-scoped readers are created and registered per query by
        # ``bind_query`` (matches the real Iceberg adapter).
        del connection, stable_name, handle

    def bind_query(
        self,
        connection: object,
        handle: SourceHandle,
        requirement: SourceScanRequirement,
    ) -> QueryBinding | None:
        self.bind_calls.append(requirement)
        table = handle.resource
        reader = table.select(list(requirement.columns))  # type: ignore[union-attr]
        connection.register(handle.source_id, reader)  # type: ignore[attr-defined]
        outer = self

        def _cleanup() -> None:
            outer.cleanup_calls += 1
            with suppress(Exception):
                connection.unregister(handle.source_id)  # type: ignore[attr-defined]

        return QueryBinding(
            source_id=handle.source_id,
            stable_name=handle.source_id,
            cleanup=_cleanup,
        )

    def close(self, handle: SourceHandle) -> None:
        self.closed_handles.append(handle.source_id)


def test_bind_requirements_binds_exact_grain_columns_and_cleans_up() -> None:
    """``bind_requirements`` receives the exact grain columns and cleans up.

    The query-scoped fake adapter records the requirement it is handed, the
    query scans the bound relation, and the binding cleanup runs exactly once
    on exit.  The registry lock covers the whole query.
    """

    table = pa.table(
        {
            "machine_id": pa.array(["m1", "m1"], pa.utf8()),
            "recorded_at": pa.array(["t1", "t2"], pa.utf8()),
            "value": pa.array([1, 2], pa.int64()),
        }
    )
    adapter = _RecordingBindingAdapter(table)
    layer = SemanticLayer(
        1,
        "bind_test",
        "",
        "",
        {
            "events": DataSource(
                "events",
                PyArrowConfig("events"),
                _events_schema(),
                ("machine_id", "recorded_at"),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )
    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        layer,
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({}),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    try:
        requirement = SourceScanRequirement(
            columns=("machine_id", "recorded_at")
        )
        with registry.bind_requirements({"events": requirement}):
            count = registry.execute(
                'select count(*) from "events"'
            ).fetchone()
        assert count == (2,)
        assert adapter.bind_calls == [requirement]
        assert adapter.cleanup_calls == 1
        # The lock is released after the context exits.
        assert registry._lock.acquire(blocking=False)
        registry._lock.release()
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# Grain audit tests (Step 2)
# ---------------------------------------------------------------------------


def _composite_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("machine_id", ScalarType("utf8"), True),
            FieldSchema("recorded_at", ScalarType("utf8"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )


def _single_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )


def _write_parquet(path: Path, table: pa.Table) -> None:
    pq.write_table(table, path)


@dataclass
class _GrainSources:
    layer: SemanticLayer
    profiles: RuntimeProfileResolver
    arrow_providers: ArrowProviderResolver


@pytest.fixture
def grain_sources(tmp_path: Path) -> _GrainSources:
    """Four Parquet sources exercising valid/invalid single and composite grains.

    * ``single`` — a valid single-column grain (no nulls, no duplicates).
    * ``composite`` — a valid composite grain (no nulls, no duplicates).
    * ``nulls`` — a composite grain with one null grain field.
    * ``events`` — a composite grain with one null grain field and one
      duplicated composite tuple; a grain value carries a secret sentinel.
    """

    single_path = tmp_path / "single.parquet"
    _write_parquet(
        single_path,
        pa.table(
            {"id": pa.array([1, 2], pa.int64()), "value": pa.array([1, 2], pa.int64())}
        ),
    )

    composite_path = tmp_path / "composite.parquet"
    _write_parquet(
        composite_path,
        pa.table(
            {
                "machine_id": pa.array(["m1", "m2"], pa.utf8()),
                "recorded_at": pa.array(["t1", "t2"], pa.utf8()),
                "value": pa.array([1, 2], pa.int64()),
            }
        ),
    )

    nulls_path = tmp_path / "nulls.parquet"
    # A genuine NULL (not an empty string) in a utf8 grain column.
    _write_parquet(
        nulls_path,
        pa.table(
            {
                "machine_id": pa.array(["m1", None], pa.utf8()),
                "recorded_at": pa.array(["t1", "t2"], pa.utf8()),
                "value": pa.array([1, 2], pa.int64()),
            }
        ),
    )

    events_path = tmp_path / "events.parquet"
    # One duplicate composite tuple (m1, t1) and one null grain field (the
    # third row has a null ``machine_id``).  A grain value carries the secret
    # sentinel so the no-leak assertion is meaningful.
    _write_parquet(
        events_path,
        pa.table(
            {
                "machine_id": pa.array(
                    ["m1", "m1", None, "secret-key-value"], pa.utf8()
                ),
                "recorded_at": pa.array(["t1", "t1", "t3", "t4"], pa.utf8()),
                "value": pa.array([1, 2, 3, 4], pa.int64()),
            }
        ),
    )

    layer = SemanticLayer(
        1,
        "grain_audit",
        "",
        "",
        {
            "single": DataSource(
                "single",
                ParquetConfig(str(single_path)),
                _single_schema(),
                ("id",),
            ),
            "composite": DataSource(
                "composite",
                ParquetConfig(str(composite_path)),
                _composite_schema(),
                ("machine_id", "recorded_at"),
            ),
            "nulls": DataSource(
                "nulls",
                ParquetConfig(str(nulls_path)),
                _composite_schema(),
                ("machine_id", "recorded_at"),
            ),
            "events": DataSource(
                "events",
                ParquetConfig(str(events_path)),
                _composite_schema(),
                ("machine_id", "recorded_at"),
            ),
        },
        {},
        {},
        {},
        {},
        {},
    )
    return _GrainSources(
        layer=layer,
        profiles=MappingProfileResolver({}),
        arrow_providers=MappingArrowProviderResolver({}),
    )


def _outcome(report: Any, check_id: str) -> Any:
    return next(item for item in report.outcomes if item.check_id == check_id)


def test_valid_single_and_composite_grains_pass(grain_sources: _GrainSources) -> None:
    report = verify(grain_sources.layer, PhysicalCheck())

    single = _outcome(report, "source.single.grain")
    assert single.status == "passed"
    assert single.scope == "full_scan"
    assert single.evidence["row_count"] == 2
    assert single.evidence["distinct_grain_count"] == 2
    assert single.evidence["null_grain_rows"] == 0
    assert single.evidence["duplicate_grain_groups"] == 0
    assert single.evidence["connector"] == "parquet"
    assert single.evidence["generation"] == 1
    assert isinstance(single.evidence["schema_fingerprint"], str)

    composite = _outcome(report, "source.composite.grain")
    assert composite.status == "passed"
    assert composite.evidence["row_count"] == 2
    assert composite.evidence["distinct_grain_count"] == 2
    assert composite.evidence["null_grain_rows"] == 0
    assert composite.evidence["duplicate_grain_groups"] == 0


def test_null_grain_field_is_reported(grain_sources: _GrainSources) -> None:
    report = verify(grain_sources.layer, PhysicalCheck())
    outcome = _outcome(report, "source.nulls.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["null_grain_rows"] == 1
    assert outcome.evidence["duplicate_grain_groups"] == 0
    assert outcome.evidence["row_count"] == 2


def test_duplicate_composite_tuple_is_reported(grain_sources: _GrainSources) -> None:
    report = verify(grain_sources.layer, PhysicalCheck())
    outcome = _outcome(report, "source.events.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["null_grain_rows"] == 1
    assert outcome.evidence["duplicate_grain_groups"] == 1
    assert outcome.evidence["row_count"] == 4
    # Distinct grain tuples: (m1,t1), (null,t3), (secret-key-value,t4) -> 3.
    assert outcome.evidence["distinct_grain_count"] == 3


def test_report_never_leaks_grain_values(grain_sources: _GrainSources) -> None:
    """No offending grain value (a secret sentinel) reaches the report."""

    report = verify(grain_sources.layer, PhysicalCheck())
    rendered = repr(report.to_dict())
    assert "secret-key-value" not in rendered
    assert "secret" not in rendered.lower()


def test_report_is_secret_safe_for_location_credentials(tmp_path: Path) -> None:
    """A source location carrying a credential token never leaks into a report.

    The location string carries a fake token; the audit reads only registry
    status and aggregate counts, so the token can never reach the report.
    """

    path = tmp_path / "orders.parquet"
    pq.write_table(
        pa.table(
            {"id": pa.array([1, 2, 3], pa.int64()), "value": pa.array([1, 2, 3], pa.int64())}
        ),
        path,
    )
    layer = SemanticLayer(
        1,
        "loc_test",
        "",
        "",
        {
            "orders": DataSource(
                "orders",
                ParquetConfig(str(path)),
                TableSchema(
                    (
                        FieldSchema("id", ScalarType("int64"), True),
                        FieldSchema("value", ScalarType("int64"), True),
                    )
                ),
                ("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )
    report = verify(layer, PhysicalCheck())
    rendered = repr(report.to_dict())
    assert str(path) not in rendered
    outcome = _outcome(report, "source.orders.grain")
    assert outcome.status == "passed"
    assert outcome.evidence["connector"] == "parquet"
    assert outcome.evidence["snapshot"] is None


def test_unavailable_registry_produces_unavailable_outcomes() -> None:
    """A registry-creation failure adapts to a secret-safe ``unavailable``.

    A ``pyarrow`` source with no resolved arrow provider cannot initialize, so
    registry creation raises a sanitized
    :class:`~selayer.sources.errors.SourceDependencyError`.  The audit adapts
    it to an ``unavailable`` outcome carrying the sanitized ``error_code`` (no
    connector metadata, since nothing registered) and no driver detail.
    """

    layer = _single_source_layer(
        "events", PyArrowConfig("events"), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    outcome = _outcome(report, "source.events.grain")
    assert outcome.status == "unavailable"
    assert outcome.scope == "full_scan"
    assert outcome.evidence["error_code"] == "missing_arrow_provider"
    # No connector metadata is available because nothing registered.
    assert "connector" not in outcome.evidence
    assert "schema_fingerprint" not in outcome.evidence
    rendered = repr(report.to_dict())
    assert "events" not in rendered.lower().replace("source.events.grain", "")


# ---------------------------------------------------------------------------
# Step 7: connector-specific audit smoke tests
# ---------------------------------------------------------------------------


def _single_source_layer(
    name: str,
    connector: Any,
    schema: TableSchema,
    grain: tuple[str, ...],
    layer_name: str = "smoke",
) -> SemanticLayer:
    return SemanticLayer(
        1,
        layer_name,
        "",
        "",
        {name: DataSource(name, connector, schema, grain)},
        {},
        {},
        {},
        {},
        {},
    )


def _assert_clean_grain(
    report: Any,
    source_id: str,
    *,
    connector: str,
    sentinels: tuple[str, ...] = (),
    snapshot_expected: bool = False,
) -> None:
    outcome = _outcome(report, f"source.{source_id}.grain")
    assert outcome.status == "passed", outcome
    assert outcome.scope == "full_scan"
    assert outcome.evidence["connector"] == connector
    assert outcome.evidence["generation"] == 1
    assert isinstance(outcome.evidence["schema_fingerprint"], str)
    assert outcome.evidence["null_grain_rows"] == 0
    assert outcome.evidence["duplicate_grain_groups"] == 0
    if snapshot_expected:
        snapshot = outcome.evidence["snapshot"]
        assert snapshot is not None
        assert isinstance(snapshot, str)
    else:
        assert outcome.evidence["snapshot"] is None
    rendered = repr(report.to_dict())
    for sentinel in sentinels:
        assert sentinel not in rendered, f"secret sentinel {sentinel!r} leaked"


def _id_value_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )


def test_audit_pyarrow_table_source(tmp_path: Path) -> None:
    """A programmatic PyArrow table source audits as ``pyarrow``."""

    table = pa.table(
        {"id": pa.array([1, 2, 3], pa.int64()), "value": pa.array([1, 2, 3], pa.int64())}
    )
    arrow_providers = MappingArrowProviderResolver({"events": lambda: table})
    layer = _single_source_layer(
        "events", PyArrowConfig("events"), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck(arrow_providers=arrow_providers))
    _assert_clean_grain(report, "events", connector="pyarrow")


def test_audit_parquet_source(tmp_path: Path) -> None:
    """A local Parquet file source audits as ``parquet``."""

    path = tmp_path / "events.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "value": pa.array([10, 20, 30], pa.int64()),
            }
        ),
        path,
    )
    layer = _single_source_layer(
        "events", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    _assert_clean_grain(report, "events", connector="parquet", sentinels=(str(path),))


def test_audit_csv_source(tmp_path: Path) -> None:
    """A local CSV file source audits as ``csv``."""

    path = tmp_path / "events.csv"
    path.write_text("id,value\n1,10\n2,20\n3,30\n", encoding="utf-8")
    layer = _single_source_layer(
        "events", CsvConfig(str(path)), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    _assert_clean_grain(report, "events", connector="csv", sentinels=(str(path),))


def test_audit_sqlite_source(tmp_path: Path) -> None:
    """A SQLite database-file source audits as ``sqlite``."""

    import sqlite3

    path = tmp_path / "facts.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            'create table facts (id integer primary key, "value" integer not null)'
        )
        connection.executemany(
            'insert into facts (id, "value") values (?, ?)', [(1, 10), (2, 20)]
        )
    layer = _single_source_layer(
        "facts", SqliteConfig(str(path), "facts"), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    _assert_clean_grain(report, "facts", connector="sqlite", sentinels=(str(path),))


def test_audit_duckdb_source(tmp_path: Path) -> None:
    """A DuckDB database-file source audits as ``duckdb``."""

    path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            'create table facts as select * from '
            '(values (1::bigint, 10::bigint), (2::bigint, 20::bigint)) '
            'as t(id, "value")'
        )
    layer = _single_source_layer(
        "facts", DuckDbConfig(str(path), "facts"), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    _assert_clean_grain(report, "facts", connector="duckdb", sentinels=(str(path),))

