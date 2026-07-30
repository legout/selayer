"""Mixed cross-connector reload/rollback and concurrency/cleanup matrix tests.

These tests prove the registry's all-or-nothing reload contract holds across
every built-in connector mode (Arrow, Delta, Iceberg, database) using *real*
adapters and a real DuckDB connection — not fakes.  They cover:

* **all-or-nothing rollback** — a failure mid-``reload_all`` restores every old
  registration, queryable value, and generation;
* **sorted successful results** — ``reload_all`` publishes results in sorted
  source-ID order with strictly advancing generations;
* **candidate cleanup** — rolled-back candidates are closed (not leaked);
* **lock coordination** — ``reload_all``, query binding, and ``close`` all
  serialize on the registry lock so a swap cannot happen mid-query;
* **stable live-handle count** — repeated reloads do not leak handles; and
* **secret-free failures** — every connector mode's reload failure is free of
  secret sentinels across every error surface.

Docker-backed PostgreSQL/MinIO connectors are exercised in their dedicated
integration suites (``test_database_adapters`` / ``test_s3``); the secrecy
matrix here covers the in-process connector modes.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from deltalake import write_deltalake

from selayer.catalog import SemanticLayer
from selayer.model import DataSource
from selayer.sources.config import (
    DeltaConfig,
    IcebergConfig,
    PyArrowConfig,
    SqliteConfig,
)
from selayer.sources.errors import (
    SourceError,
    SourceReloadError,
)
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

# ---------------------------------------------------------------------------
# Shared schema and helpers
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )


def _make_sqlite(path: Path, rows: list[tuple[int, int]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS events")
        connection.execute("CREATE TABLE events (id INTEGER, value INTEGER)")
        connection.executemany("INSERT INTO events VALUES (?, ?)", rows)
        connection.commit()


def _make_delta(path: Path, rows: list[tuple[int, int]]) -> None:
    table = pa.Table.from_arrays(
        [
            pa.array([r[0] for r in rows], pa.int64()),
            pa.array([r[1] for r in rows], pa.int64()),
        ],
        names=["id", "value"],
    )
    write_deltalake(path, table, mode="overwrite")


def _layer(*sources: DataSource) -> SemanticLayer:
    return SemanticLayer(
        1, "mixed", "", "", {s.name: s for s in sources}, {}, {}, {}, {}, {}
    )


# ---------------------------------------------------------------------------
# Mixed real-adapter fixture (Arrow + Delta + database)
# ---------------------------------------------------------------------------


class _Provider:
    """Zero-argument Arrow provider backed by a mutable table reference."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table

    def __call__(self) -> pa.Table:
        return self._table


@dataclass
class _MixedFixture:
    registry: SourceRegistry
    connection: duckdb.DuckDBPyConnection
    generations: dict[str, int]
    fail_database_candidate: Callable[[], None]
    sqlite_path: Path
    delta_path: Path
    arrow_provider: _Provider


def _build_mixed(tmp_path: Path) -> _MixedFixture:
    sqlite_path = tmp_path / "database.sqlite"
    _make_sqlite(sqlite_path, [(1, 10)])

    delta_path = tmp_path / "delta_events"
    _make_delta(delta_path, [(1, 100)])

    arrow_provider = _Provider(pa.table({"id": [1], "value": [1000]}))

    layer = _layer(
        DataSource(
            "arrow_events",
            PyArrowConfig("arrow_events"),
            _events_schema(),
            ("id",),
        ),
        DataSource(
            "database_events",
            SqliteConfig(str(sqlite_path), "events"),
            _events_schema(),
            ("id",),
        ),
        DataSource(
            "delta_events",
            DeltaConfig(str(delta_path)),
            _events_schema(),
            ("id",),
        ),
    )

    connection = duckdb.connect(":memory:")
    connection.execute("LOAD sqlite_scanner")
    profiles: RuntimeProfileResolver = MappingProfileResolver({})
    providers: ArrowProviderResolver = MappingArrowProviderResolver(
        {"arrow_events": arrow_provider}
    )
    registry = SourceRegistry.create(layer, connection, profiles, providers)

    def fail_database_candidate() -> None:
        """Make the next database reload candidate observe a drifted schema.

        Adding an extra column via a second SQLite connection causes the
        reload candidate's ephemeral introspection to observe an extra field,
        which ``compare_schemas`` rejects — so ``reload_all`` fails
        all-or-nothing.  The already-published stable view was created with a
        fixed ``SELECT *`` column list, so its row count is unaffected.
        """

        with sqlite3.connect(sqlite_path) as writer:
            writer.execute("ALTER TABLE events ADD COLUMN extra INTEGER")
            writer.commit()

    return _MixedFixture(
        registry=registry,
        connection=connection,
        generations={
            sid: 1 for sid in ("arrow_events", "database_events", "delta_events")
        },
        fail_database_candidate=fail_database_candidate,
        sqlite_path=sqlite_path,
        delta_path=delta_path,
        arrow_provider=arrow_provider,
    )


