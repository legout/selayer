"""Substantive registry lifecycle tests with fake adapters and providers.

These tests exercise the candidate-first atomic swap guarantees of
:class:`~selayer.sources.registry.SourceRegistry` using a controllable fake
adapter and a scripted provider.  A real DuckDB connection backs every
registration so SQL queries observe the live handle state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pytest

from selayer.model import DataSource
from selayer.planning import QueryPlan
from selayer.sources.base import (
    QueryBinding,
    ReloadResult,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.config import (
    DuckDbConfig,
    IcebergConfig,
    PostgresConfig,
    PyArrowConfig,
    SqliteConfig,
)
from selayer.sources.errors import (
    SourceConnectionError,
    SourceReloadError,
)
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
    table_schema_from_arrow,
)

# ---------------------------------------------------------------------------
# Schemas and sources
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
        connector=PyArrowConfig("events"),
        schema=_events_schema(),
        grain=("id",),
    )


def _named_source(name: str) -> DataSource:
    return DataSource(
        name=name,
        connector=PyArrowConfig(name),
        schema=_events_schema(),
        grain=("id",),
    )


def _layer(*sources: DataSource):
    """Build a minimal :class:`SemanticLayer`-shaped namespace for the registry."""

    from selayer.catalog import SemanticLayer

    source_map = {s.name: s for s in sources}
    return SemanticLayer(1, "test", "", "", source_map, {}, {}, {}, {}, {})


# ---------------------------------------------------------------------------
# Tracking connection
# ---------------------------------------------------------------------------


class _TrackingConnection:
    """DuckDB connection wrapper that records whether it has been closed."""

    def __init__(self) -> None:
        self._conn = duckdb.connect(":memory:")
        self.closed = False
        self.register_count = 0

    def register(self, name: str, object: object) -> None:
        self.register_count += 1
        self._conn.register(name, object)

    def unregister(self, name: str) -> None:
        self._conn.unregister(name)

    def sql(self, query: str) -> Any:
        return self._conn.sql(query)

    def execute(self, sql: str, *parameters: object) -> Any:
        return self._conn.execute(sql, *parameters)

    def close(self) -> None:
        self.closed = True
        self._conn.close()


# ---------------------------------------------------------------------------
# Scripted provider / fake adapter
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """Mutable control plane that is also the zero-argument provider factory.

    The registry resolves this object from the arrow-provider resolver and
    invokes it (``__call__``) to obtain a fresh :data:`ArrowObject`.  Test code
    mutates ``next_dataset`` / ``failure`` / ``fail_register_after`` between
    calls to steer the provider and the recording adapter.
    """

    def __init__(self, initial: pa.Table | None = None) -> None:
        self.initial: pa.Table = initial or pa.table({"id": [1], "value": [1]})
        self.next_dataset: pa.Table | ds.Dataset | None = None
        self.failure: BaseException | None = None
        self.fail_register_after: int | None = None
        self.register_order: list[str] = []
        self.closed_handles: list[str] = []
        self._register_count = 0
        self.invoke_count = 0
        self.source_ids: tuple[str, ...] = ()

    def __call__(self) -> ArrowObject:
        self.invoke_count += 1
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        return self.next_dataset if self.next_dataset is not None else self.initial


class _FakeAdapter:
    """Adapter with failure injection and recording for registry tests.

    The provider is resolved from the arrow-provider resolver during
    ``prepare`` so the real resolution path is exercised; the scripted
    provider controls the returned object.  ``inspect_schema`` returns the
    declared schema (stored on the handle) so these tests focus on registry
    atomicity rather than schema drift (covered by the arrow adapter tests).
    """

    def __init__(self, provider: _ScriptedProvider) -> None:
        self._provider = provider
        self.drift_on_inspect: bool = False
        self.raise_on_inspect: bool = False
        self.register_delay: float = 0.0

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
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=isinstance(resource, pa.RecordBatchReader),
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        if self.raise_on_inspect:
            raise RuntimeError("injected inspect failure")
        if self.drift_on_inspect:
            return table_schema_from_arrow(
                pa.schema(
                    [
                        ("id", pa.int64()),
                        ("value", pa.int64()),
                        ("extra", pa.utf8()),
                    ]
                )
            )
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        if self.register_delay > 0:
            time.sleep(self.register_delay)
        self._provider._register_count += 1
        if (
            self._provider.fail_register_after is not None
            and self._provider._register_count >= self._provider.fail_register_after
        ):
            raise RuntimeError("injected register failure")
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]
        if stable_name not in self._provider.register_order:
            self._provider.register_order.append(stable_name)

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        if not handle.query_scoped:
            return None
        return QueryBinding(
            source_id=handle.source_id,
            stable_name=handle.source_id,
            cleanup=lambda: None,
        )

    def close(self, handle: SourceHandle) -> None:
        self._provider.closed_handles.append(handle.source_id)


def _empty_profiles() -> RuntimeProfileResolver:
    return MappingProfileResolver({})


def _providers_for(provider: _ScriptedProvider) -> ArrowProviderResolver:
    return MappingArrowProviderResolver({"events": provider})


# ---------------------------------------------------------------------------
# Helpers used by the brief's tests
# ---------------------------------------------------------------------------


def registry_statuses(registry: SourceRegistry) -> dict[str, int]:
    """Return ``{source_id: generation}`` for every registered source."""

    return {
        source_id: registry.status(source_id).generation
        for source_id in sorted(registry._sources)
    }


def registered_values(connection: _TrackingConnection) -> dict[str, int]:
    """Return ``{source_id: sum(value)}`` for each registered source."""

    result: dict[str, int] = {}
    for name in sorted(_registered_table_names(connection)):
        row = connection.sql(f'SELECT sum("value") FROM "{name}"').fetchone()
        result[name] = row[0]
    return result


def _registered_table_names(connection: _TrackingConnection) -> set[str]:
    rows = connection.sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _RegistryFixture:
    registry: SourceRegistry
    connection: _TrackingConnection
    provider: _ScriptedProvider


@pytest.fixture
def registry_fixture() -> _RegistryFixture:
    """A registry with one ``events`` source backed by a scripted provider."""

    provider = _ScriptedProvider()
    provider.source_ids = ("events",)
    adapter = _FakeAdapter(provider)
    connection = _TrackingConnection()
    layer = _layer(_events_source())
    registry = SourceRegistry.create(
        layer,
        connection,
        _empty_profiles(),
        _providers_for(provider),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    return _RegistryFixture(registry, connection, provider)


@dataclass
class _BuildResult:
    error: SourceConnectionError | None
    closed_handles: tuple[str, ...]
    connection_closed: bool


@pytest.fixture
def registry_builder() -> Callable[..., _BuildResult]:
    """Factory that builds a multi-source registry with failure injection.

    ``fail_prepare`` names a source whose *registration* is injected to fail.
    Every source is prepared first (so every handle exists), then the named
    source's register call raises — guaranteeing the init-failure cleanup
    closes every prepared handle in sorted order before the connection closes.
    """

    def build(
        *,
        fail_prepare: str | None = None,
        fail_inspect: str | None = None,
        sources: tuple[str, ...] = ("a", "b"),
    ) -> _BuildResult:
        provider_map = {name: _ScriptedProvider() for name in sources}
        connection = _TrackingConnection()
        layer = _layer(*[_named_source(name) for name in sources])
        adapter = _InitRecordingAdapter(
            fail_register=fail_prepare,
            fail_inspect=fail_inspect,
        )
        try:
            SourceRegistry.create(
                layer,
                connection,
                _empty_profiles(),
                MappingArrowProviderResolver(provider_map),
                adapters=MappingProxyType({"pyarrow": adapter}),
            )
        except SourceConnectionError as error:
            return _BuildResult(
                error=error,
                closed_handles=tuple(adapter.closed),
                connection_closed=connection.closed,
            )
        return _BuildResult(
            error=None,
            closed_handles=tuple(adapter.closed),
            connection_closed=connection.closed,
        )

    return build


class _InitRecordingAdapter:
    """Adapter for ``registry_builder`` that records close order during init."""

    def __init__(
        self,
        *,
        fail_register: str | None = None,
        fail_inspect: str | None = None,
    ) -> None:
        self._fail_register = fail_register
        self._fail_inspect = fail_inspect
        self.closed: list[str] = []

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
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=False,
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        if handle.source_id == self._fail_inspect:
            raise RuntimeError("injected inspect failure")
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        if stable_name == self._fail_register:
            raise RuntimeError("injected register failure")
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        return None

    def close(self, handle: SourceHandle) -> None:
        self.closed.append(handle.source_id)


# ---------------------------------------------------------------------------
# Tests from the brief
# ---------------------------------------------------------------------------


def test_reload_source_publishes_new_generation_without_engine_rebuild(
    registry_fixture: _RegistryFixture,
) -> None:
    registry, connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    assert connection.sql('SELECT sum("value") FROM "events"').fetchone() == (1,)

    provider.next_dataset = pa.table({"id": [1], "value": [9]})
    result = registry.reload_source("events")

    assert result.old_generation == 1
    assert result.new_generation == 2
    assert connection.sql('SELECT sum("value") FROM "events"').fetchone() == (9,)


def test_failed_reload_keeps_old_registration_queryable(
    registry_fixture: _RegistryFixture,
) -> None:
    registry, connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    provider.failure = RuntimeError("s3://user:secret@example/private")

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_source("events")

    assert connection.sql('SELECT sum("value") FROM "events"').fetchone() == (1,)
    assert registry.status("events").generation == 1
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_initial_failure_closes_every_prepared_handle(
    registry_builder: Callable[..., _BuildResult],
) -> None:
    result = registry_builder(fail_prepare="b")
    assert result.error is not None
    assert result.error.code == "source_initialization_failed"
    assert result.closed_handles == ("a", "b")
    assert result.connection_closed


def test_reload_all_mid_swap_restores_old_handles(
    registry_fixture: _RegistryFixture,
) -> None:
    registry, connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    provider.fail_register_after = 1
    before = registry_statuses(registry)

    with pytest.raises(SourceReloadError):
        registry.reload_all()

    assert registry_statuses(registry) == before
    assert registered_values(connection) == {"events": 1}


def test_reload_order_and_cleanup_are_deterministic(
    registry_fixture: _RegistryFixture,
) -> None:
    registry, _connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    registry.reload_all()
    assert tuple(provider.register_order) == tuple(sorted(provider.source_ids))
    assert tuple(provider.closed_handles) == tuple(sorted(provider.source_ids))


def test_close_is_idempotent_and_status_fails_after_close(
    registry_fixture: _RegistryFixture,
) -> None:
    registry = registry_fixture.registry
    registry.close()
    registry.close()
    with pytest.raises(SourceConnectionError):
        registry.status("events")


# ---------------------------------------------------------------------------
# Additional substantive tests
# ---------------------------------------------------------------------------


def test_reload_all_publishes_sorted_immutable_results(
    registry_fixture: _RegistryFixture,
) -> None:
    registry, _connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    provider.next_dataset = pa.table({"id": [1], "value": [5]})
    results = registry.reload_all()

    assert tuple(item.source_id for item in results) == tuple(
        sorted(provider.source_ids)
    )
    for item in results:
        assert item.old_generation == 1
        assert item.new_generation == 2


def test_reload_unknown_source_raises_sanitized_error(
    registry_fixture: _RegistryFixture,
) -> None:
    registry = registry_fixture.registry
    with pytest.raises(SourceReloadError) as caught:
        registry.reload_source("missing")
    assert caught.value.code == "reload_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_reload_source_schema_drift_rejects_candidate(
    registry_fixture: _RegistryFixture,
) -> None:
    from selayer.sources.errors import SourceSchemaError

    registry, _connection, _provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )

    # Flip the fake adapter to report a drifted schema on the next inspect so
    # the candidate is rejected and the old registration stays queryable.
    adapter = cast("_FakeAdapter", registry._registrations["events"].adapter)
    adapter.drift_on_inspect = True

    with pytest.raises(SourceSchemaError) as caught:
        registry.reload_source("events")
    assert caught.value.code == "schema_mismatch"
    assert registry.status("events").generation == 1


def test_concurrent_binding_blocks_reload_until_query_exits(
    registry_fixture: _RegistryFixture,
) -> None:
    """A query binding holds the lock; reload's generation change waits for it."""

    registry, _connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    provider.next_dataset = pa.table({"id": [1], "value": [9]})

    bind_entered = threading.Event()
    bind_can_exit = threading.Event()
    reload_completed = threading.Event()
    generation_before = registry.status("events").generation

    def holder() -> None:
        with registry.bind(QueryPlan("events", (), (), (), (), ())):
            bind_entered.set()
            bind_can_exit.wait(timeout=5)

    def reloader() -> None:
        registry.reload_source("events")
        reload_completed.set()

    holder_thread = threading.Thread(target=holder)
    reloader_thread = threading.Thread(target=reloader)

    holder_thread.start()
    assert bind_entered.wait(timeout=5)

    reloader_thread.start()
    # The reload must be blocked while the binding holds the lock.
    time.sleep(0.15)
    assert not reload_completed.is_set()
    assert registry.status("events").generation == generation_before

    bind_can_exit.set()
    holder_thread.join(timeout=5)
    reloader_thread.join(timeout=5)

    assert reload_completed.is_set()
    assert registry.status("events").generation == generation_before + 1


