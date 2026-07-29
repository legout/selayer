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
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.config import PyArrowConfig
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
        sources: tuple[str, ...] = ("a", "b"),
    ) -> _BuildResult:
        provider_map = {name: _ScriptedProvider() for name in sources}
        connection = _TrackingConnection()
        layer = _layer(*[_named_source(name) for name in sources])
        adapter = _InitRecordingAdapter(fail_register=fail_prepare)
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

    def __init__(self, *, fail_register: str | None = None) -> None:
        self._fail_register = fail_register
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
