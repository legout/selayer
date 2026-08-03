"""Physical source-grain audit tests.

These tests cover :func:`~selayer.verification.audit.verify_physical` and the
registry's query-scoped :meth:`~selayer.sources.registry.SourceRegistry.bind_requirements`
context.  Small CSV/Parquet sources exercise valid single and composite grains,
a null grain field, and a duplicated composite tuple; the grain audit counts
exactly and never echoes offending values.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Relationship
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
    assert single.evidence["duplicate_row_count"] == 0
    assert single.evidence["connector"] == "parquet"
    assert single.evidence["generation"] == 1
    assert isinstance(single.evidence["schema_fingerprint"], str)

    composite = _outcome(report, "source.composite.grain")
    assert composite.status == "passed"
    assert composite.evidence["row_count"] == 2
    assert composite.evidence["distinct_grain_count"] == 2
    assert composite.evidence["duplicate_row_count"] == 0
    assert composite.evidence["null_grain_rows"] == 0
    assert composite.evidence["duplicate_grain_groups"] == 0


def test_null_grain_field_is_reported(grain_sources: _GrainSources) -> None:
    report = verify(grain_sources.layer, PhysicalCheck())
    outcome = _outcome(report, "source.nulls.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["null_grain_rows"] == 1
    assert outcome.evidence["duplicate_grain_groups"] == 0
    assert outcome.evidence["duplicate_row_count"] == 0
    assert outcome.evidence["row_count"] == 2


def test_duplicate_composite_tuple_is_reported(grain_sources: _GrainSources) -> None:
    report = verify(grain_sources.layer, PhysicalCheck())
    outcome = _outcome(report, "source.events.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["null_grain_rows"] == 1
    assert outcome.evidence["duplicate_grain_groups"] == 1
    # One row beyond the distinct grain tuples is a duplicate.
    assert outcome.evidence["duplicate_row_count"] == 1
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
    # An unavailable source makes the report incomplete and non-passing, with
    # a single stable diagnostic flagging the gap.
    assert report.complete is False
    assert report.passed is False
    unavailable_diags = [
        diag for diag in report.diagnostics if diag.code == "source.audit.unavailable"
    ]
    assert unavailable_diags
    assert unavailable_diags[0].severity == "error"
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


# ---------------------------------------------------------------------------
# Review findings: raw scan error sanitization, completeness/diagnostic,
# duplicate_row_count evidence, and metadata binding-context timing.
# ---------------------------------------------------------------------------


def test_raw_scan_error_is_sanitized_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw DuckDB scan failure becomes a secret-safe ``unavailable`` outcome.

    The registry's ``execute`` (the bound scan) raises a raw ``duckdb.Error``
    carrying a fake credential/location sentinel.  The audit adapts it to an
    ``unavailable`` outcome with the stable ``scan_failed`` code, the report is
    incomplete, and no raw driver text escapes.
    """

    path = tmp_path / "events.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "value": pa.array([1, 2, 3], pa.int64()),
            }
        ),
        path,
    )
    layer = _single_source_layer(
        "events", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )

    secret = "s3://user:RAWSECRETtoken@host/private/location"

    def _boom(
        self: SourceRegistry, sql: str, parameters: tuple[object, ...] = ()
    ) -> Any:
        del self, sql, parameters
        raise duckdb.Error(f"scan failure reading {secret}")

    monkeypatch.setattr(SourceRegistry, "execute", _boom)

    report = verify(layer, PhysicalCheck())
    outcome = _outcome(report, "source.events.grain")
    assert outcome.status == "unavailable"
    assert outcome.evidence["error_code"] == "scan_failed"
    # The bound-scan status was read (under the lock) before the scan failed,
    # so connector metadata is present.
    assert outcome.evidence["connector"] == "parquet"
    assert report.complete is False
    assert report.passed is False
    rendered = repr(report.to_dict())
    assert "RAWSECRETtoken" not in rendered
    assert secret not in rendered
    assert "scan failure" not in rendered


