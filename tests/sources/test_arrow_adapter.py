"""Arrow adapter tests for parquet/csv/pyarrow sources and DuckDB pushdown.

These tests exercise the real :class:`~selayer.sources.adapters.arrow.ArrowDatasetAdapter`
and the registry-backed lifecycle for file-based and programmatic sources.
Fixtures create deterministic parquet/csv files in ``tmp_path`` so every test
is self-contained.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.adapters.arrow import ArrowDatasetAdapter
from selayer.sources.base import (
    QueryBinding,
    SourceConsistency,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import (
    CsvConfig,
    ParquetConfig,
    PyArrowConfig,
    SourceConnector,
)
from selayer.sources.errors import SourceConnectionError, SourceSchemaError
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
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
    readers: list[pa.RecordBatchReader] = []

    def reader_provider() -> ArrowObject:
        nonlocal invoke_count
        invoke_count += 1
        schema = pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("value", pa.int64(), nullable=False),
            ]
        )
        reader = pa.RecordBatchReader.from_batches(
            schema,
            [
                pa.RecordBatch.from_arrays(
                    [pa.array([1, 2], pa.int64()), pa.array([10, 20], pa.int64())],
                    names=["id", "value"],
                )
            ],
        )
        readers.append(reader)
        return reader

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
    assert engine._registry._registrations["events"].handle.resource is None

    engine._registry.reload_source("events")
    assert engine._registry._registrations["events"].handle.resource is None

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


# ---------------------------------------------------------------------------
# Physical schema drift regressions (registry gate: compare before register)
# ---------------------------------------------------------------------------
#
# The registry validates a candidate's *observed* physical schema against the
# declared schema with ``compare_schemas`` *before* committing any DuckDB
# registration — both at initial creation and on every reload.  These tests
# prove, for parquet and csv and for both physical-type and extra-column drift:
#
# * the drift is detected *before* registration (a register-spy records zero
#   ``register`` calls at initialization; on reload the candidate is never
#   swapped in);
# * after a failed *reload*, the previously registered data and generation
#   remain queryable (no half-swap);
# * every drift error is sanitized — ``__cause__``/``__context__`` are ``None``
#   and the constant message echoes neither the observed schema, the location
#   (whose path carries a ``secret`` token), nor any driver text.
#
# ``compare_schemas`` is *not* weakened: the fixtures simply write data whose
# physical schema genuinely drifts from the declaration.


class _RegisterSpy:
    """Adapter wrapper recording every ``register`` call.

    Delegates every lifecycle method to a real
    :class:`~selayer.sources.adapters.arrow.ArrowDatasetAdapter` while appending
    each registered ``stable_name`` to :attr:`register_calls`, so a drift test
    can prove ``register`` was never reached for a rejected candidate.
    """

    def __init__(self, inner: ArrowDatasetAdapter) -> None:
        self._inner = inner
        self.register_calls: list[str] = []

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        return self._inner.prepare(source, profiles, arrow_providers)

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        return self._inner.inspect_schema(handle)

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        self.register_calls.append(stable_name)
        self._inner.register(connection, stable_name, handle)

    def bind_query(
        self,
        connection: object,
        handle: SourceHandle,
        requirement: SourceScanRequirement,
    ) -> QueryBinding | None:
        return self._inner.bind_query(connection, handle, requirement)

    def close(self, handle: SourceHandle) -> None:
        self._inner.close(handle)


@dataclass(frozen=True)
class _DriftCase:
    """One physical-drift scenario (connector kind x drift kind)."""

    label: str
    declared: TableSchema
    write_valid: Callable[[Path], None]
    write_drifted: Callable[[Path], None]
    connector: Callable[[str], SourceConnector]
    mismatch_code: str


def _parquet_connector(location: str) -> SourceConnector:
    return ParquetConfig(location)


def _csv_connector(location: str) -> SourceConnector:
    return CsvConfig(location)


def _write_parquet(path: Path, columns: dict[str, object], schema: pa.Schema) -> None:
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array(columns[field.name], field.type) for field in schema],
            schema=schema,
        ),
        path,
    )


def _pq_valid_single(path: Path) -> None:
    _write_parquet(
        path,
        {"id": [1, 2, 3]},
        pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )


def _pq_valid_typed(path: Path) -> None:
    _write_parquet(
        path,
        {"id": [1, 2, 3], "amount": [1.0, 2.0, 3.0]},
        pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("amount", pa.float64(), nullable=False),
            ]
        ),
    )


def _pq_drifted_with_int_amount(path: Path) -> None:
    # ``id`` values are unchanged ([1, 2, 3]); an extra/drifting ``amount``
    # column is int64, which is either an extra field or a type drift
    # depending on the declared schema.
    _write_parquet(
        path,
        {"id": [1, 2, 3], "amount": [5, 15, 3]},
        pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("amount", pa.int64(), nullable=False),
            ]
        ),
    )


def _csv_valid_single(path: Path) -> None:
    path.write_text("id\n1\n2\n3\n", encoding="utf-8")


def _csv_valid_typed(path: Path) -> None:
    # ``amount`` holds floats so CSV inference reads it back as float64.
    path.write_text("id,amount\n1,1.0\n2,2.0\n3,3.0\n", encoding="utf-8")


def _csv_drifted_with_int_amount(path: Path) -> None:
    # ``id`` values are unchanged ([1, 2, 3]); ``amount`` is integer-only so CSV
    # inference reads it back as int64 (an extra field or a float64->int64
    # type drift depending on the declared schema).
    path.write_text("id,amount\n1,5\n2,15\n3,3\n", encoding="utf-8")


_DRIFT_CASES: tuple[_DriftCase, ...] = (
    _DriftCase(
        label="parquet-extra-column",
        declared=TableSchema((FieldSchema("id", ScalarType("int64"), False),)),
        write_valid=_pq_valid_single,
        write_drifted=_pq_drifted_with_int_amount,
        connector=_parquet_connector,
        mismatch_code="extra_observed_field",
    ),
    _DriftCase(
        label="parquet-physical-type",
        declared=TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), False),
                FieldSchema("amount", ScalarType("float64"), False),
            )
        ),
        write_valid=_pq_valid_typed,
        write_drifted=_pq_drifted_with_int_amount,
        connector=_parquet_connector,
        mismatch_code="field_type",
    ),
    _DriftCase(
        label="csv-extra-column",
        declared=TableSchema((FieldSchema("id", ScalarType("int64"), True),)),
        write_valid=_csv_valid_single,
        write_drifted=_csv_drifted_with_int_amount,
        connector=_csv_connector,
        mismatch_code="extra_observed_field",
    ),
    _DriftCase(
        label="csv-physical-type",
        declared=TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), True),
                FieldSchema("amount", ScalarType("float64"), True),
            )
        ),
        write_valid=_csv_valid_typed,
        write_drifted=_csv_drifted_with_int_amount,
        connector=_csv_connector,
        mismatch_code="field_type",
    ),
)


def _orders_layer(connector: SourceConnector, schema: TableSchema) -> SemanticLayer:
    return SemanticLayer(
        1,
        "drift",
        "",
        "",
        {
            "orders": DataSource(
                name="orders",
                connector=connector,
                schema=schema,
                grain=("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )


@pytest.mark.parametrize("case", _DRIFT_CASES, ids=[c.label for c in _DRIFT_CASES])
def test_physical_drift_prevents_initialization(
    case: _DriftCase,
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    # The drifted file path deliberately carries a ``secret`` token so the
    # sanitized-error assertions are meaningful: the connector ``location``
    # would leak if the drift error ever echoed it.
    path = tmp_path / "orders_secret"
    case.write_drifted(path)
    connector = case.connector(str(path))
    declared = case.declared

    # 1. The adapter gate detects the drift on the prepared candidate before
    #    any registration could occur.
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(
        ParsedSource(
            name="orders", connector=connector, schema=declared, grain=("id",)
        ),
        profiles,
        providers,
    )
    observed = adapter.inspect_schema(handle)
    mismatches = compare_schemas(declared, observed)
    assert any(mismatch.code == case.mismatch_code for mismatch in mismatches)
    adapter.close(handle)

    # 2. A register-spy proves ``register`` is never called for the drifted
    #    candidate, and the registry surfaces a sanitized initialization error.
    spy = _RegisterSpy(ArrowDatasetAdapter())
    connection = duckdb.connect(":memory:")
    with pytest.raises(SourceConnectionError) as caught:
        SourceRegistry.create(
            _orders_layer(connector, declared),
            connection,
            profiles,
            providers,
            adapters={"parquet": spy, "csv": spy, "pyarrow": spy},
        )

    assert caught.value.code == "source_initialization_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)
    # No registration was committed before the mismatch was detected.
    assert spy.register_calls == []


@pytest.mark.parametrize("case", _DRIFT_CASES, ids=[c.label for c in _DRIFT_CASES])
def test_physical_drift_on_reload_keeps_old_registration(
    case: _DriftCase,
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    path = tmp_path / "orders_secret"
    case.write_valid(path)
    connector = case.connector(str(path))
    declared = case.declared

    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        _orders_layer(connector, declared), connection, profiles, providers
    )
    # The source is live at generation 1 and its ``id`` values sum to 6.
    assert registry.status("orders").generation == 1
    assert registry.execute('SELECT sum("id") FROM "orders"').fetchone() == (6,)

    # Overwrite the file with a drifted physical schema.  The ``id`` values are
    # unchanged ([1, 2, 3]); only the drifted column differs, so the *old*
    # registered dataset still reads the original ``id`` projection after the
    # rejected reload.
    case.write_drifted(path)

    with pytest.raises(SourceSchemaError) as caught:
        registry.reload_source("orders")

    assert caught.value.code == "schema_mismatch"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)

    # The failed reload did not swap the registration: the generation is
    # unchanged and the previously registered data is still queryable.
    assert registry.status("orders").generation == 1
    assert registry.execute('SELECT sum("id") FROM "orders"').fetchone() == (6,)

    registry.close()


# ---------------------------------------------------------------------------
# S3 credential_profile filesystem resolution
# ---------------------------------------------------------------------------


def test_parquet_with_credential_profile_uses_s3_filesystem(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A parquet source with ``credential_profile`` resolves an S3 filesystem.

    The ``s3://`` scheme is stripped per PyArrow's filesystem contract, the
    named profile is resolved through the resolver, and the dataset is built
    against the resolved filesystem.  A :class:`pyarrow.fs.SubTreeFileSystem`
    rooted at ``tmp_path`` stands in for S3 so the test needs no Docker.
    """

    import pyarrow.fs as pafs

    # Write a parquet file under tmp_path/bucket/ so the SubTreeFileSystem can
    # read it via the stripped path "bucket/data.parquet".  Fields are written
    # non-nullable to match the declared ``_orders_schema()`` so the observed
    # physical schema does not drift on nullability.
    bucket_dir = tmp_path / "mybucket"
    bucket_dir.mkdir()
    table = pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "amount": pa.array([10, 20, 30], pa.int64()),
        },
        schema=pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("amount", pa.int64(), nullable=False),
            ]
        ),
    )
    pq.write_table(table, bucket_dir / "data.parquet")

    subtree = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())

    resolved_profiles: list[str] = []

    def fake_s3_filesystem(_profile: object) -> pafs.FileSystem:
        resolved_profiles.append("s3_profile")
        return subtree

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.s3_filesystem", fake_s3_filesystem
    )

    source = ParsedSource(
        name="orders",
        connector=ParquetConfig(
            "s3://mybucket/data.parquet",
            credential_profile="s3_profile",
        ),
        schema=_orders_schema(),
        grain=("id",),
    )
    profiles = MappingProfileResolver(
        {"s3_profile": {"access_key": "AKIA", "secret_key": "shh"}}
    )
    providers = MappingArrowProviderResolver({})

    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(source, profiles, providers)
    observed = adapter.inspect_schema(handle)

    assert observed == _orders_schema()
    assert resolved_profiles == ["s3_profile"]

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "orders", handle)
    assert connection.execute('SELECT sum("amount") FROM "orders"').fetchone() == (60,)
    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# Task 5: consistency modes and safe snapshot tokens
