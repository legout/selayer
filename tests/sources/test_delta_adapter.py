"""Delta adapter tests for deltalake-backed Delta Lake sources and DuckDB pushdown.

These tests exercise the real :class:`~selayer.sources.adapters.delta.DeltaAdapter`
and the registry-backed lifecycle for Delta Lake tables.  Fixtures create
deterministic Delta tables in ``tmp_path`` so every test is self-contained.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.dataset as padataset
import pytest
from deltalake import write_deltalake
from selayer.sources.adapters.delta import DeltaAdapter

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import DeltaConfig
from selayer.sources.errors import SourceDependencyError, SourceSchemaError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


_EVENTS_PA_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("value", pa.int64(), nullable=False),
    ]
)


def _events_table(rows: dict[str, list[int]]) -> pa.Table:
    """Build a non-nullable Arrow table matching the declared events schema."""

    return pa.Table.from_arrays(
        [pa.array(rows[field.name], field.type) for field in _EVENTS_PA_SCHEMA],
        schema=_EVENTS_PA_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Layer factory
# ---------------------------------------------------------------------------


@pytest.fixture
def delta_layer_factory() -> Callable[[str | Path], SemanticLayer]:
    """Return a SemanticLayer factory backed by a Delta source at *location*."""

    def factory(location: str | Path) -> SemanticLayer:
        return SemanticLayer(
            1,
            "delta_test",
            "",
            "",
            {
                "events": DataSource(
                    name="events",
                    connector=DeltaConfig(str(location)),
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
            {
                "total_value": Metric.from_expression(
                    "total_value", "total_value", ("total_value",)
                )
            },
            {},
        )

    return factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles() -> RuntimeProfileResolver:
    return MappingProfileResolver({})


@pytest.fixture
def providers() -> ArrowProviderResolver:
    return MappingArrowProviderResolver({})


# ---------------------------------------------------------------------------
# Step 1: reload publishes latest snapshot
# ---------------------------------------------------------------------------


def test_delta_reload_publishes_latest_snapshot(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    with QueryEngine(layer) as engine:
        first = engine.source_status("events")
        assert engine.query(["total_value"])["total_value"].item() == 10

        write_deltalake(
            location,
            _events_table({"id": [2], "value": [20]}),
            mode="append",
        )
        result = engine.reload_source("events")

        assert result.old_generation == first.generation
        assert result.snapshot != first.snapshot
        assert engine.query(["total_value"])["total_value"].item() == 30


# ---------------------------------------------------------------------------
# Registers a pyarrow Dataset
# ---------------------------------------------------------------------------


def test_delta_registers_pyarrow_dataset(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, providers)

    # The resource wraps a DeltaTable and a PyArrow Dataset; only the Dataset
    # is registered on the connection.
    dataset = handle.resource.dataset  # type: ignore[attr-defined]
    assert isinstance(dataset, padataset.Dataset)

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)
    assert connection.execute('SELECT sum("value") FROM "events"').fetchone() == (10,)
    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# DuckDB Arrow scan pushdown (projection + filter)
# ---------------------------------------------------------------------------


def test_delta_explain_contains_arrow_scan_projection_and_filter(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1, 2, 3], "value": [5, 15, 3]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, providers)

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)

    explain = "\n".join(
        row[1]
        for row in connection.execute(
            'EXPLAIN SELECT "id" FROM "events" WHERE "value" > 10'
        ).fetchall()
    )

    assert "ARROW_SCAN" in explain
    assert "id" in explain
    assert "value" in explain
    assert connection.execute(
        'SELECT "id" FROM "events" WHERE "value" > 10'
    ).fetchall() == [(2,)]

    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# Schema mismatch preserves old snapshot
# ---------------------------------------------------------------------------


def test_delta_schema_mismatch_preserves_old_snapshot(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """An observed schema with an extra column is rejected; the old generation
    remains queryable and no location/profile secret reaches any error surface."""

    # The location path deliberately carries a ``secret`` token so the
    # sanitized-error assertions are meaningful.
    location = tmp_path / "events_secret"
    write_deltalake(location, _events_table({"id": [1, 2, 3], "value": [1, 2, 3]}))

    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        SemanticLayer(
            1,
            "drift",
            "",
            "",
            {
                "events": DataSource(
                    name="events",
                    connector=DeltaConfig(str(location)),
                    schema=_events_schema(),
                    grain=("id",),
                )
            },
            {},
            {},
            {},
            {},
            {},
        ),
        connection,
        profiles,
        providers,
    )

    assert registry.status("events").generation == 1
    assert registry.execute('SELECT sum("id") FROM "events"').fetchone() == (6,)

    # Overwrite the Delta table with a drifted schema (extra column).
    drifted_schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("value", pa.int64(), nullable=False),
            pa.field("extra", pa.int64(), nullable=False),
        ]
    )
    write_deltalake(
        location,
        pa.table(
            {"id": [1, 2, 3], "value": [1, 2, 3], "extra": [9, 9, 9]},
            schema=drifted_schema,
        ),
        mode="overwrite",
        schema_mode="overwrite",
    )

    with pytest.raises(SourceSchemaError) as caught:
        registry.reload_source("events")

    assert caught.value.code == "schema_mismatch"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)

    # The failed reload did not swap the registration: the generation is
    # unchanged and the previously registered data is still queryable.
    assert registry.status("events").generation == 1
    assert registry.execute('SELECT sum("id") FROM "events"').fetchone() == (6,)

    registry.close()


# ---------------------------------------------------------------------------
# Missing deltalake extra surfaces as SourceDependencyError
# ---------------------------------------------------------------------------


def test_missing_deltalake_extra_is_source_dependency_error(
    monkeypatch,
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events_secret"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )

    # Simulate the optional ``delta`` extra not being installed.
    monkeypatch.setattr("selayer.sources.adapters.delta._DeltaTable", None)

    adapter = DeltaAdapter()
    with pytest.raises(SourceDependencyError) as caught:
        adapter.prepare(source, profiles, providers)

    assert caught.value.code == "missing_delta_dependency"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)


# ---------------------------------------------------------------------------
# S3 profile builds filesystem
# ---------------------------------------------------------------------------


def test_delta_s3_profile_builds_filesystem(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A delta source with ``credential_profile`` resolves an S3 filesystem.

    The filesystem is passed to ``DeltaTable.to_pyarrow_dataset``.  A
    :class:`pyarrow.fs.SubTreeFileSystem` rooted at the table directory stands
    in for S3 so the test needs no Docker — the relative file paths in the
    Delta log resolve through the subtree root.
    """

    import pyarrow.fs as pafs

    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    resolved_profiles: list[str] = []

    def fake_s3_filesystem(_profile: object) -> pafs.FileSystem:
        resolved_profiles.append("s3_profile")
        return pafs.SubTreeFileSystem(str(location), pafs.LocalFileSystem())

    monkeypatch.setattr(
        "selayer.sources.adapters.delta.s3_filesystem", fake_s3_filesystem
    )

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location), credential_profile="s3_profile"),
        schema=_events_schema(),
        grain=("id",),
    )
    profiles = MappingProfileResolver(
        {"s3_profile": {"access_key": "AKIA_SECRET", "secret_key": "shh_secret"}}
    )

    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, MappingArrowProviderResolver({}))

    assert resolved_profiles == ["s3_profile"]

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)
    assert connection.execute('SELECT sum("value") FROM "events"').fetchone() == (10,)
    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# Status contains integer version only
