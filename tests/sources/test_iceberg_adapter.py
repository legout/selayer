"""PyIceberg adapter integration tests with a local SQL-backed catalog.

These tests exercise the :class:`~selayer.sources.adapters.iceberg.IcebergAdapter`
through the full :class:`~selayer.query.QueryEngine` pipeline using a local
SQL-backed PyIceberg catalog (SQLite) and a local warehouse.  They verify
projection/filter pushdown, snapshot refresh, schema-mismatch safety, reader
cleanup, and that no DuckDB Iceberg extension is ever loaded or installed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pytest

from selayer.model import (
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
)
from selayer.query import QueryEngine
from selayer.sources.config import IcebergConfig
from selayer.sources.profiles import MappingProfileResolver
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

if TYPE_CHECKING:
    from selayer.catalog import SemanticLayer

try:
    import pyiceberg  # noqa: F401

    _ICEBERG_AVAILABLE = True
except ImportError:
    _ICEBERG_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _ICEBERG_AVAILABLE, reason="pyiceberg extra not installed"
)


# ---------------------------------------------------------------------------
# Recording infrastructure
# ---------------------------------------------------------------------------


class _RecordingScan:
    """Records the arguments passed to every ``table.scan(...)`` call."""

    def __init__(self) -> None:
        self.selected_fields: tuple[str, ...] | None = None
        self.row_filter: str | None = None
        self.reader_count: int = 0
        self.close_count: int = 0
        self.scan_error: BaseException | None = None


class _TrackedReader:
    """Wraps a ``RecordBatchReader`` recording every ``close()`` call.

    Every attribute access other than ``close`` is delegated to the underlying
    reader via ``__getattr__`` so DuckDB's Arrow stream interface is unchanged.
    A ``close()`` increments the recording's ``close_count`` *before*
    delegating so the instrumentation survives even if the inner close raises.
    """

    _inner: Any
    _recording: _RecordingScan

    def __init__(self, inner: Any, recording: _RecordingScan) -> None:
        self._inner = inner
        self._recording = recording

    def close(self) -> None:
        self._recording.close_count += 1
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingScanBuilder:
    """Wraps a PyIceberg ``TableScan`` so the reader is tracked on creation."""

    def __init__(self, inner: object, recording: _RecordingScan) -> None:
        self._inner = inner
        self._recording = recording

    def to_arrow_batch_reader(self) -> _TrackedReader:
        raw = self._inner.to_arrow_batch_reader()  # type: ignore[attr-defined]
        return _TrackedReader(raw, self._recording)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _RecordingTable:
    """Wraps a PyIceberg table and intercepts ``scan`` for recording."""

    def __init__(self, inner: object, recording: _RecordingScan) -> None:
        self._inner = inner
        self._recording = recording

    def scan(self, **kwargs: object) -> _RecordingScanBuilder:
        if self._recording.scan_error is not None:
            error = self._recording.scan_error
            self._recording.scan_error = None
            raise error
        fields = kwargs.get("selected_fields")
        self._recording.selected_fields = (
            tuple(fields) if isinstance(fields, (tuple, list)) else ()
        )
        raw_filter = kwargs.get("row_filter")
        self._recording.row_filter = raw_filter if isinstance(raw_filter, str) else None
        self._recording.reader_count += 1
        return _RecordingScanBuilder(
            self._inner.scan(**kwargs),  # type: ignore[attr-defined]
            self._recording,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _RecordingCatalog:
    """Wraps a PyIceberg catalog so ``load_table`` returns recording tables."""

    def __init__(self, inner: object, recording: _RecordingScan) -> None:
        self._inner = inner
        self._recording = recording

    def load_table(self, identifier: tuple[str, ...]) -> _RecordingTable:
        table = self._inner.load_table(identifier)  # type: ignore[attr-defined]
        return _RecordingTable(table, self._recording)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _FailingRegisterConnection:
    """Connection wrapper that raises on ``register``, delegating all else.

    Used to inject a register failure *after* the reader has been created by
    ``table.scan(...).to_arrow_batch_reader()`` so the adapter's bind_query
    cleanup path is exercised.  Every other attribute access delegates to the
    inner DuckDB connection.
    """

    def __init__(self, inner: Any, error: BaseException) -> None:
        self._inner = inner
        self._error = error

    def register(self, name: str, obj: object) -> None:
        raise self._error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Test layer
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), True),
            FieldSchema("category", ScalarType("utf8"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )


def _iceberg_layer() -> SemanticLayer:
    from selayer.catalog import SemanticLayer as _SL

    data_sources = {
        "events": DataSource(
            "events",
            IcebergConfig(
                catalog_profile="test_catalog",
                namespace=("default",),
                table="events",
            ),
            _events_schema(),
            ("id",),
        ),
    }
    dimensions = {
        "category": Dimension("category", "events", "category", "string"),
        "value": Dimension("value", "events", "value", "integer"),
    }
    facts = {
        "event_value": Fact.from_expression(
            "event_value", "events", "events.value", "int64"
        ),
    }
    measures = {
        "total_value": Measure("total_value", "event_value", "sum"),
    }
    metrics = {
        "total_value": Metric.from_expression(
            "total_value", "total_value", ["total_value"]
        ),
    }
    return _SL(
        1, "test", "", "", data_sources, dimensions, facts, measures, metrics, {}
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _IcebergFixture:
    layer: SemanticLayer
    profiles: MappingProfileResolver
    append_snapshot: Callable[[], None]
    recording: _RecordingScan


def _create_iceberg_table(tmp_path: Path) -> object:
    """Create a local SQL-backed PyIceberg catalog and events table."""

    from pyiceberg.catalog import load_catalog

    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    db_path = tmp_path / "catalog.db"
    catalog = load_catalog(
        "test_catalog",
        type="sql",
        uri=f"sqlite:///{db_path}",
        warehouse=f"file://{warehouse}",
    )
    catalog.create_namespace("default")
    schema = pa.schema(
        [
            ("id", pa.int64()),
            ("category", pa.string()),
            ("value", pa.int64()),
        ]
    )
    table: object = catalog.create_table(("default", "events"), schema=schema)
    table.append(  # type: ignore[attr-defined]
        pa.table({"id": [1, 2, 3], "category": ["A", "A", "B"], "value": [4, 6, 5]})
    )
    return table


@pytest.fixture
def iceberg_table_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _IcebergFixture:
    """A local iceberg table, semantic layer, profile resolver, and recording."""

    from pyiceberg.catalog import load_catalog as real_load_catalog

    import selayer.sources.adapters.iceberg as iceberg_mod

    table = _create_iceberg_table(tmp_path)
    recording = _RecordingScan()

    warehouse = tmp_path / "warehouse"
    db_path = tmp_path / "catalog.db"

    def recording_load_catalog(name: str, **config: str) -> _RecordingCatalog:
        catalog = real_load_catalog(name, **config)
        return _RecordingCatalog(catalog, recording)

    monkeypatch.setattr(iceberg_mod, "_load_catalog", recording_load_catalog)

    profiles = MappingProfileResolver(
        {
            "test_catalog": {
                "type": "sql",
                "uri": f"sqlite:///{db_path}",
                "warehouse": f"file://{warehouse}",
            }
        }
    )

    def append_snapshot() -> None:
        table.append(  # type: ignore[attr-defined]
            pa.table({"id": [4], "category": ["A"], "value": [20]})
        )

    return _IcebergFixture(
        layer=_iceberg_layer(),
        profiles=profiles,
        append_snapshot=append_snapshot,
        recording=recording,
    )


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------


def test_normalize_arrow_schema_recurses_into_nested_types() -> None:
    """Normalization recurses through every nested container type.

    PyIceberg surfaces ``large_string``/``large_binary`` at any depth (a
    ``string``/``binary`` column is read as its large variant, including inside
    ``list``/``large_list``/``fixed_size_list``, ``struct``, ``map``, and
    ``dictionary`` containers).  The adapter normalizes every leaf to
    ``string``/``binary`` while preserving field names, nullability, metadata,
    list sizes, map key/item shape, and dictionary ordering — so a standard
    ``utf8``/``binary`` declaration matches the observed schema.
    """

    from selayer.sources.adapters.iceberg import _normalize_arrow_schema

    field_metadata = {b"field": b"meta"}
    schema_metadata = {b"schema": b"meta"}

    raw = pa.schema(
        [
            pa.field(
                "top_str",
                pa.large_string(),
                nullable=False,
                metadata=field_metadata,
            ),
            pa.field("top_bin", pa.large_binary(), metadata=field_metadata),
            pa.field(
                "list_str",
                pa.list_(pa.field("item", pa.large_string(), nullable=False)),
            ),
            pa.field(
                "large_list_bin",
                pa.large_list(pa.field("item", pa.large_binary())),
            ),
            pa.field(
                "fixed_list_bin",
                pa.list_(pa.field("item", pa.large_binary()), 4),
            ),
            pa.field(
                "struct_nested",
                pa.struct(
                    [
                        pa.field("a", pa.large_string(), metadata=field_metadata),
                        pa.field("b", pa.large_binary(), nullable=False),
                    ]
                ),
            ),
            pa.field(
                "map_nested",
                pa.map_(
                    pa.field("key", pa.large_string(), nullable=False),
                    pa.field("value", pa.large_binary()),
                ),
            ),
            pa.field("dict_str", pa.dictionary(pa.int32(), pa.large_string())),
            pa.field(
                "dict_ordered_bin",
                pa.dictionary(pa.int8(), pa.large_binary(), ordered=True),
            ),
        ],
        metadata=schema_metadata,
    )

    normalized = _normalize_arrow_schema(raw)

    # Top-level leaves normalized; nullability and metadata preserved.
    assert normalized.field("top_str").type == pa.string()
    assert normalized.field("top_str").nullable is False
    assert normalized.field("top_str").metadata == field_metadata
    assert normalized.field("top_bin").type == pa.binary()
    assert normalized.field("top_bin").metadata == field_metadata
    assert normalized.metadata == schema_metadata

    # list<large_string> -> list<string>; inner nullability preserved.
    list_type = normalized.field("list_str").type
    assert pa.types.is_list(list_type)
    assert list_type.value_type == pa.string()
    assert list_type.value_field.nullable is False

    # large_list<large_binary> stays a large_list with a normalized value.
    large_list_type = normalized.field("large_list_bin").type
    assert pa.types.is_large_list(large_list_type)
    assert large_list_type.value_type == pa.binary()

    # fixed_size_list<large_binary, 4> keeps the size and normalizes the value.
    fixed_type = normalized.field("fixed_list_bin").type
    assert pa.types.is_fixed_size_list(fixed_type)
    assert fixed_type.list_size == 4
    assert fixed_type.value_type == pa.binary()

    # struct recurses into every named child, preserving metadata/nullability.
    struct_type = normalized.field("struct_nested").type
    assert pa.types.is_struct(struct_type)
    assert struct_type.field("a").type == pa.string()
    assert struct_type.field("a").metadata == field_metadata
    assert struct_type.field("b").type == pa.binary()
    assert struct_type.field("b").nullable is False

    # map<large_string, large_binary> -> map<string, binary>; key nullability
    # preserved (PyIceberg requires non-nullable keys).
    map_type = normalized.field("map_nested").type
    assert pa.types.is_map(map_type)
    assert map_type.key_type == pa.string()
    assert map_type.item_type == pa.binary()
    assert map_type.key_field.nullable is False

    # dictionary<index, large_string> -> dictionary<index, string>; ordered flag
    # preserved on both an unordered and an ordered dictionary.
    dict_type = normalized.field("dict_str").type
    assert pa.types.is_dictionary(dict_type)
    assert dict_type.index_type == pa.int32()
    assert dict_type.value_type == pa.string()
    assert dict_type.ordered is False
    dict_ordered_type = normalized.field("dict_ordered_bin").type
    assert pa.types.is_dictionary(dict_ordered_type)
    assert dict_ordered_type.value_type == pa.binary()
    assert dict_ordered_type.ordered is True


# ---------------------------------------------------------------------------
# Main test from the brief
# ---------------------------------------------------------------------------


def test_iceberg_binding_uses_fresh_scan_with_projection_and_filter(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    f = iceberg_table_fixture
    with QueryEngine(f.layer, profiles=f.profiles) as engine:
        first = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A"]},
        )
        f.append_snapshot()
        engine.reload_source("events")
        second = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A"]},
        )

    assert first["total_value"].sum() == 10
    assert second["total_value"].sum() == 30
    assert f.recording.selected_fields == ("id", "category", "value")
    assert f.recording.row_filter == "category IN ('A')"
    assert f.recording.reader_count == 2


# ---------------------------------------------------------------------------
# Filter translation tests
# ---------------------------------------------------------------------------


def test_iceberg_scalar_filter_translation(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """A scalar equality filter is pushed down and the residual matches Arrow."""

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        result = engine.query(
            ["total_value"],
            ["category"],
            {"category": "A"},
        )
    assert fixture.recording.row_filter == "category = 'A'"
    assert result["total_value"].sum() == 10


def test_iceberg_list_filter_translation(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """A list membership filter is pushed down as ``IN`` and matches Arrow."""

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        result = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A", "B"]},
        )
    assert fixture.recording.row_filter == "category IN ('A', 'B')"
    assert result["total_value"].sum() == 15


def test_iceberg_range_filter_translation(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """An inclusive range filter on a numeric dimension is pushed down and matches.

    The ``value`` integer dimension receives an inclusive range.  PyIceberg gets
    ``value >= 5 AND value <= 10`` so it scans only the matching rows, while the
    compiled DuckDB query still evaluates ``value BETWEEN ? AND ?`` as a
    residual.  The recorded projection carries the grain (``id``), the dimension
    (``value``), and the fact reference (``value``, de-duplicated); the query
    result is cross-checked against an independent Arrow calculation over the
    raw rows rather than against the private formatter alone.
    """

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        result = engine.query(
            ["total_value"],
            ["value"],
            {"value": (5, 10)},
        )
    assert fixture.recording.selected_fields == ("id", "value")
    assert fixture.recording.row_filter == "value >= 5 AND value <= 10"

    # Independent Arrow calculation over the raw rows.
    raw_values = [4, 6, 5]
    expected = sum(v for v in raw_values if 5 <= v <= 10)
    assert expected == 11
    assert result["total_value"].sum() == expected


def test_nonfinite_float_filters_remain_residual_only(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """NaN and infinity stay residual filters instead of invalid pushdown."""

    fixture = iceberg_table_fixture
    cases = (
        (float("nan"), 0),
        ([float("inf")], 0),
        ((float("-inf"), float("inf")), 15),
    )
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        for filter_value, expected in cases:
            result = engine.query(
                ["total_value"],
                ["value"],
                {"value": filter_value},
            )
            assert fixture.recording.row_filter is None
            assert result["total_value"].sum() == expected


def test_unsupported_filter_remains_residual_only(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """An unsupported filter value produces no pushdown; DuckDB still filters.

    A list filter containing a ``None`` value is not a pushdown scalar, so
    ``requirements_for_plan`` emits no :class:`SourceFilter` and PyIceberg scans
    every row (``row_filter`` is ``None``).  DuckDB still evaluates
    ``category IN ('A', NULL)`` as a residual: the three scanned rows are
    reduced to the two ``'A'`` rows, which an independent Arrow calculation
    confirms.  This proves the residual path end-to-end through the query
    engine rather than only through the private formatter.
    """

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        result = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A", None]},
        )
    # No pushdown: PyIceberg scanned every row.
    assert fixture.recording.row_filter is None

    # Independent Arrow calculation: the IN ('A', NULL) residual matches only
    # the 'A' rows (a NULL list member never matches a non-NULL value), so the
    # residual genuinely filtered the three full-scan rows down to two.
    raw_categories = ["A", "A", "B"]
    raw_values = [4, 6, 5]
    full_scan_sum = sum(raw_values)
    residual_sum = sum(v for c, v in zip(raw_categories, raw_values) if c == "A")
    assert full_scan_sum == 15
    assert residual_sum == 10
    assert result["total_value"].sum() == residual_sum


# ---------------------------------------------------------------------------
# Schema mismatch / snapshot safety
# ---------------------------------------------------------------------------


def test_iceberg_schema_mismatch_preserves_snapshot(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """A schema mismatch during reload preserves the old registration.

    We swap the registry's declared schema for a drifted one (value as float64
    instead of int64), then reload.  The old handle must remain queryable with
    its snapshot and generation unchanged.
    """

    from types import MappingProxyType

    from selayer.sources.catalog import ParsedSource
    from selayer.sources.errors import SourceSchemaError

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        status_before = engine.source_status("events")
        snapshot_before = status_before.snapshot
        generation_before = status_before.generation

        # Swap the registry's source definition to a drifted schema.
        old_parsed = engine._registry._sources["events"]  # type: ignore[attr-defined]
        drifted_schema = TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), True),
                FieldSchema("category", ScalarType("utf8"), True),
                FieldSchema("value", ScalarType("float64"), True),
            )
        )
        engine._registry._sources = MappingProxyType(  # type: ignore[attr-defined]
            {
                "events": ParsedSource(
                    name=old_parsed.name,
                    connector=old_parsed.connector,
                    schema=drifted_schema,
                    grain=old_parsed.grain,
                )
            }
        )

        with pytest.raises(SourceSchemaError):
            engine.reload_source("events")

        status_after = engine.source_status("events")
        assert status_after.snapshot == snapshot_before
        assert status_after.generation == generation_before
        result = engine.query(["total_value"], ["category"], {"category": ["A"]})
        assert result["total_value"].sum() == 10


def test_iceberg_status_exposes_snapshot_id_only(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """Status exposes only the safe snapshot ID, never the catalog or table."""

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        status = engine.source_status("events")
    assert status.snapshot is not None
    assert status.snapshot.isdigit()
    assert "warehouse" not in repr(status)
    assert "sqlite" not in repr(status)
    assert "file://" not in repr(status)


# ---------------------------------------------------------------------------
# Reader cleanup
# ---------------------------------------------------------------------------


def test_reader_closes_after_success_and_failure(
    iceberg_table_fixture: _IcebergFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers close after both successful and failed query execution.

    The reader is registered inside ``registry.bind`` *before* compile/execute,
    so a deterministic execution failure still drives the binding's ``finally``
    cleanup.  ``close_count`` increments once per query — for the successful
    query and for the failed query alike — proving readers never leak on either
    path.  The failure is injected by compiling malformed SQL (not by querying
    a closed engine), so it is deterministic and not flaky.
    """

    import selayer.query as query_mod
    from selayer.compilation import CompiledQuery
    from selayer.errors import QueryExecutionError

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        # Successful query: a reader is created and then closed in bind's
        # ``finally`` cleanup.
        engine.query(["total_value"], ["category"], {"category": ["A"]})
        assert fixture.recording.reader_count == 1
        assert fixture.recording.close_count == 1

        # Failing query: inject malformed SQL so execution fails after the
        # reader has already been registered by ``bind``.
        def failing_compile(_plan: object) -> CompiledQuery:
            return CompiledQuery(
                sql="SELECT * FROM __selayer_nonexistent_table__",
                parameters=(),
            )

        monkeypatch.setattr(query_mod, "compile_duckdb", failing_compile)

        with pytest.raises(QueryExecutionError):
            engine.query(["total_value"], ["category"], {"category": ["A"]})

        # A new reader was created for the failing query and closed in the
        # bind ``finally`` cleanup, so both counters advance to 2.
        assert fixture.recording.reader_count == 2
        assert fixture.recording.close_count == 2