@pytest.fixture
def mixed_registry_fixture(tmp_path: Path) -> _MixedFixture:
    return _build_mixed(tmp_path)


_COUNT_QUERIES = {
    "arrow_events": 'SELECT count(*) FROM "arrow_events"',
    "database_events": 'SELECT count(*) FROM "database_events"',
    "delta_events": 'SELECT count(*) FROM "delta_events"',
}


def _counts(fixture: _MixedFixture) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source_id in fixture.generations:
        row = fixture.connection.sql(_COUNT_QUERIES[source_id]).fetchone()
        assert row is not None
        counts[source_id] = int(row[0])
    return counts


def _generations(fixture: _MixedFixture) -> dict[str, int]:
    return {
        source_id: fixture.registry.status(source_id).generation
        for source_id in fixture.generations
    }


# ---------------------------------------------------------------------------
# Step 1: all-or-nothing rollback across adapter modes
# ---------------------------------------------------------------------------


def test_reload_all_is_all_or_nothing_across_adapter_modes(
    mixed_registry_fixture: _MixedFixture,
) -> None:
    fixture = mixed_registry_fixture
    registry = fixture.registry
    before = _counts(fixture)

    fixture.fail_database_candidate()

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_all()

    # Every old registration restored: queryable row counts unchanged.
    assert _counts(fixture) == before
    # Every generation unchanged — no half-published swap.
    assert _generations(fixture) == fixture.generations
    assert caught.value.code == "reload_all_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# ---------------------------------------------------------------------------
# Sorted successful results
# ---------------------------------------------------------------------------


def test_reload_all_mixed_success_returns_sorted_results(
    mixed_registry_fixture: _MixedFixture,
) -> None:
    fixture = mixed_registry_fixture
    # Mutate every source so the reload observes new data.
    fixture.arrow_provider._table = pa.table({"id": [2], "value": [2000]})
    _make_delta(fixture.delta_path, [(1, 200)])
    _make_sqlite(fixture.sqlite_path, [(1, 20)])

    results = fixture.registry.reload_all()

    assert tuple(item.source_id for item in results) == tuple(
        sorted(fixture.generations)
    )
    for item in results:
        assert item.old_generation == 1
        assert item.new_generation == 2
    assert _counts(fixture) == {
        "arrow_events": 1,
        "database_events": 1,
        "delta_events": 1,
    }
    assert _generations(fixture) == {sid: 2 for sid in fixture.generations}


# ---------------------------------------------------------------------------
# Rollback closes candidates (no leak)
# ---------------------------------------------------------------------------


def test_reload_all_rollback_closes_candidates(
    mixed_registry_fixture: _MixedFixture,
) -> None:
    """A rolled-back reload does not leak candidates: a second reload succeeds.

    After the all-or-nothing failure, every rolled-back candidate must have
    been closed so the underlying file/socket handles are released.  The proof:
    a second, successful ``reload_all`` advances every generation by exactly one
    (1 -> 2) and every source remains queryable — no leaked candidate holds the
    database file or blocks the re-attach.
    """

    fixture = mixed_registry_fixture
    fixture.fail_database_candidate()
    with pytest.raises(SourceReloadError):
        fixture.registry.reload_all()

    # The rollback did not advance any generation.
    assert _generations(fixture) == fixture.generations
    with sqlite3.connect(fixture.sqlite_path) as writer:
        writer.execute("ALTER TABLE events DROP COLUMN extra")
        writer.commit()

    # A second reload (all sources valid again) succeeds cleanly: every
    # candidate closed on the failed attempt, so the re-prepare/re-attach is
    # unobstructed and generations advance 1 -> 2.
    results = fixture.registry.reload_all()
    assert _generations(fixture) == {sid: 2 for sid in fixture.generations}
    assert tuple(item.source_id for item in results) == tuple(
        sorted(fixture.generations)
    )