# ---------------------------------------------------------------------------


def test_delta_status_contains_integer_version_only(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    with QueryEngine(layer) as engine:
        status = engine.source_status("events")
        snapshot = status.snapshot
        assert snapshot is not None
        # The snapshot is a bare integer version string — no path, URI, or
        # other detail.
        assert snapshot.isdigit()
        assert "/" not in snapshot
        assert ":" not in snapshot
        assert "." not in snapshot

        # After an append the version increments and the snapshot changes.
        write_deltalake(
            location,
            _events_table({"id": [2], "value": [20]}),
            mode="append",
        )
        engine.reload_source("events")
        new_status = engine.source_status("events")
        assert new_status.snapshot is not None
        assert new_status.snapshot.isdigit()
        assert new_status.snapshot != snapshot


# ---------------------------------------------------------------------------
# Close after reload and engine close
# ---------------------------------------------------------------------------


def test_delta_handles_close_after_reload_and_engine_close(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    engine = QueryEngine(layer)
    assert engine.query(["total_value"])["total_value"].item() == 10

    write_deltalake(
        location,
        _events_table({"id": [2], "value": [20]}),
        mode="append",
    )
    engine.reload_source("events")
    assert engine.query(["total_value"])["total_value"].item() == 30

    # Closing the engine closes every handle (old + new) and the connection
    # without raising.
    engine.close()