# ---------------------------------------------------------------------------


def test_local_parquet_is_reopenable_with_content_digest(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """A local parquet file is REOPENABLE_SNAPSHOT with a content digest token.

    The snapshot is a hex content digest over the physical file(s) — never the
    file location.  The digest is stable across prepares and changes when the
    content changes.
    """

    path = tmp_path / "orders_secret_loc" / "orders.parquet"
    path.parent.mkdir()
    pq.write_table(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "amount": pa.array([5, 15, 3], pa.int64()),
            },
            schema=pa.schema(
                [
                    pa.field("id", pa.int64(), nullable=False),
                    pa.field("amount", pa.int64(), nullable=False),
                ]
            ),
        ),
        path,
    )
    source = ParsedSource(
        name="orders",
        connector=ParquetConfig(str(path)),
        schema=_orders_schema(),
        grain=("id",),
    )
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(source, profiles, providers)

    assert handle.consistency is SourceConsistency.REOPENABLE_SNAPSHOT
    assert handle.snapshot is not None
    # The snapshot is a hex digest, not the file location.
    assert all(c in "0123456789abcdef" for c in handle.snapshot)
    assert "secret_loc" not in handle.snapshot
    assert str(path) not in handle.snapshot

    # Stable across prepares.
    handle2 = adapter.prepare(source, profiles, providers)
    assert handle2.snapshot == handle.snapshot
    adapter.close(handle)
    adapter.close(handle2)