# ---------------------------------------------------------------------------
# Fix 1: concurrent same-source reload serializes generations
# ---------------------------------------------------------------------------


def test_concurrent_same_source_reload_serializes_generations(
    registry_fixture: _RegistryFixture,
) -> None:
    """Two concurrent reload_source calls on the same source serialize.

    Both threads prepare candidates *outside* the lock, then serialize on the
    registry lock for the snapshot/commit/publish section.  The result is
    strictly increasing generations (1 -> 2 -> 3), never two reloads both
    observing generation 1 and both publishing 2.
    """

    registry, connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    provider.next_dataset = pa.table({"id": [1], "value": [9]})
    # Widen the register window so a *missing* lock would reliably race:
    # the register call sits between the old-generation snapshot and the
    # new-generation publication inside the critical section.
    adapter = cast("_FakeAdapter", registry._registrations["events"].adapter)
    adapter.register_delay = 0.03

    barrier = threading.Barrier(2)
    results: list[ReloadResult] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def reloader() -> None:
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            return
        try:
            result = registry.reload_source("events")
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(result)

    threads = [threading.Thread(target=reloader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"concurrent reload errors: {errors!r}"
    assert len(results) == 2
    # Each reload must observe a distinct old generation and publish a
    # distinct new generation — proof of strict serialization.
    assert sorted(r.old_generation for r in results) == [1, 2]
    assert sorted(r.new_generation for r in results) == [2, 3]
    assert registry.status("events").generation == 3
    assert connection.sql('SELECT sum("value") FROM "events"').fetchone() == (9,)


# ---------------------------------------------------------------------------
# Fix 2: mutating-then-raising register restores failing source and prior swaps
# ---------------------------------------------------------------------------


class _SharedCounterAdapter:
    """Multi-source adapter with a shared register counter.

    Unlike :class:`_FakeAdapter`, register counting is self-contained so a
    single counter spans every source's register calls — required to inject a
    mid-swap failure in ``reload_all`` *after* an earlier source has already
    been swapped.  ``mutate_before_fail`` registers the candidate handle
    *before* raising, simulating a driver that partially committed before
    failing, so the rollback path must truly overwrite the mutated state.
    """

    def __init__(self) -> None:
        self._register_count = 0
        self.fail_register_on_call: int | None = None
        self.mutate_before_fail: bool = False
        self.register_log: list[str] = []
        self.closed: list[str] = []

    def reset_count(self) -> None:
        self._register_count = 0
        self.register_log.clear()

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
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=False,
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        self._register_count += 1
        self.register_log.append(stable_name)
        is_fail = (
            self.fail_register_on_call is not None
            and self._register_count == self.fail_register_on_call
        )
        if is_fail and self.mutate_before_fail:
            # Mutate: register the candidate, then raise, simulating a driver
            # that partially committed before failing.
            connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]
            raise RuntimeError("injected register failure after mutation")
        if is_fail:
            raise RuntimeError("injected register failure")
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        return None

    def close(self, handle: SourceHandle) -> None:
        self.closed.append(handle.source_id)


@dataclass
class _MultiSourceFixture:
    registry: SourceRegistry
    connection: _TrackingConnection
    adapter: _SharedCounterAdapter
    providers: dict[str, _ScriptedProvider]


def _build_multi_source(
    names: tuple[str, ...] = ("alpha", "beta"),
) -> _MultiSourceFixture:
    """Build a multi-source registry with a shared-counter adapter."""

    provider_map = {name: _ScriptedProvider() for name in names}
    adapter = _SharedCounterAdapter()
    connection = _TrackingConnection()
    layer = _layer(*[_named_source(name) for name in names])
    registry = SourceRegistry.create(
        layer,
        connection,
        _empty_profiles(),
        MappingArrowProviderResolver(provider_map),
        adapters=MappingProxyType({"pyarrow": adapter}),
    )
    return _MultiSourceFixture(registry, connection, adapter, provider_map)


def test_reload_all_mid_swap_restores_failing_and_prior_swaps() -> None:
    """A mid-swap register failure restores the failing source *and* prior swaps.

    Two sources (``alpha``, ``beta``) are reloaded atomically.  ``alpha`` swaps
    successfully, then ``beta``'s register mutates the connection *and* raises.
    The rollback must restore ``beta``'s old handle (overwriting the mutation)
    *and* ``alpha``'s already-swapped old handle, leaving every old
    registration, queryable value, and generation unchanged.
    """

    fixture = _build_multi_source(("alpha", "beta"))
    registry, connection, adapter, providers = (
        fixture.registry,
        fixture.connection,
        fixture.adapter,
        fixture.providers,
    )
    # Candidates carry a distinct value so a missing restore would be visible.
    for name in ("alpha", "beta"):
        providers[name].next_dataset = pa.table({"id": [1], "value": [100]})

    before_generations = registry_statuses(registry)
    before_values = registered_values(connection)

    # After create the counter is at 2 (one register per source).  Reset so
    # the reload-phase calls are counted from 1.  Make the 2nd reload-phase
    # register (beta, sorted after alpha) mutate-then-fail.
    adapter.reset_count()
    adapter.fail_register_on_call = 2
    adapter.mutate_before_fail = True

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_all()

    # Every old registration restored: generations and queryable values unchanged.
    assert registry_statuses(registry) == before_generations
    assert registered_values(connection) == before_values
    # The register log proves alpha WAS swapped before beta failed — the
    # rollback genuinely had to restore a prior swap, not only the failing one.
    assert adapter.register_log[:2] == ["alpha", "beta"]
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# ---------------------------------------------------------------------------
# Fix 3a: inspect_schema failure during reload closes the prepared candidate
# ---------------------------------------------------------------------------


def test_reload_inspect_failure_closes_prepared_candidate(
    registry_fixture: _RegistryFixture,
) -> None:
    """inspect_schema failure during reload closes the candidate, not the old handle.

    ``prepare`` succeeds but ``inspect_schema`` raises.  The prepared candidate
    handle must be closed (not leaked) and the old registration must remain
    queryable with its generation unchanged.
    """

    registry, connection, provider = (
        registry_fixture.registry,
        registry_fixture.connection,
        registry_fixture.provider,
    )
    assert provider.closed_handles == []

    adapter = cast("_FakeAdapter", registry._registrations["events"].adapter)
    adapter.raise_on_inspect = True

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_source("events")

    # Exactly one handle closed — the candidate, not the old handle.
    assert provider.closed_handles == ["events"]
    # Old handle untouched: still queryable, generation unchanged.
    assert connection.sql('SELECT sum("value") FROM "events"').fetchone() == (1,)
    assert registry.status("events").generation == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# ---------------------------------------------------------------------------
# Fix 3b: inspect_schema failure during initial create closes every prepared handle
# ---------------------------------------------------------------------------


def test_initial_create_inspect_failure_closes_every_prepared_handle(
    registry_builder: Callable[..., _BuildResult],
) -> None:
    """inspect_schema failure during create closes every handle prepared so far.

    Source ``a`` prepares and inspects successfully; source ``b`` prepares but
    ``inspect_schema`` raises.  Both handles were tracked *before* inspect, so
    both are closed during the init-failure cleanup, and the connection closes.
    """

    result = registry_builder(fail_inspect="b")

    assert result.error is not None
    assert result.error.code == "source_initialization_failed"
    assert result.error.source_id == "b"
    assert result.closed_handles == ("a", "b")
    assert result.connection_closed


# ---------------------------------------------------------------------------
# Fix 4: execute acquires the registry RLock
# ---------------------------------------------------------------------------


def test_execute_blocks_while_query_binding_holds_lock(
    registry_fixture: _RegistryFixture,
) -> None:
    """registry.execute acquires the same RLock as bind.

    While a query binding holds the lock, a direct ``execute`` call must block
    until the binding exits.  This proves registry-aware execution can never
    bypass the locking that protects reload/binding atomicity.
    """

    registry = registry_fixture.registry

    bind_entered = threading.Event()
    bind_can_exit = threading.Event()
    executed = threading.Event()

    def holder() -> None:
        with registry.bind(QueryPlan("events", (), (), (), (), ())):
            bind_entered.set()
            bind_can_exit.wait(timeout=5)

    def executor() -> None:
        registry.execute("SELECT 1")
        executed.set()

    holder_thread = threading.Thread(target=holder)
    executor_thread = threading.Thread(target=executor)

    holder_thread.start()
    assert bind_entered.wait(timeout=5)

    executor_thread.start()
    # execute must block while the binding holds the lock.
    time.sleep(0.15)
    assert not executed.is_set()

    bind_can_exit.set()
    holder_thread.join(timeout=5)
    executor_thread.join(timeout=5)

    assert executed.is_set()


# ---------------------------------------------------------------------------
# Fix 5: unsupported connector fails sanitized with code unsupported_connector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "kind"),
    [
        (IcebergConfig("catalog", ("ns",), "table"), "iceberg"),
        (SqliteConfig("/data/db.sqlite", "main.events"), "sqlite"),
        (DuckDbConfig("/data/warehouse.duckdb", "main.events"), "duckdb"),
        (PostgresConfig("warehouse", "public.events"), "postgres"),
    ],
)
def test_unsupported_connector_fails_sanitized(
    connector: object,
    kind: str,
) -> None:
    """Delta/Iceberg/database connectors fail with a sanitized unsupported_connector error.

    These connector kinds have no registered adapter (only parquet/csv/pyarrow
    are built in).  They must fail deterministically with a sanitized
    :class:`SourceConnectionError` (code ``unsupported_connector``) *before*
    a bare ``KeyError`` can escape the adapter lookup.
    """

    # The connector kind must be one without a built-in adapter.
    assert kind in {"delta", "iceberg", "sqlite", "duckdb", "postgres"}

    source = DataSource(
        name="unsupported_src",
        connector=connector,  # type: ignore[arg-type]
        schema=_events_schema(),
        grain=("id",),
    )
    layer = _layer(source)
    connection = _TrackingConnection()
    fake_adapter = _FakeAdapter(_ScriptedProvider())

    with pytest.raises(SourceConnectionError) as caught:
        SourceRegistry.create(
            layer,
            connection,
            _empty_profiles(),
            MappingArrowProviderResolver({}),
            adapters=MappingProxyType({"pyarrow": fake_adapter}),
        )

    assert caught.value.code == "unsupported_connector"
    assert caught.value.source_id == "unsupported_src"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert connection.closed