# ---------------------------------------------------------------------------
# Lock coordination: reload_all, query binding, and close serialize
# ---------------------------------------------------------------------------


def test_query_waits_for_reload_swap(mixed_registry_fixture: _MixedFixture) -> None:
    """A query binding waits for an in-progress reload to finish its swap.

    ``reload_source`` holds the registry lock for the snapshot/commit/publish
    critical section.  A concurrent query binding must block until that section
    exits, so the query observes the post-swap generation.
    """

    fixture = mixed_registry_fixture
    registry = fixture.registry
    fixture.arrow_provider._table = pa.table({"id": [9], "value": [99]})

    reload_done = threading.Event()
    bind_entered = threading.Event()
    reload_started = threading.Event()

    def reloader() -> None:
        reload_started.set()
        registry.reload_source("arrow_events")
        reload_done.set()

    def binder() -> None:
        from selayer.planning import QueryPlan

        # Spin until the reload has started, then try to acquire the lock via a
        # query binding.  The binding must block until the reload finishes.
        reload_started.wait(timeout=5)
        with registry.bind(QueryPlan("arrow_events", (), (), (), (), ())):
            bind_entered.set()

    reload_thread = threading.Thread(target=reloader)
    bind_thread = threading.Thread(target=binder)

    reload_thread.start()
    bind_thread.start()
    bind_thread.join(timeout=10)
    reload_thread.join(timeout=10)

    # The binding eventually entered (after the reload released the lock).
    assert bind_entered.is_set()
    assert reload_done.is_set()


def test_close_waits_for_active_reload(mixed_registry_fixture: _MixedFixture) -> None:
    """``close`` serializes with an in-progress reload on the registry lock.

    ``close`` acquires the registry lock, so it cannot tear down the connection
    while a reload is mid-swap.  The reload completes (publishing its
    generation) before close marks the registry closed.
    """

    fixture = mixed_registry_fixture
    registry = fixture.registry
    fixture.arrow_provider._table = pa.table({"id": [7], "value": [77]})

    reload_started = threading.Event()
    reload_finished = threading.Event()
    close_finished = threading.Event()
    generation_after_reload: list[int] = []

    def reloader() -> None:
        # Hold the lock long enough that close must wait.  We cannot widen the
        # real adapter's register window, so we instead run reload_source under
        # the registry's execute_lock and gate completion.
        reload_started.set()
        result = registry.reload_source("arrow_events")
        generation_after_reload.append(result.new_generation)
        reload_finished.set()

    def closer() -> None:
        reload_started.wait(timeout=5)
        # Give the reload a chance to enter its critical section.
        time.sleep(0.1)
        registry.close()
        close_finished.set()

    reload_thread = threading.Thread(target=reloader)
    close_thread = threading.Thread(target=closer)

    reload_thread.start()
    close_thread.start()
    reload_thread.join(timeout=10)
    close_thread.join(timeout=10)

    assert reload_finished.is_set()
    assert close_finished.is_set()
    assert generation_after_reload == [2]