def test_local_parquet_digest_changes_on_content_change(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """The content digest changes when the underlying file content changes."""

    path = tmp_path / "orders.parquet"
    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("amount", pa.int64(), nullable=False),
        ]
    )
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([1, 2, 3], pa.int64()), pa.array([5, 15, 3], pa.int64())],
            schema=schema,
        ),
        path,
    )
    source = ParsedSource(
        name="orders",
        connector=ParquetConfig(str(path)),
        schema=_orders_schema(),
        grain=("id",),
    )
    adapter = ArrowDatasetAdapter()
    digest1 = adapter.prepare(source, profiles, providers).snapshot

    # Overwrite with different content (same schema).
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([1, 2, 3], pa.int64()), pa.array([9, 9, 9], pa.int64())],
            schema=schema,
        ),
        path,
    )
    digest2 = adapter.prepare(source, profiles, providers).snapshot

    assert digest1 != digest2


def test_local_csv_is_reopenable_with_content_digest(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """A local csv file is REOPENABLE_SNAPSHOT with a content digest token."""

    path = tmp_path / "events.csv"
    path.write_text("id,amount\n1,10\n2,20\n3,30\n", encoding="utf-8")
    source = ParsedSource(
        name="events",
        connector=CsvConfig(str(path)),
        schema=TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), True),
                FieldSchema("amount", ScalarType("int64"), True),
            )
        ),
        grain=("id",),
    )
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(source, profiles, providers)

    assert handle.consistency is SourceConsistency.REOPENABLE_SNAPSHOT
    assert handle.snapshot is not None
    assert all(c in "0123456789abcdef" for c in handle.snapshot)
    assert str(path) not in handle.snapshot
    adapter.close(handle)