# ---------------------------------------------------------------------------
# No DuckDB extension
# ---------------------------------------------------------------------------


def test_iceberg_never_installs_or_loads_duckdb_extension(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """No DuckDB ``iceberg`` extension is ever loaded or installed."""

    fixture = iceberg_table_fixture
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        engine.query(["total_value"], ["category"], {"category": ["A"]})
        connection = engine._registry._connection  # type: ignore[attr-defined]
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT extension_name, installed, loaded "
            "FROM duckdb_extensions() WHERE installed OR loaded"
        ).fetchall()
        active = {row[0] for row in rows}
    assert "iceberg" not in active


# ---------------------------------------------------------------------------
# Binding-failure sanitization and reader cleanup (Task 7 review fixes)
# ---------------------------------------------------------------------------


def test_scan_failure_surfaces_sanitized_source_error(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """A raw PyIceberg exception from ``table.scan`` is sanitized at the boundary.

    A ``RuntimeError`` carrying a fake credential/location is injected into
    ``table.scan``.  The registry boundary converts it into a
    :class:`~selayer.sources.errors.SourceConnectionError` (code
    ``bind_failed``) whose message and repr never contain the credential, and
    whose ``__cause__``/``__context__`` are both ``None``.  No reader is
    created because the scan failed before reader construction.
    """

    from selayer.sources.errors import SourceConnectionError

    fixture = iceberg_table_fixture
    credential = "s3://AKIAIOSFODNN7EXAMPLE@warehouse/events"
    fixture.recording.scan_error = RuntimeError(
        f"failed to scan iceberg table: {credential}"
    )
    with (
        QueryEngine(fixture.layer, profiles=fixture.profiles) as engine,
        pytest.raises(SourceConnectionError) as exc_info,
    ):
        engine.query(["total_value"], ["category"], {"category": ["A"]})

    error = exc_info.value
    # The constant message never echoes driver text.
    assert credential not in str(error)
    assert credential not in repr(error)
    assert error.code == "bind_failed"
    assert error.message == "the source could not be bound for the query"
    # source_id is sanitized (valid catalog name retained).
    assert error.source_id == "events"
    # No retained driver exception.
    assert error.__cause__ is None
    assert error.__context__ is None
    # No reader was created (scan raised before reader construction).
    assert fixture.recording.reader_count == 0
    assert fixture.recording.close_count == 0


def test_register_failure_closes_reader_and_sanitizes_error(
    iceberg_table_fixture: _IcebergFixture,
) -> None:
    """A failing ``register`` closes the created reader and is sanitized.

    A ``RuntimeError`` carrying a fake credential is injected into
    ``connection.register`` *after* the reader has been created by
    ``table.scan(...).to_arrow_batch_reader()``.  The adapter's ``bind_query``
    cleanup unregisters and closes the reader best-effort before re-raising,
    so ``close_count`` increments.  The registry boundary then converts the
    raw exception into a sanitized
    :class:`~selayer.sources.errors.SourceConnectionError` with no credential
    and no ``__cause__``/``__context__``.
    """

    from selayer.sources.errors import SourceConnectionError

    fixture = iceberg_table_fixture
    credential = "AKIAIOSFODNN7EXAMPLE/s3://secret-warehouse/token"
    with QueryEngine(fixture.layer, profiles=fixture.profiles) as engine:
        original = engine._registry._connection  # type: ignore[attr-defined]
        engine._registry._connection = _FailingRegisterConnection(  # type: ignore[attr-defined]
            original,
            RuntimeError(f"connection.register failed: {credential}"),
        )
        with pytest.raises(SourceConnectionError) as exc_info:
            engine.query(["total_value"], ["category"], {"category": ["A"]})

    # The reader was created by the scan and then closed by the adapter's
    # bind_query cleanup despite the register failure.
    assert fixture.recording.reader_count == 1
    assert fixture.recording.close_count == 1

    error = exc_info.value
    # The constant message never echoes driver text.
    assert credential not in str(error)
    assert credential not in repr(error)
    assert error.code == "bind_failed"
    assert error.message == "the source could not be bound for the query"
    assert error.source_id == "events"
    # No retained driver exception.
    assert error.__cause__ is None
    assert error.__context__ is None