def test_unavailable_outcome_marks_report_incomplete(tmp_path: Path) -> None:
    """Any required unavailable source makes the report incomplete + non-passing.

    A clean layer (all sources pass) yields a complete, passing report with no
    ``source.audit.unavailable`` diagnostic; once a source is unavailable the
    report flips to ``complete=False`` with the stable diagnostic and
    ``passed`` is ``False``.
    """

    path = tmp_path / "events.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "value": pa.array([1, 2, 3], pa.int64()),
            }
        ),
        path,
    )
    layer = _single_source_layer(
        "events", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )
    clean_report = verify(layer, PhysicalCheck())
    assert clean_report.complete is True
    assert clean_report.passed is True
    assert not [
        diag for diag in clean_report.diagnostics
        if diag.code == "source.audit.unavailable"
    ]

    unavailable_layer = _single_source_layer(
        "events", PyArrowConfig("events"), _id_value_schema(), ("id",)
    )
    unavailable_report = verify(unavailable_layer, PhysicalCheck())
    assert unavailable_report.complete is False
    assert unavailable_report.passed is False
    unavailable_diags = [
        diag
        for diag in unavailable_report.diagnostics
        if diag.code == "source.audit.unavailable"
    ]
    assert unavailable_diags
    assert unavailable_diags[0].severity == "error"
    # The diagnostic message is a constant (no source id / driver detail).
    assert "events" not in unavailable_diags[0].message


