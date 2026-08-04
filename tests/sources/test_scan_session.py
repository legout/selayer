"""Bounded public source scan-session contract tests.

These tests pin the public scan-session contract produced by Task 4:

* :class:`SourceConsistency`, :class:`SourceSnapshot`, and
  :class:`SourceScanSession` are public immutable types.
* :meth:`SourceRegistry.open_scan_session` opens a bounded, context-managed
  session that streams typed Arrow batches, holds the registry lifecycle lock
  for its full lifetime (blocking reload/close/query-binding/execute on the
  same registry), exposes no raw connection or handle, accepts no credential
  override, and sanitizes every DuckDB/adapter failure into a stable
  :class:`SourceError` code.

They use a self-contained fake adapter and a real in-memory DuckDB connection
so the streamed batches observe the live registered handle state.
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from selayer.model import DataSource
from selayer.sources.base import (
    QueryBinding,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.errors import (
    SourceConnectionError,
    SourceError,
)
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.scan import (
    SourceConsistency,
    SourceSnapshot,
)
from selayer.sources.schema import (
    FieldSchema,
    ScalarType,
    TableSchema,
    schema_fingerprint,
)

# ---------------------------------------------------------------------------
# Schemas / sources / layer
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


def _events_source() -> DataSource:
    return DataSource(
        name="events",
        connector=_pyarrow_connector("events"),
        schema=_events_schema(),
        grain=("id",),
    )


def _pyarrow_connector(handle: str) -> Any:
    from selayer.sources.config import PyArrowConfig

    return PyArrowConfig(handle)


def _layer(*sources: DataSource) -> Any:
    from selayer.catalog import SemanticLayer

    source_map = {s.name: s for s in sources}
    return SemanticLayer(1, "test", "", "", source_map, {}, {}, {}, {}, {})


# ---------------------------------------------------------------------------
# Tracking connection
# ---------------------------------------------------------------------------


class _TrackingConnection:
    """DuckDB connection wrapper recording register/unregister/close."""

    def __init__(self) -> None:
        self._conn = duckdb.connect(":memory:")
        self.closed = False
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(self, name: str, obj: object) -> None:
        self.registered.append(name)
        self._conn.register(name, obj)

    def unregister(self, name: str) -> None:
        self.unregistered.append(name)
        self._conn.unregister(name)

    def sql(self, query: str) -> Any:
        return self._conn.sql(query)

    def execute(self, sql: str, *parameters: object) -> Any:
        return self._conn.execute(sql, *parameters)

    def close(self) -> None:
        self.closed = True
        self._conn.close()


class _CloseCountingReader:
    """Proxy ``RecordBatchReader`` that counts ``close()`` invocations."""

    def __init__(self, inner: pa.RecordBatchReader) -> None:
        self._inner = inner
        self.close_count = 0

    def read_next_batch(self) -> pa.RecordBatch:
        return self._inner.read_next_batch()

    def close(self) -> None:
        self.close_count += 1
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ReaderTrackingResult:
    """Proxy over a DuckDB result exposing a close-counting reader factory."""

    def __init__(self, inner: Any, readers: list[_CloseCountingReader]) -> None:
        self._inner = inner
        self._readers = readers

    def to_arrow_reader(self, batch_size: int = 1024) -> _CloseCountingReader:
        wrapper = _CloseCountingReader(
            self._inner.to_arrow_reader(batch_size=batch_size)
        )
        self._readers.append(wrapper)
        return wrapper


class _ReaderTrackingConnection(_TrackingConnection):
    """Connection whose scan readers are wrapped to count ``close()``."""

    def __init__(self) -> None:
        super().__init__()
        self.readers: list[_CloseCountingReader] = []

    def execute(self, sql: str, *parameters: object) -> Any:
        result = self._conn.execute(sql, *parameters)
        if "FROM" in sql:
            return _ReaderTrackingResult(result, self.readers)
        return result


# ---------------------------------------------------------------------------
# Fake provider / adapter
# ---------------------------------------------------------------------------


class _Provider:
    """Zero-arg provider factory returning a fresh Arrow object."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table
        self.invoke_count = 0

    def __call__(self) -> ArrowObject:
        self.invoke_count += 1
        return self._table