def test_local_parquet_directory_digests_sorted_files(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """A local parquet directory produces a digest over sorted physical files.

    Adding a file changes the digest; the digest is independent of file
    enumeration order.
    """

    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("amount", pa.int64(), nullable=False),
        ]
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([1], pa.int64()), pa.array([10], pa.int64())], schema=schema
        ),
        data_dir / "a.parquet",
    )
    declared = TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("amount", ScalarType("int64"), False),
        )
    )
    source = ParsedSource(
        name="orders",
        connector=ParquetConfig(str(data_dir)),
        schema=declared,
        grain=("id",),
    )
    adapter = ArrowDatasetAdapter()
    digest1 = adapter.prepare(source, profiles, providers).snapshot

    # Adding a second file changes the digest.
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([2], pa.int64()), pa.array([20], pa.int64())], schema=schema
        ),
        data_dir / "b.parquet",
    )
    digest2 = adapter.prepare(source, profiles, providers).snapshot
    assert digest1 != digest2
    assert digest2 is not None


def test_remote_parquet_with_credential_profile_is_live(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A parquet source with a ``credential_profile`` is LIVE (unversioned remote).

    Remote objects cannot be content-digested at prepare time without a network
    round-trip, so they remain LIVE in version 1.
    """

    import pyarrow.fs as pafs

    bucket_dir = tmp_path / "mybucket"
    bucket_dir.mkdir()
    table = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "amount": pa.array([10], pa.int64()),
        },
        schema=pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("amount", pa.int64(), nullable=False),
            ]
        ),
    )
    pq.write_table(table, bucket_dir / "data.parquet")
    subtree = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.s3_filesystem",
        lambda _profile: subtree,
    )
    source = ParsedSource(
        name="orders",
        connector=ParquetConfig(
            "s3://mybucket/data.parquet",
            credential_profile="s3_profile",
        ),
        schema=_orders_schema(),
        grain=("id",),
    )
    profiles = MappingProfileResolver(
        {"s3_profile": {"access_key": "AKIA", "secret_key": "shh"}}
    )
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(source, profiles, MappingArrowProviderResolver({}))

    assert handle.consistency is SourceConsistency.LIVE
    assert handle.snapshot is None
    adapter.close(handle)


def test_pyarrow_provider_is_live() -> None:
    """A programmatic pyarrow source is LIVE (no stable snapshot in version 1)."""

    def provider() -> ArrowObject:
        return pa.table(
            {
                "id": pa.array([1], pa.int64()),
                "value": pa.array([1], pa.int64()),
            },
            schema=pa.schema(
                [
                    pa.field("id", pa.int64(), nullable=False),
                    pa.field("value", pa.int64(), nullable=False),
                ]
            ),
        )

    providers = MappingArrowProviderResolver({"events": provider})
    source = ParsedSource(
        name="events",
        connector=PyArrowConfig("events"),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(source, MappingProfileResolver({}), providers)

    assert handle.consistency is SourceConsistency.LIVE
    assert handle.snapshot is None
    adapter.close(handle)