def test_duplicate_row_count_evidence(tmp_path: Path) -> None:
    """``duplicate_row_count`` counts the excess rows beyond distinct grains.

    Three identical tuples plus one distinct one: row_count=4, distinct=2,
    duplicate_row_count=2 (the two surplus copies of the repeated tuple),
    duplicate_grain_groups=1, and the outcome fails.
    """

    path = tmp_path / "dup.parquet"
    _write_parquet(
        path,
        pa.table(
            {
                "id": pa.array([1, 1, 1, 2], pa.int64()),
                "value": pa.array([10, 10, 10, 20], pa.int64()),
            }
        ),
    )
    layer = _single_source_layer(
        "dup", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    outcome = _outcome(report, "source.dup.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["row_count"] == 4
    assert outcome.evidence["distinct_grain_count"] == 2
    assert outcome.evidence["duplicate_row_count"] == 2
    assert outcome.evidence["duplicate_grain_groups"] == 1
    assert outcome.evidence["null_grain_rows"] == 0


def test_single_column_nullable_grain_is_null_safe(tmp_path: Path) -> None:
    """Single-column grains count NULL as one distinct grain tuple.

    ``count(distinct "col")`` drops NULL, so a nullable single-column grain
    would undercount distinct grains and overcount duplicate rows.  The audit
    uses a null-safe formulation consistent with the composite ``struct_pack``
    path: NULL is one distinct grain tuple, ``{1, NULL}`` has no duplicate
    group, and a repeated NULL is one duplicate group.
    """

    # {1, NULL, 2}: three distinct grains (1, NULL, 2), no duplicates.
    path = tmp_path / "nullable.parquet"
    _write_parquet(
        path,
        pa.table(
            {
                "id": pa.array([1, None, 2], pa.int64()),
                "value": pa.array([1, 2, 3], pa.int64()),
            }
        ),
    )
    layer = _single_source_layer(
        "nullable", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )
    report = verify(layer, PhysicalCheck())
    outcome = _outcome(report, "source.nullable.grain")
    assert outcome.status == "failed"
    assert outcome.evidence["row_count"] == 3
    assert outcome.evidence["distinct_grain_count"] == 3
    assert outcome.evidence["null_grain_rows"] == 1
    assert outcome.evidence["duplicate_grain_groups"] == 0
    assert outcome.evidence["duplicate_row_count"] == 0

    # {1, NULL, NULL}: two distinct grains (1, NULL), one duplicate group
    # (the repeated NULL), one surplus duplicate row.
    dup_path = tmp_path / "nullable_dup.parquet"
    _write_parquet(
        dup_path,
        pa.table(
            {
                "id": pa.array([1, None, None], pa.int64()),
                "value": pa.array([1, 2, 3], pa.int64()),
            }
        ),
    )
    dup_layer = _single_source_layer(
        "nullable_dup", ParquetConfig(str(dup_path)), _id_value_schema(), ("id",)
    )
    dup_report = verify(dup_layer, PhysicalCheck())
    dup_outcome = _outcome(dup_report, "source.nullable_dup.grain")
    assert dup_outcome.status == "failed"
    assert dup_outcome.evidence["row_count"] == 3
    assert dup_outcome.evidence["distinct_grain_count"] == 2
    assert dup_outcome.evidence["null_grain_rows"] == 2
    assert dup_outcome.evidence["duplicate_grain_groups"] == 1
    assert dup_outcome.evidence["duplicate_row_count"] == 1


def test_audit_reads_status_inside_binding_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connector status is read while the per-source binding context is active.

    Moving the ``status`` read out of the binding context (so a concurrent
    reload could swap the handle between the status read and the scan) would
    flip ``status_while_binding`` to ``[False]`` and fail this test.
    """

    path = tmp_path / "events.parquet"
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "value": pa.array([1, 2, 3], pa.int64()),
            }
        ),
        path,
    )
    layer = _single_source_layer(
        "events", ParquetConfig(str(path)), _id_value_schema(), ("id",)
    )

    binding_active: list[bool] = []
    status_while_binding: list[bool] = []
    original_status = SourceRegistry.status

    def _recording_status(self: SourceRegistry, source_id: str) -> Any:
        status_while_binding.append(bool(binding_active) and binding_active[-1])
        return original_status(self, source_id)

    monkeypatch.setattr(SourceRegistry, "status", _recording_status)

    original_bind = SourceRegistry.bind_requirements

    @contextmanager
    def _tracking_bind(self: SourceRegistry, requirements: Any):
        binding_active.append(True)
        try:
            with original_bind(self, requirements):
                yield
        finally:
            binding_active.pop()

    monkeypatch.setattr(SourceRegistry, "bind_requirements", _tracking_bind)

    report = verify(layer, PhysicalCheck())
    outcome = _outcome(report, "source.events.grain")
    assert outcome.status == "passed"
    assert status_while_binding  # status was read
    assert all(status_while_binding)  # always while the binding is active


# ---------------------------------------------------------------------------
# Relationship cardinality audit tests (Task 13)
# ---------------------------------------------------------------------------


def _relationship_outcome(report: Any, name: str) -> Any:
    return next(
        item
        for item in report.outcomes
        if item.check_id == f"relationship.{name}.cardinality"
    )


def _relationship_layer(
    name: str,
    sources: dict[str, DataSource],
    relationships: dict[str, Relationship],
) -> SemanticLayer:
    return SemanticLayer(
        1, name, "", "", sources, {}, {}, {}, {}, relationships
    )


def _orders_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("order_id", ScalarType("utf8"), True),
            FieldSchema("amount", ScalarType("int64"), True),
        )
    )


def _items_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("item_id", ScalarType("utf8"), True),
            FieldSchema("order_id", ScalarType("utf8"), True),
        )
    )


def _order_id_schema() -> TableSchema:
    return TableSchema((FieldSchema("order_id", ScalarType("utf8"), True),))


def _key_schema() -> TableSchema:
    return TableSchema((FieldSchema("key", ScalarType("int64"), True),))


@dataclass
class _DirectedSources:
    orders: DataSource
    items: DataSource


@pytest.fixture
def directed_sources(tmp_path: Path) -> _DirectedSources:
    """A one(order_id)-to-many(order_id) pair with a secret order key.

    ``orders`` carries a secret sentinel order id so the no-leak assertion is
    meaningful; ``items`` has a nullable many-side key (one null order_id) and
    one zero-child order.
    """

    orders_path = tmp_path / "orders.parquet"
    _write_parquet(
        orders_path,
        pa.table(
            {
                "order_id": pa.array(
                    ["o1", "o2", "secret-order-key"], pa.utf8()
                ),
                "amount": pa.array([10, 20, 30], pa.int64()),
            }
        ),
    )
    items_path = tmp_path / "items.parquet"
    _write_parquet(
        items_path,
        pa.table(
            {
                "item_id": pa.array(["i1", "i2", "i3", "i4"], pa.utf8()),
                "order_id": pa.array(["o1", "o1", "o2", None], pa.utf8()),
            }
        ),
    )
    return _DirectedSources(
        orders=DataSource(
            "orders",
            ParquetConfig(str(orders_path)),
            _orders_schema(),
            ("order_id",),
        ),
        items=DataSource(
            "items",
            ParquetConfig(str(items_path)),
            _items_schema(),
            ("item_id",),
        ),
    )


_DIRECTED_EVIDENCE = {
    "one_side_null_rows": 0,
    "one_side_duplicate_groups": 0,
    "many_side_null_rows": 1,
    "orphan_non_null_rows": 0,
    "zero_child_one_side_rows": 1,
    "maximum_child_multiplicity": 2,
}


def test_one_to_many_passes_with_nullable_many_side_and_zero_child(
    directed_sources: _DirectedSources,
) -> None:
    layer = _relationship_layer(
        "rel",
        {"orders": directed_sources.orders, "items": directed_sources.items},
        {
            "orders_items": Relationship(
                "orders_items",
                "orders",
                "items",
                "one_to_many",
                "order_id",
                "order_id",
            )
        },
    )
    report = verify(layer, PhysicalCheck())
    outcome = _relationship_outcome(report, "orders_items")
    assert outcome.status == "passed"
    assert outcome.scope == "full_scan"
    assert outcome.evidence == _DIRECTED_EVIDENCE
    # Nullable many-side keys and zero-child parents stay informational: no
    # diagnostics and the outcome still passes.
    assert outcome.diagnostics == ()
    # The report also carries the source-grain outcomes alongside the
    # relationship outcome.
    assert any(
        item.check_id == "source.orders.grain" for item in report.outcomes
    )
    rendered = repr(report.to_dict())
    assert "secret" not in rendered.lower()


def test_many_to_one_uses_target_as_one_side(
    directed_sources: _DirectedSources,
) -> None:
    layer = _relationship_layer(
        "rel",
        {"orders": directed_sources.orders, "items": directed_sources.items},
        {
            "items_orders": Relationship(
                "items_orders",
                "items",
                "orders",
                "many_to_one",
                "order_id",
                "order_id",
            )
        },
    )
    report = verify(layer, PhysicalCheck())
    outcome = _relationship_outcome(report, "items_orders")
    assert outcome.status == "passed"
    # many_to_one flips the sides: the declared target (orders) is the one
    # side, so the evidence mirrors the one_to_many case exactly.
    assert outcome.evidence == _DIRECTED_EVIDENCE


def test_one_to_many_fails_on_duplicate_one_side_keys(
    tmp_path: Path,
) -> None:
    orders_path = tmp_path / "orders.parquet"
    _write_parquet(
        orders_path,
        pa.table(
            {
                "row_id": pa.array([1, 2, 3], pa.int64()),
                "order_id": pa.array(
                    ["dup-secret-key", "dup-secret-key", "o2"], pa.utf8()
                ),
            }
        ),
    )
    items_path = tmp_path / "items.parquet"
    _write_parquet(
        items_path,
        pa.table(
            {
                "item_id": pa.array(["i1", "i2"], pa.utf8()),
                "order_id": pa.array(["dup-secret-key", "o2"], pa.utf8()),
            }
        ),
    )
    layer = _relationship_layer(
        "rel",
        {
            "orders": DataSource(
                "orders",
                ParquetConfig(str(orders_path)),
                TableSchema(
                    (
                        FieldSchema("row_id", ScalarType("int64"), True),
                        FieldSchema("order_id", ScalarType("utf8"), True),
                    )
                ),
                ("row_id",),
            ),
            "items": DataSource(
                "items",
                ParquetConfig(str(items_path)),
                _items_schema(),
                ("item_id",),
            ),
        },
        {
            "orders_items": Relationship(
                "orders_items",
                "orders",
                "items",
                "one_to_many",
                "order_id",
                "order_id",
            )
        },
    )
    report = verify(layer, PhysicalCheck())
    outcome = _relationship_outcome(report, "orders_items")
    assert outcome.status == "failed"
    assert outcome.evidence["one_side_duplicate_groups"] == 1
    codes = {diagnostic.code for diagnostic in outcome.diagnostics}
    assert "relationship.one_side_duplicates" in codes
    assert all(
        diagnostic.severity == "error"
        for diagnostic in outcome.diagnostics
        if diagnostic.code == "relationship.one_side_duplicates"
    )
    # Failure codes and counts only — never the duplicate key value.
    rendered = repr(report.to_dict())
    assert "secret" not in rendered.lower()


def test_one_to_many_fails_on_non_null_orphan_child_keys(
    tmp_path: Path,
) -> None:
    orders_path = tmp_path / "orders.parquet"
    _write_parquet(
        orders_path,
        pa.table({"order_id": pa.array(["o1", "o2"], pa.utf8())}),
    )
    items_path = tmp_path / "items.parquet"
    _write_parquet(
        items_path,
        pa.table(
            {
                "item_id": pa.array(["i1", "i2"], pa.utf8()),
                "order_id": pa.array(["o1", "orphan-secret-key"], pa.utf8()),
            }
        ),
    )
    layer = _relationship_layer(
        "rel",
        {
            "orders": DataSource(
                "orders",
                ParquetConfig(str(orders_path)),
                _order_id_schema(),
                ("order_id",),
            ),
            "items": DataSource(
                "items",
                ParquetConfig(str(items_path)),
                _items_schema(),
                ("item_id",),
            ),
        },
        {
            "orders_items": Relationship(
                "orders_items",
                "orders",
                "items",
                "one_to_many",
                "order_id",
                "order_id",
            )
        },
    )
    report = verify(layer, PhysicalCheck())
    outcome = _relationship_outcome(report, "orders_items")
    assert outcome.status == "failed"
    assert outcome.evidence["orphan_non_null_rows"] == 1
    assert outcome.evidence["many_side_null_rows"] == 0
    codes = {diagnostic.code for diagnostic in outcome.diagnostics}
    assert "relationship.many_side_orphans" in codes
    rendered = repr(report.to_dict())
    assert "secret" not in rendered.lower()


def _key_sources(
    tmp_path: Path,
    left_keys: list[int | None],
    right_keys: list[int | None],
) -> tuple[DataSource, DataSource]:
    left_path = tmp_path / "left.parquet"
    _write_parquet(
        left_path, pa.table({"key": pa.array(left_keys, pa.int64())})
    )
    right_path = tmp_path / "right.parquet"
    _write_parquet(
        right_path, pa.table({"key": pa.array(right_keys, pa.int64())})
    )
    schema = _key_schema()
    return (
        DataSource(
            "left_src", ParquetConfig(str(left_path)), schema, ("key",)
        ),
        DataSource(
            "right_src", ParquetConfig(str(right_path)), schema, ("key",)
        ),
    )


def _one_to_one_layer(left: DataSource, right: DataSource) -> SemanticLayer:
    return _relationship_layer(
        "oto",
        {"left_src": left, "right_src": right},
        {
            "left_right": Relationship(
                "left_right",
                "left_src",
                "right_src",
                "one_to_one",
                "key",
                "key",
            )
        },
    )


def _many_to_many_layer(
    left: DataSource, right: DataSource
) -> SemanticLayer:
    return _relationship_layer(
        "mtm",
        {"left_src": left, "right_src": right},
        {
            "left_right": Relationship(
                "left_right",
                "left_src",
                "right_src",
                "many_to_many",
                "key",
                "key",
            )
        },
    )


def test_one_to_one_passes_when_unique_and_matched(tmp_path: Path) -> None:
    left, right = _key_sources(tmp_path, [1, 2, 3], [1, 2, 3])
    report = verify(_one_to_one_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "passed"
    assert outcome.evidence == {
        "source_null_rows": 0,
        "source_duplicate_groups": 0,
        "target_null_rows": 0,
        "target_duplicate_groups": 0,
        "source_unmatched_non_null_rows": 0,
        "target_unmatched_non_null_rows": 0,
    }
    assert outcome.diagnostics == ()


def test_one_to_one_fails_on_source_duplicates(tmp_path: Path) -> None:
    left, right = _key_sources(tmp_path, [1, 1, 2], [1, 2])
    report = verify(_one_to_one_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "failed"
    assert outcome.evidence["source_duplicate_groups"] == 1
    assert "relationship.source_duplicates" in {
        diagnostic.code for diagnostic in outcome.diagnostics
    }


def test_one_to_one_fails_on_null_keys(tmp_path: Path) -> None:
    left, right = _key_sources(tmp_path, [1, 2, None], [1, 2])
    report = verify(_one_to_one_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "failed"
    assert outcome.evidence["source_null_rows"] == 1
    assert "relationship.source_null" in {
        diagnostic.code for diagnostic in outcome.diagnostics
    }


def test_one_to_one_fails_on_unmatched_source(tmp_path: Path) -> None:
    left, right = _key_sources(tmp_path, [1, 2, 3], [1, 2])
    report = verify(_one_to_one_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "failed"
    assert outcome.evidence["source_unmatched_non_null_rows"] == 1
    assert "relationship.source_unmatched" in {
        diagnostic.code for diagnostic in outcome.diagnostics
    }


def test_one_to_one_fails_on_unmatched_target(tmp_path: Path) -> None:
    left, right = _key_sources(tmp_path, [1, 2], [1, 2, 3])
    report = verify(_one_to_one_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "failed"
    assert outcome.evidence["target_unmatched_non_null_rows"] == 1
    assert "relationship.target_unmatched" in {
        diagnostic.code for diagnostic in outcome.diagnostics
    }


def test_many_to_many_passes_with_duplicates_and_info_diagnostic(
    tmp_path: Path,
) -> None:
    left, right = _key_sources(tmp_path, [1, 1, 2, 3], [1, 2, 2, 3])
    report = verify(_many_to_many_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "passed"
    assert outcome.evidence == {
        "source_unmatched_non_null_rows": 0,
        "target_unmatched_non_null_rows": 0,
    }
    codes = {diagnostic.code for diagnostic in outcome.diagnostics}
    severities = {diagnostic.severity for diagnostic in outcome.diagnostics}
    assert "relationship.many_to_many_no_safe_traversal" in codes
    # No uniqueness failure despite duplicate keys on both sides.
    assert severities == {"info"}


def test_many_to_many_fails_on_unmatched_non_null_keys(
    tmp_path: Path,
) -> None:
    left, right = _key_sources(tmp_path, [1, 2, 3], [1, 2])
    report = verify(_many_to_many_layer(left, right), PhysicalCheck())
    outcome = _relationship_outcome(report, "left_right")
    assert outcome.status == "failed"
    assert outcome.evidence["source_unmatched_non_null_rows"] == 1
    codes = {diagnostic.code for diagnostic in outcome.diagnostics}
    assert "relationship.source_unmatched" in codes
    # The informational no-safe-traversal diagnostic is always present.
    assert "relationship.many_to_many_no_safe_traversal" in codes


def test_relationship_unavailable_when_a_bound_source_is_missing(
    tmp_path: Path,
) -> None:
    orders_path = tmp_path / "orders.parquet"
    _write_parquet(
        orders_path,
        pa.table({"order_id": pa.array(["o1", "o2"], pa.utf8())}),
    )
    layer = _relationship_layer(
        "rel",
        {
            "orders": DataSource(
                "orders",
                ParquetConfig(str(orders_path)),
                _order_id_schema(),
                ("order_id",),
            )
        },
        {
            "orders_missing": Relationship(
                "orders_missing",
                "orders",
                "missing_source",
                "one_to_many",
                "order_id",
                "order_id",
            )
        },
    )
    report = verify(layer, PhysicalCheck())
    outcome = _relationship_outcome(report, "orders_missing")
    assert outcome.status == "unavailable"
    assert outcome.evidence["error_code"] == "scan_failed"
    # An unavailable relationship makes the report incomplete + non-passing.
    assert report.complete is False
    assert report.passed is False
    unavailable_diags = [
        diagnostic
        for diagnostic in report.diagnostics
        if diagnostic.code == "source.audit.unavailable"
    ]
    assert unavailable_diags
    assert unavailable_diags[0].severity == "error"