class _ScanAdapter:
    """Fake adapter with mutable consistency/snapshot controls.

    ``snapshot`` and ``consistency`` are read on every ``prepare`` so the test
    can mutate them between operations (status / reload / recheck).
    """

    def __init__(
        self,
        provider: _Provider,
        *,
        snapshot: str | None = None,
        consistency: SourceConsistency = SourceConsistency.LIVE,
    ) -> None:
        self._provider = provider
        self.snapshot: str | None = snapshot
        self.consistency: SourceConsistency = consistency
        self.prepare_count = 0
        self.closed: list[str] = []
        self.bind_count = 0
        self.bind_raise: BaseException | None = None

    def prepare(
        self,
        source: Any,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        factory = arrow_providers.resolve(
            source.connector.handle, source_id=source.name
        )
        resource = factory()
        self.prepare_count += 1
        query_scoped = isinstance(resource, pa.RecordBatchReader)
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=self.snapshot,
            query_scoped=query_scoped,
            consistency=self.consistency,
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]

    def bind_query(
        self,
        connection: object,
        handle: SourceHandle,
        requirement: SourceScanRequirement,
    ) -> QueryBinding | None:
        if not handle.query_scoped:
            return None
        self.bind_count += 1
        if self.bind_raise is not None:
            failure = self.bind_raise
            self.bind_raise = None
            raise failure
        fresh = self._provider()
        connection.register(handle.source_id, fresh)  # type: ignore[attr-defined]

        def cleanup() -> None:
            try:
                connection.unregister(handle.source_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110
                pass

        return QueryBinding(
            source_id=handle.source_id,
            stable_name=handle.source_id,
            cleanup=cleanup,
        )

    def close(self, handle: SourceHandle) -> None:
        self.closed.append(handle.source_id)


# ---------------------------------------------------------------------------
# Builders / fixtures
# ---------------------------------------------------------------------------


def _build(
    *,
    provider_table: pa.Table | None = None,
    snapshot: str | None = None,
    consistency: SourceConsistency = SourceConsistency.LIVE,
) -> tuple[SourceRegistry, _TrackingConnection, _ScanAdapter, _Provider]:
    table = provider_table or pa.table({"id": [1, 2, 3], "value": [10, 20, 30]})
    provider = _Provider(table)
    adapter = _ScanAdapter(provider, snapshot=snapshot, consistency=consistency)
    connection = _TrackingConnection()
    registry = SourceRegistry.create(
        _layer(_events_source()),
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({"events": provider}),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    return registry, connection, adapter, provider


def _build_query_scoped() -> (
    tuple[SourceRegistry, _TrackingConnection, _ScanAdapter, _Provider]
):
    """A registry whose provider returns a single-pass ``RecordBatchReader``."""

    table = pa.table({"id": [1, 2, 3], "value": [10, 20, 30]})

    class _ReaderProvider:
        def __init__(self) -> None:
            self.invoke_count = 0

        def __call__(self) -> ArrowObject:
            self.invoke_count += 1
            return pa.RecordBatchReader.from_batches(
                table.schema, table.to_batches()
            )

    provider = _ReaderProvider()
    adapter = _ScanAdapter(provider)  # type: ignore[arg-type]
    connection = _TrackingConnection()
    registry = SourceRegistry.create(
        _layer(_events_source()),
        connection,
        MappingProfileResolver({}),
        MappingArrowProviderResolver({"events": provider}),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    return registry, connection, adapter, provider  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Step 3: public immutable scan types
# ---------------------------------------------------------------------------


def test_source_consistency_is_closed_str_enum() -> None:
    assert SourceConsistency.REOPENABLE_SNAPSHOT.value == "reopenable_snapshot"
    assert SourceConsistency.TRANSACTION_SNAPSHOT.value == "transaction_snapshot"
    assert SourceConsistency.LIVE.value == "live"
    members = {member.value for member in SourceConsistency}
    assert members == {
        "reopenable_snapshot",
        "transaction_snapshot",
        "live",
    }


def test_source_snapshot_is_frozen_and_slotted() -> None:
    snapshot = SourceSnapshot(
        consistency=SourceConsistency.TRANSACTION_SNAPSHOT,
        snapshot_id="tx-1",
        schema_fingerprint="abc123",
    )
    assert snapshot.consistency is SourceConsistency.TRANSACTION_SNAPSHOT
    assert snapshot.snapshot_id == "tx-1"
    assert snapshot.schema_fingerprint == "abc123"
    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_id = "other"  # type: ignore[misc]
    assert not hasattr(snapshot, "__dict__")


# ---------------------------------------------------------------------------
# Step 1: known-column and batch-size validation
# ---------------------------------------------------------------------------


def test_open_scan_session_validates_known_columns() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id", "value")) as session:
        batches = list(session.iter_batches())
    assert sum(b.num_rows for b in batches) == 3


def test_open_scan_session_rejects_unknown_column() -> None:
    registry, *_ = _build()
    with pytest.raises(SourceConnectionError) as caught:
        registry.open_scan_session("events", columns=("secret",))
    assert caught.value.code == "scan_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    # The lock must have been released after the failed open.
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


@pytest.mark.parametrize("bad_size", [0, -1, 1.0, "1024", True])
def test_open_scan_session_validates_batch_size(bad_size: object) -> None:
    registry, *_ = _build()
    with pytest.raises(SourceConnectionError) as caught:
        registry.open_scan_session(
            "events", columns=("id",), batch_size=bad_size  # type: ignore[arg-type]
        )
    assert caught.value.code == "scan_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


def test_open_scan_session_unknown_source_is_sanitized() -> None:
    registry, *_ = _build()
    with pytest.raises(SourceConnectionError) as caught:
        registry.open_scan_session("missing", columns=("id",))
    assert caught.value.code == "connect_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


# ---------------------------------------------------------------------------
# Step 1: typed pyarrow.RecordBatch iteration
# ---------------------------------------------------------------------------


def test_iter_batches_yields_typed_record_batches() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id", "value")) as session:
        batches = list(session.iter_batches())
    assert batches, "expected at least one batch"
    for batch in batches:
        assert isinstance(batch, pa.RecordBatch)
    total_ids: list[int] = []
    for batch in batches:
        total_ids.extend(batch.column("id").to_pylist())
    assert sorted(total_ids) == [1, 2, 3]


def test_iter_batches_projects_selected_columns() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("value",)) as session:
        batch = next(session.iter_batches())
    assert batch.schema.names == ["value"]


def test_open_scan_session_defaults_to_all_columns() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events") as session:
        batch = next(session.iter_batches())
    assert set(batch.schema.names) == {"id", "value"}


# ---------------------------------------------------------------------------
# Step 1: one active iterator per session
# ---------------------------------------------------------------------------


def test_only_one_active_iterator_per_session() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id",)) as session:
        first = session.iter_batches()
        next(first)
        with pytest.raises(SourceConnectionError) as caught:
            session.iter_batches()
        assert caught.value.code == "scan_failed"
        # Drain the first so the flag resets.
        list(first)
        # A fresh iterator is allowed after the previous one drained.
        list(session.iter_batches())


# ---------------------------------------------------------------------------
# Step 1: cancellation
# ---------------------------------------------------------------------------


def test_cancel_interrupts_and_marks_session_unusable() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id",)) as session:
        iterator = session.iter_batches()
        next(iterator)
        session.cancel()
        # The session is unusable after cancellation.
        with pytest.raises(SourceConnectionError):
            next(session.iter_batches())
        # The in-flight iterator ends without surfacing a raw driver error.
        rest = list(iterator)
        assert rest == []
    # After context exit the registry lock is released.
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


def test_cancel_is_idempotent() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id",)) as session:
        session.cancel()
        session.cancel()  # second cancel is a no-op


# ---------------------------------------------------------------------------
# Step 1: session cleanup after success and failure
# ---------------------------------------------------------------------------


def _lock_is_held(registry: SourceRegistry) -> bool:
    """Return ``True`` if the registry lifecycle lock is currently held.

    Probes from a *separate* thread: the registry lock is a reentrant
    :class:`threading.RLock`, so a same-thread ``acquire(blocking=False)``
    always succeeds (incrementing the recursion level) and cannot detect an
    already-held lock.  A different thread's non-blocking acquire fails when
    the lock is held by the session's owning thread, which is exactly the
    "the lock blocks the same registry" invariant the scan session must hold.
    """

    blocked = threading.Event()

    def probe() -> None:
        if not registry._lock.acquire(blocking=False):
            blocked.set()
        else:
            registry._lock.release()

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    probe_thread.join()
    return blocked.is_set()


def test_session_cleanup_after_success_releases_lock() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id",)) as session:
        list(session.iter_batches())
        # The lifecycle lock is held for the full session lifetime.
        assert _lock_is_held(registry)
    # Lock released on exit.
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()
    # The registry is still usable afterwards.
    assert registry.status("events").source_id == "events"


def test_session_cleanup_after_reader_failure_releases_lock() -> None:
    registry, *_ = _build()

    class _BadConnection(_TrackingConnection):
        def execute(self, sql: str, *parameters: object) -> Any:
            if "FROM" in sql:
                raise RuntimeError("injected scan failure echoing SECRET")
            return super().execute(sql, *parameters)

    registry._connection = _BadConnection()  # type: ignore[attr-defined]
    # Re-register the existing handle under the bad connection.
    handle = registry._registrations["events"].handle
    registry._connection.register("events", handle.resource)

    with pytest.raises(SourceConnectionError) as caught:
        registry.open_scan_session("events", columns=("id",))
    assert caught.value.code == "scan_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SECRET" not in repr(caught.value)
    # Lock released even though the reader failed.
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


def test_session_cleanup_after_binding_failure_releases_lock() -> None:
    registry, _, adapter, _ = _build_query_scoped()
    adapter.bind_raise = RuntimeError("injected bind failure echoing TOKEN")
    with pytest.raises(SourceConnectionError) as caught:
        registry.open_scan_session("events", columns=("id",))
    assert caught.value.code == "bind_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "TOKEN" not in repr(caught.value)
    assert registry._lock.acquire(blocking=False)
    registry._lock.release()


# ---------------------------------------------------------------------------
# Step 1: registry operations block while a scan session owns the lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        "reload",
        "close",
        "bind",
        "execute",
    ],
)
def test_registry_operation_blocks_while_scan_session_holds_lock(
    operation: str,
) -> None:
    registry, *_ = _build()
    entered = threading.Event()
    can_exit = threading.Event()
    completed = threading.Event()

    def holder() -> None:
        with registry.open_scan_session("events", columns=("id",)):
            entered.set()
            can_exit.wait(timeout=5)

    def worker() -> None:
        if operation == "reload":
            registry.reload_source("events")
        elif operation == "close":
            registry.close()
        elif operation == "bind":
            from selayer.planning import QueryPlan

            with registry.bind(QueryPlan("events", (), (), (), (), ())):
                pass
        elif operation == "execute":
            registry.execute("SELECT 1")
        completed.set()

    holder_thread = threading.Thread(target=holder)
    worker_thread = threading.Thread(target=worker)
    holder_thread.start()
    assert entered.wait(timeout=5)
    worker_thread.start()
    time.sleep(0.2)
    # The operation must be blocked while the scan session holds the lock.
    assert not completed.is_set()

    can_exit.set()
    holder_thread.join(timeout=5)
    worker_thread.join(timeout=5)
    assert completed.is_set()