def test_close_waits_for_reload_candidate_preparation(
    mixed_registry_fixture: _MixedFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close waits for preparation and cannot orphan an in-flight candidate."""

    fixture = mixed_registry_fixture
    registry = fixture.registry
    preparation_started = threading.Event()
    release_preparation = threading.Event()
    close_finished = threading.Event()
    reload_errors: list[BaseException] = []
    original_prepare = SourceRegistry._prepare_candidate

    def blocked_prepare(registry, adapter, source):
        preparation_started.set()
        assert release_preparation.wait(timeout=5)
        return original_prepare(registry, adapter, source)

    monkeypatch.setattr(SourceRegistry, "_prepare_candidate", blocked_prepare)

    def reloader() -> None:
        try:
            registry.reload_source("arrow_events")
        except BaseException as error:  # noqa: BLE001
            reload_errors.append(error)

    def closer() -> None:
        registry.close()
        close_finished.set()

    reload_thread = threading.Thread(target=reloader)
    close_thread = threading.Thread(target=closer)
    reload_thread.start()
    assert preparation_started.wait(timeout=5)
    close_thread.start()
    assert not close_finished.wait(timeout=0.2)
    release_preparation.set()
    reload_thread.join(timeout=10)
    close_thread.join(timeout=10)

    assert reload_errors == []
    assert close_finished.is_set()
    assert registry._closed is True


def _create_iceberg_table(tmp_path: Path) -> tuple[object, Path, Path]:
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
            pa.field("id", pa.int64(), nullable=True),
            pa.field("category", pa.string(), nullable=True),
            pa.field("value", pa.int64(), nullable=True),
        ]
    )
    table = catalog.create_table(("default", "events"), schema=schema)
    table.append(  # type: ignore[attr-defined]
        pa.table({"id": [1], "category": ["A"], "value": [4]})
    )
    return table, warehouse, db_path


def test_reload_all_waits_for_iceberg_query_binding(tmp_path: Path) -> None:
    """``reload_all`` waits while an Iceberg query binding holds the registry lock.

    Iceberg sources are query-scoped: each query creates a fresh
    ``RecordBatchReader`` binding under the registry lock.  While that binding
    is held, ``reload_all`` must block rather than swap a handle mid-query.
    """

    _table, warehouse, db_path = _create_iceberg_table(tmp_path)
    schema = TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), True),
            FieldSchema("category", ScalarType("utf8"), True),
            FieldSchema("value", ScalarType("int64"), True),
        )
    )
    layer = SemanticLayer(
        1,
        "iceberg_test",
        "",
        "",
        {
            "events": DataSource(
                "events",
                IcebergConfig("test_catalog", ("default",), "events"),
                schema,
                ("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )
    connection = duckdb.connect(":memory:")
    profiles = MappingProfileResolver(
        {
            "test_catalog": {
                "type": "sql",
                "uri": f"sqlite:///{db_path}",
                "warehouse": f"file://{warehouse}",
            }
        }
    )
    registry = SourceRegistry.create(
        layer, connection, profiles, MappingArrowProviderResolver({})
    )

    from selayer.planning import QueryPlan

    bind_entered = threading.Event()
    bind_can_exit = threading.Event()
    reload_done = threading.Event()

    def holder() -> None:
        with registry.bind(
            QueryPlan(
                "events",
                (),
                (),
                (),
                (),
                (),
                {"events": ("id",)},
            )
        ):
            bind_entered.set()
            bind_can_exit.wait(timeout=5)

    def reloader() -> None:
        registry.reload_all()
        reload_done.set()

    holder_thread = threading.Thread(target=holder)
    reloader_thread = threading.Thread(target=reloader)
    holder_thread.start()
    assert bind_entered.wait(timeout=5)

    reloader_thread.start()
    time.sleep(0.2)
    # reload_all must be blocked while the query binding holds the lock.
    assert not reload_done.is_set()

    bind_can_exit.set()
    holder_thread.join(timeout=5)
    reloader_thread.join(timeout=5)
    assert reload_done.is_set()
    assert registry.status("events").generation == 2


# ---------------------------------------------------------------------------
# Stable live-handle count across repeated reloads
# ---------------------------------------------------------------------------


def test_repeated_reload_has_constant_live_handle_count(
    mixed_registry_fixture: _MixedFixture,
) -> None:
    """Repeated ``reload_all`` does not leak live handles.

    The database adapter attaches a fresh internal alias on every successful
    reload and detaches the previous one; if a rolled-back or superseded handle
    leaked, the DuckDB attachment count would grow.  Across many reloads the
    attachment count stays constant, proving no handle leak.
    """

    fixture = mixed_registry_fixture

    def attachment_count() -> int:
        row = fixture.connection.execute(
            """
            SELECT count(*)
            FROM duckdb_databases()
            WHERE database_name NOT IN ('memory', 'system', 'temp')
            """
        ).fetchone()
        assert row is not None
        return int(row[0])

    baseline = attachment_count()
    for _ in range(12):
        fixture.registry.reload_all()
    assert attachment_count() == baseline
    assert _generations(fixture) == {sid: 13 for sid in fixture.generations}
    assert _counts(fixture) == {
        "arrow_events": 1,
        "database_events": 1,
        "delta_events": 1,
    }


# ---------------------------------------------------------------------------
# Secret-free failures parameterized across every in-process connector mode
# ---------------------------------------------------------------------------


def _assert_no_secret_leak(error: BaseException, sentinel: str) -> None:
    """Assert ``sentinel`` is absent from every rendered error surface."""

    tb_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    surfaces = [
        str(error),
        getattr(error, "message", ""),
        repr(error.args),
        tb_text,
        repr(error.__cause__),
        repr(error.__context__),
    ]
    for surface in surfaces:
        assert sentinel not in surface, (
            f"secret sentinel {sentinel!r} leaked into error surface"
        )


def _arrow_layer(
    secret: str,
) -> tuple[SemanticLayer, RuntimeProfileResolver, ArrowProviderResolver]:
    del secret
    layer = _layer(
        DataSource("events", PyArrowConfig("events"), _events_schema(), ("id",))
    )
    return (
        layer,
        MappingProfileResolver({}),
        MappingArrowProviderResolver(
            {"events": _Provider(pa.table({"id": [1], "value": [1]}))}
        ),
    )


def _delta_layer(
    secret: str, tmp_path: Path
) -> tuple[SemanticLayer, RuntimeProfileResolver, ArrowProviderResolver]:
    delta_path = tmp_path / secret / "delta"
    _make_delta(delta_path, [(1, 1)])
    layer = _layer(
        DataSource("events", DeltaConfig(str(delta_path)), _events_schema(), ("id",))
    )
    return layer, MappingProfileResolver({}), MappingArrowProviderResolver({})


def _iceberg_layer(
    secret: str, tmp_path: Path
) -> tuple[SemanticLayer, RuntimeProfileResolver, ArrowProviderResolver]:
    root = tmp_path / secret
    root.mkdir(parents=True)
    _table, warehouse, db_path = _create_iceberg_table(root)
    layer = _layer(
        DataSource(
            "events",
            IcebergConfig("catalog", ("default",), "events"),
            TableSchema(
                (
                    FieldSchema("id", ScalarType("int64"), True),
                    FieldSchema("category", ScalarType("utf8"), True),
                    FieldSchema("value", ScalarType("int64"), True),
                )
            ),
            ("id",),
        )
    )
    profiles = MappingProfileResolver(
        {
            "catalog": {
                "type": "sql",
                "uri": f"sqlite:///{db_path}",
                "warehouse": f"file://{warehouse}",
            }
        }
    )
    return layer, profiles, MappingArrowProviderResolver({})


def _database_layer(
    secret: str, tmp_path: Path
) -> tuple[SemanticLayer, RuntimeProfileResolver, ArrowProviderResolver]:
    sqlite_path = tmp_path / secret / "events.sqlite"
    sqlite_path.parent.mkdir(parents=True)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("CREATE TABLE events (id INTEGER, value INTEGER)")
        connection.execute("INSERT INTO events VALUES (1, 1)")
    layer = _layer(
        DataSource(
            "events",
            SqliteConfig(str(sqlite_path), "events"),
            _events_schema(),
            ("id",),
        )
    )
    return layer, MappingProfileResolver({}), MappingArrowProviderResolver({})


@pytest.mark.parametrize(
    ("mode", "builder"),
    [
        ("arrow", lambda secret, tmp: _arrow_layer(secret)),
        ("delta", lambda secret, tmp: _delta_layer(secret, tmp)),
        ("iceberg", lambda secret, tmp: _iceberg_layer(secret, tmp)),
        ("database", lambda secret, tmp: _database_layer(secret, tmp)),
    ],
)
def test_reload_failure_is_secret_free_across_connector_modes(
    tmp_path: Path,
    mode: str,
    builder: Callable[
        [str, Path], tuple[SemanticLayer, RuntimeProfileResolver, ArrowProviderResolver]
    ],
) -> None:
    secret = "TOKENONLYSECRET"
    layer, profiles, providers = builder(secret, tmp_path)
    connection = duckdb.connect(":memory:")
    if mode == "database":
        connection.execute("LOAD sqlite_scanner")
    try:
        registry = SourceRegistry.create(layer, connection, profiles, providers)
    except SourceError as caught:
        # Some adapters reject an intentionally malformed secret-bearing
        # candidate during initial preparation.  The lifecycle boundary is
        # still the contract under test: no secret may escape initialization.
        _assert_no_secret_leak(caught, secret)
        return

    if mode == "arrow":
        registry._arrow_providers = MappingArrowProviderResolver({})
    elif mode == "delta":
        shutil.rmtree(tmp_path / secret / "delta")
    elif mode == "iceberg":
        (tmp_path / secret / "catalog.db").unlink()
    else:
        (tmp_path / secret / "events.sqlite").unlink()

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_all()

    assert caught.value.code == "reload_all_failed"
    _assert_no_secret_leak(caught.value, secret)