# ---------------------------------------------------------------------------
# Step 1: discovery profiling uses a dedicated registry
# ---------------------------------------------------------------------------


def test_dedicated_registry_is_not_blocked_by_profile_scan_session() -> None:
    """A scan session on one registry does not block another registry's lock."""

    profile_registry, *_ = _build()
    query_registry, *_ = _build()

    entered = threading.Event()
    can_exit = threading.Event()
    executed = threading.Event()

    def profiler() -> None:
        with profile_registry.open_scan_session("events", columns=("id",)):
            entered.set()
            can_exit.wait(timeout=5)

    def querier() -> None:
        query_registry.execute("SELECT 1")
        executed.set()

    profiler_thread = threading.Thread(target=profiler)
    querier_thread = threading.Thread(target=querier)
    profiler_thread.start()
    assert entered.wait(timeout=5)
    querier_thread.start()
    # The separate registry's lock is independent; the query runs immediately.
    querier_thread.join(timeout=5)
    assert executed.is_set()

    can_exit.set()
    profiler_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Step 1: environment-backed resolver, no profile values exposed
# ---------------------------------------------------------------------------


def test_open_scan_session_uses_registry_resolver_and_exposes_no_profile_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "TOKENONLYSECRET-VALUE"
    profiles = MappingProfileResolver({"s3": {"secret_key": secret_value}})
    table = pa.table({"id": [1], "value": [1]})
    provider = _Provider(table)
    adapter = _ScanAdapter(provider)
    connection = _TrackingConnection()
    registry = SourceRegistry.create(
        _layer(_events_source()),
        connection,
        profiles,
        MappingArrowProviderResolver({"events": provider}),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    # open_scan_session accepts no credential override.
    sig = inspect.signature(registry.open_scan_session)
    forbidden = {"profiles", "profile", "credentials", "credential"}
    assert not (forbidden & set(sig.parameters))

    with registry.open_scan_session("events", columns=("id",)) as session:
        # No public attribute exposes a profile or its values.
        public = {
            name: getattr(session, name)
            for name in dir(session)
            if not name.startswith("_")
        }
        for name, value in public.items():
            assert secret_value not in repr(value), (
                f"profile value leaked via session.{name}"
            )
        assert secret_value not in repr(session)


# ---------------------------------------------------------------------------
# Step 1: no public raw connection or handle attribute
# ---------------------------------------------------------------------------


def test_session_exposes_no_raw_connection_or_handle() -> None:
    registry, *_ = _build()
    with registry.open_scan_session("events", columns=("id",)) as session:
        public_names = {
            name for name in dir(session) if not name.startswith("_")
        }
        assert "connection" not in public_names
        assert "handle" not in public_names
        assert "resource" not in public_names
        # The public surface is exactly the bounded contract.
        assert public_names == {
            "cancel",
            "consistency",
            "iter_batches",
            "recheck_snapshot",
            "schema",
            "snapshot_id",
            "source_id",
        }
        assert session.source_id == "events"
        assert isinstance(session.schema, TableSchema)
        assert session.consistency is SourceConsistency.LIVE
        assert session.snapshot_id is None


# ---------------------------------------------------------------------------
# Step 4: status, reload, and scan sessions report the same canonical token
# ---------------------------------------------------------------------------


def test_status_reload_and_scan_session_share_canonical_snapshot_token() -> None:
    token = "file-set:abc"
    registry, _, adapter, _ = _build(snapshot=token)
    adapter.snapshot = token

    # status reads the registration handle's snapshot.
    assert registry.status("events").snapshot == token

    # reload publishes the freshly-prepared candidate's snapshot.
    reload = registry.reload_source("events")
    assert reload.snapshot == token

    # the scan session derives its snapshot_id from the same canonical token.
    with registry.open_scan_session("events", columns=("id",)) as session:
        assert session.snapshot_id == token
        assert registry.status("events").snapshot == token


def test_scan_session_reflects_handle_consistency() -> None:
    registry, *_ = _build(
        consistency=SourceConsistency.TRANSACTION_SNAPSHOT, snapshot="tx-9"
    )
    with registry.open_scan_session("events", columns=("id",)) as session:
        assert session.consistency is SourceConsistency.TRANSACTION_SNAPSHOT
        assert session.snapshot_id == "tx-9"


# ---------------------------------------------------------------------------
# Step 6: snapshot recheck
# ---------------------------------------------------------------------------


def test_recheck_snapshot_returns_fresh_candidate_view() -> None:
    registry, _, adapter, _ = _build(snapshot="snap-1")
    with registry.open_scan_session("events", columns=("id",)) as session:
        fresh = session.recheck_snapshot()
    assert isinstance(fresh, SourceSnapshot)
    assert fresh.snapshot_id == "snap-1"
    assert fresh.consistency is SourceConsistency.LIVE
    assert fresh.schema_fingerprint == schema_fingerprint(_events_schema())
    # The fresh candidate was closed after recheck.
    assert "events" in adapter.closed


def test_recheck_snapshot_detects_mismatched_token() -> None:
    registry, _, adapter, _ = _build(snapshot="snap-1")
    with registry.open_scan_session("events", columns=("id",)) as session:
        assert session.snapshot_id == "snap-1"
        adapter.snapshot = "snap-2"
        with pytest.raises(SourceConnectionError) as caught:
            session.recheck_snapshot()
        assert caught.value.code == "snapshot_mismatch"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        # The session itself still reflects the original token.
        assert session.snapshot_id == "snap-1"
    # The fresh candidate was still closed after the mismatch was detected.
    assert "events" in adapter.closed


def test_recheck_snapshot_detects_consistency_mismatch() -> None:
    registry, _, adapter, _ = _build(snapshot="snap-1")
    with registry.open_scan_session("events", columns=("id",)) as session:
        assert session.consistency is SourceConsistency.LIVE
        adapter.consistency = SourceConsistency.TRANSACTION_SNAPSHOT
        with pytest.raises(SourceConnectionError) as caught:
            session.recheck_snapshot()
        assert caught.value.code == "snapshot_mismatch"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_recheck_snapshot_detects_schema_mismatch() -> None:
    registry, _, adapter, _ = _build(snapshot="snap-1")
    drifted = TableSchema(
        (FieldSchema("id", ScalarType("int64"), False),)
    )

    def drift(_handle: SourceHandle) -> TableSchema:
        return drifted

    with registry.open_scan_session("events", columns=("id",)) as session:
        adapter.inspect_schema = drift  # type: ignore[method-assign]
        with pytest.raises(SourceConnectionError) as caught:
            session.recheck_snapshot()
        assert caught.value.code == "snapshot_mismatch"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    assert "events" in adapter.closed


def test_recheck_snapshot_sanitizes_adapter_failure() -> None:
    registry, _, adapter, _ = _build(snapshot="snap-1")

    def boom(_handle: SourceHandle) -> TableSchema:
        raise RuntimeError("injected recheck failure echoing CREDS")

    adapter.inspect_schema = boom  # type: ignore[method-assign]
    with registry.open_scan_session("events", columns=("id",)) as session, pytest.raises(
        SourceConnectionError
    ) as caught:
        session.recheck_snapshot()
    assert caught.value.code == "scan_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CREDS" not in repr(caught.value)


# ---------------------------------------------------------------------------
# Step 1: sanitized error codes (catch-all)
# ---------------------------------------------------------------------------


def test_all_scan_failures_surface_as_source_error() -> None:
    registry, *_ = _build()
    with pytest.raises(SourceError) as caught:
        registry.open_scan_session("events", columns=("nope",))
    assert isinstance(caught.value, SourceError)
    assert caught.value.operation_id  # UUIDv4 present


# ---------------------------------------------------------------------------
# Lifecycle idempotency: reader closed exactly once
# ---------------------------------------------------------------------------


def _reader_tracking_registry() -> (
    tuple[SourceRegistry, _ReaderTrackingConnection]
):
    registry, *_ = _build()
    conn = _ReaderTrackingConnection()
    registry._connection = conn  # type: ignore[attr-defined]
    handle = registry._registrations["events"].handle
    conn.register("events", handle.resource)
    return registry, conn


def test_reader_closed_exactly_once_on_cancel_then_exit() -> None:
    registry, conn = _reader_tracking_registry()
    with registry.open_scan_session("events", columns=("id",)) as session:
        session.cancel()
    assert len(conn.readers) == 1
    assert conn.readers[0].close_count == 1


def test_reader_closed_exactly_once_on_normal_exit() -> None:
    registry, conn = _reader_tracking_registry()
    with registry.open_scan_session("events", columns=("id",)) as session:
        list(session.iter_batches())
    assert len(conn.readers) == 1
    assert conn.readers[0].close_count == 1


def test_reader_closed_exactly_once_on_cancel_without_iteration() -> None:
    registry, conn = _reader_tracking_registry()
    with registry.open_scan_session("events", columns=("id",)) as session:
        session.cancel()
        session.cancel()  # idempotent — no second close
    assert len(conn.readers) == 1
    assert conn.readers[0].close_count == 1
