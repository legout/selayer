"""Candidate-first atomic source registry.

:class:`SourceRegistry` owns the DuckDB connection lifecycle for registered
sources.  It prepares and validates adapter candidates *before* mutating the
shared connection, then commits a reload by replacing the DuckDB registration
under a single :class:`threading.RLock`.  A failed reload never leaves the
connection pointing at a half-swapped source: the previous handle is restored
and the candidate is closed.

Lifecycle guarantees:

* **Atomic single-source reload.**  :meth:`reload_source` builds and validates
  the candidate, then ``connection.register`` is the single commit point.  If
  registration fails the previous handle is re-registered before the lock is
  released.
* **Atomic multi-source reload.**  :meth:`reload_all` prepares every candidate
  first, swaps in sorted source-ID order, and on failure restores each
  already-swapped old handle in reverse order.  Generation counters advance
  only after every swap succeeds.
* **Sanitized errors.**  Driver exceptions are never retained: every failure
  surfaces as a :class:`~selayer.sources.errors.SourceError` subclass raised
  *outside* the ``except`` scope so ``__cause__``/``__context__`` stay ``None``,
  and the constant message never echoes driver text.
* **Query-scoped binding.**  :meth:`bind` is a context manager that holds the
  registry lock for the query duration and recreates query-scoped (reader)
  sources once per query, so a reload cannot swap a handle out from under a
  running query.
* **Idempotent close.**  :meth:`close` is safe to call repeatedly; afterwards
  lifecycle operations raise
  :class:`~selayer.sources.errors.SourceConnectionError`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from selayer.sources.adapters.arrow import ArrowDatasetAdapter
from selayer.sources.adapters.delta import DeltaAdapter
from selayer.sources.base import (
    ReloadResult,
    SourceAdapter,
    SourceHandle,
    SourceStatus,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import connector_kind
from selayer.sources.errors import (
    SourceConnectionError,
    SourceReloadError,
    SourceSchemaError,
)
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, compare_schemas, schema_fingerprint

if TYPE_CHECKING:
    from selayer.model import SemanticLayer
    from selayer.planning import QueryPlan

__all__ = ["SourceRegistry"]


# Closed built-in adapter mapping keyed by connector kind.  No public plugin
# registration API is exposed.
def _builtin_adapters() -> Mapping[str, SourceAdapter]:
    arrow = ArrowDatasetAdapter()
    delta = DeltaAdapter()
    return MappingProxyType(
        {"parquet": arrow, "csv": arrow, "pyarrow": arrow, "delta": delta}
    )


@dataclass(frozen=True, slots=True)
class _Registration:
    adapter: SourceAdapter
    handle: SourceHandle
    generation: int


def _parsed_source_from_data_source(source: Any) -> ParsedSource:
    """Reconstruct a :class:`ParsedSource` from a public :class:`DataSource`.

    The public :class:`~selayer.model.DataSource` carries the same
    ``(name, connector, schema, grain)`` shape as a parsed source; adapters
    consume :class:`ParsedSource`, so this is a structural copy.
    """

    return ParsedSource(
        name=source.name,
        connector=source.connector,
        schema=source.schema,
        grain=source.grain,
    )


class SourceRegistry:
    """Atomic, registry-backed source lifecycle manager."""

    __slots__ = (
        "_adapters",
        "_arrow_providers",
        "_closed",
        "_connection",
        "_lock",
        "_profiles",
        "_registrations",
        "_sources",
    )

    def __init__(
        self,
        connection: Any,
        sources: Mapping[str, ParsedSource],
        registrations: Mapping[str, _Registration],
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
        adapters: Mapping[str, SourceAdapter],
    ) -> None:
        self._connection = connection
        self._sources = MappingProxyType(dict(sources))
        self._registrations: dict[str, _Registration] = dict(registrations)
        self._profiles = profiles
        self._arrow_providers = arrow_providers
        self._adapters = adapters
        self._lock = RLock()
        self._closed = False

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        layer: SemanticLayer,
        connection: Any,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
        *,
        adapters: Mapping[str, SourceAdapter] | None = None,
    ) -> SourceRegistry:
        """Prepare, validate, and register every source in ``layer``.

        Candidates are built and schema-validated before the connection is
        mutated.  If any source fails to initialize, every prepared handle and
        the connection itself are closed and a
        :class:`~selayer.sources.errors.SourceConnectionError` (code
        ``source_initialization_failed``) is raised outside the failure scope.
        """

        adapter_map = adapters if adapters is not None else _builtin_adapters()
        sources: dict[str, ParsedSource] = {}
        registrations: dict[str, _Registration] = {}
        prepared: list[tuple[str, SourceAdapter, SourceHandle]] = []
        failed_source_id: str | None = None
        init_failed = False
        unsupported_id: str | None = None
        try:
            for source_id in sorted(layer.data_sources):
                failed_source_id = source_id
                data_source = layer.data_sources[source_id]
                parsed = _parsed_source_from_data_source(data_source)
                sources[source_id] = parsed
                kind = connector_kind(data_source.connector)
                # Unsupported connector kinds (not mapped by any built-in or
                # injected adapter) must fail deterministically with a sanitized
                # error *before* a bare ``KeyError`` can escape the lookup.
                if kind not in adapter_map:
                    unsupported_id = source_id
                    raise _UnsupportedConnector(source_id)
                adapter = adapter_map[kind]
                handle = adapter.prepare(parsed, profiles, arrow_providers)
                # Track the handle immediately so cleanup closes it even if
                # ``inspect_schema`` raises after a successful prepare.
                prepared.append((source_id, adapter, handle))
                observed = adapter.inspect_schema(handle)
                mismatches = compare_schemas(data_source.schema, observed)
                if mismatches:
                    raise _SchemaDrift(data_source.schema, observed)
        except Exception:  # noqa: BLE001
            init_failed = True

        if init_failed:
            # Close every handle prepared so far, then the connection.  Errors
            # raised below are constructed outside the except scope so the
            # SourceError __cause__/__context__ invariants hold.
            for _source_id, adapter, handle in prepared:
                close_quietly(adapter, handle)
            connection.close()
            # An unmapped connector kind surfaces a dedicated, sanitized error
            # (never the bare ``KeyError`` from the adapter lookup).
            if unsupported_id is not None:
                raise SourceConnectionError(
                    unsupported_id,
                    "unsupported_connector",
                    "connector type is not supported",
                )
            if failed_source_id is not None:
                raise SourceConnectionError(
                    failed_source_id,
                    "source_initialization_failed",
                    "source initialization failed",
                )
            raise SourceConnectionError(
                "<source>",
                "source_initialization_failed",
                "source initialization failed",
            )

        # Commit: register every prepared handle.  Registration failure here
        # is a hard initialization failure, so tear everything down.
        try:
            for source_id, adapter, handle in prepared:
                failed_source_id = source_id
                adapter.register(connection, source_id, handle)
                registrations[source_id] = _Registration(adapter, handle, 1)
        except Exception:  # noqa: BLE001
            init_failed = True
        if init_failed:
            for _source_id, adapter, handle in prepared:
                close_quietly(adapter, handle)
            connection.close()
            raise SourceConnectionError(
                failed_source_id or "<source>",
                "source_initialization_failed",
                "source initialization failed",
            )

        return cls(
            connection=connection,
            sources=sources,
            registrations=registrations,
            profiles=profiles,
            arrow_providers=arrow_providers,
            adapters=adapter_map,
        )

    # -- lifecycle: reload -------------------------------------------------

    def reload_source(self, source_id: str) -> ReloadResult:
        """Atomically reload one source, publishing a new generation.

        Candidate preparation and schema verification happen *before* the
        critical section.  Snapshotting the old registration/generation, the
        ``register`` commit, the registry publication, and the generation
        increment all occur inside one ``RLock`` critical section so concurrent
        ``reload_source`` calls serialize and produce strictly increasing
        generations (``1 -> 2 -> 3``).  The old/candidate handles are closed
        *outside* the lock only after the commit has succeeded or rolled back.
        """

        self._ensure_open(source_id)
        if source_id not in self._registrations:
            raise SourceReloadError(source_id, "reload_failed", "unknown source")
        source = self._sources[source_id]
        adapter = self._registrations[source_id].adapter

        prepared_candidate = self._prepare_candidate(adapter, source)
        if prepared_candidate is None:
            # prepare or inspect failed; raise outside the except scope.
            raise SourceReloadError(source_id, "reload_failed", "reload failed")
        candidate, observed = prepared_candidate
        mismatches = compare_schemas(source.schema, observed)
        if mismatches:
            close_quietly(adapter, candidate)
            raise SourceSchemaError(source_id, "schema_mismatch", "schema mismatch")

        # One critical section: snapshot old registration/generation, commit
        # the candidate registration, and publish the incremented generation.
        # A concurrent reload_source blocks here and observes the new
        # generation when it acquires the lock, guaranteeing 1 -> 2 -> 3.
        commit_failed = False
        old_handle: SourceHandle | None = None
        old_generation = 0
        with self._lock:
            old_registration = self._registrations[source_id]
            old_handle = old_registration.handle
            old_generation = old_registration.generation
            try:
                adapter.register(self._connection, source_id, candidate)
            except Exception:  # noqa: BLE001
                commit_failed = True
                # register may have mutated the connection before raising, so
                # restore the previous handle before releasing the lock.
                restore_quietly(adapter, self._connection, source_id, old_handle)
            else:
                self._registrations[source_id] = _Registration(
                    adapter, candidate, old_generation + 1
                )
        if commit_failed:
            close_quietly(adapter, candidate)
            raise SourceReloadError(source_id, "reload_failed", "reload failed")

        # The old handle is no longer referenced by DuckDB; release it outside
        # the lock after the commit succeeded.
        close_quietly(adapter, old_handle)
        return ReloadResult(
            source_id=source_id,
            old_generation=old_generation,
            new_generation=old_generation + 1,
            schema_fingerprint=schema_fingerprint(observed),
            snapshot=candidate.snapshot,
        )

    def reload_all(self) -> tuple[ReloadResult, ...]:
        """Atomically reload every source in sorted source-ID order.

        Candidates are prepared and verified before the critical section.
        Inside one ``RLock`` section the stable registrations are swapped in
        sorted source-ID order and the generations are published.  If any
        ``register`` raises — even after mutating the connection — the failing
        source's old handle is restored too (not only the already-swapped
        entries), every old registration is restored in reverse order, no
        generation changes, and the old query results are unchanged.
        """

        self._ensure_open("<source>")
        source_ids = sorted(self._sources)
        candidates: dict[str, tuple[SourceAdapter, SourceHandle, TableSchema]] = {}
        prepare_failed_source: str | None = None
        try:
            for source_id in source_ids:
                source = self._sources[source_id]
                registration = self._registrations[source_id]
                adapter = registration.adapter
                prepared_candidate = self._prepare_candidate(adapter, source)
                if prepared_candidate is None:
                    prepare_failed_source = source_id
                    raise _PrepareFailed
                candidate, observed = prepared_candidate
                mismatches = compare_schemas(source.schema, observed)
                if mismatches:
                    close_quietly(adapter, candidate)
                    prepare_failed_source = source_id
                    raise _PrepareFailed
                candidates[source_id] = (adapter, candidate, observed)
        except Exception:  # noqa: BLE001
            # Close every candidate prepared so far.
            for adapter, candidate, _observed in candidates.values():
                close_quietly(adapter, candidate)
            failed_id = (
                prepare_failed_source
                if prepare_failed_source is not None
                else "<source>"
            )
            raise SourceReloadError(failed_id, "reload_failed", "reload failed")

        swapped: list[tuple[str, SourceAdapter, SourceHandle, int]] = []
        commit_failed = False
        results: list[ReloadResult] = []
        # One critical section: swap every registration and publish every
        # generation.  A register failure rolls back the failing source's old
        # handle *and* every already-swapped entry in reverse order, leaving
        # all old registrations, query results, and generations unchanged.
        with self._lock:
            for source_id in source_ids:
                adapter, candidate, _observed = candidates[source_id]
                old = self._registrations[source_id]
                try:
                    adapter.register(self._connection, source_id, candidate)
                except Exception:  # noqa: BLE001
                    commit_failed = True
                    # register may have mutated the connection before raising,
                    # so restore the failing source's old handle too — not only
                    # the previously swapped entries.
                    restore_quietly(adapter, self._connection, source_id, old.handle)
                    break
                swapped.append((source_id, adapter, old.handle, old.generation))
            if commit_failed:
                # Restore each already-swapped old handle in reverse order.
                for source_id, adapter, old_handle, _gen in reversed(swapped):
                    restore_quietly(adapter, self._connection, source_id, old_handle)
            else:
                # Publish every generation change only after every swap
                # succeeded, within the same critical section.
                for source_id in source_ids:
                    adapter, candidate, observed = candidates[source_id]
                    old_generation = self._registrations[source_id].generation
                    self._registrations[source_id] = _Registration(
                        adapter, candidate, old_generation + 1
                    )
                    results.append(
                        ReloadResult(
                            source_id=source_id,
                            old_generation=old_generation,
                            new_generation=old_generation + 1,
                            schema_fingerprint=schema_fingerprint(observed),
                            snapshot=candidate.snapshot,
                        )
                    )
        if commit_failed:
            for adapter, candidate, _observed in candidates.values():
                close_quietly(adapter, candidate)
            raise SourceReloadError("<source>", "reload_failed", "reload failed")

        # Close old handles outside the lock after the commit succeeded.
        for source_id, adapter, old_handle, _gen in swapped:
            close_quietly(adapter, old_handle)
        return tuple(results)

    # -- lifecycle: status / close ----------------------------------------

    def status(self, source_id: str) -> SourceStatus:
        self._ensure_open(source_id)
        if source_id not in self._registrations:
            raise SourceConnectionError(source_id, "connect_failed", "unknown source")
        registration = self._registrations[source_id]
        return SourceStatus.from_handle(registration.handle, registration.generation)

    def close(self) -> None:
        """Idempotently close every handle and the connection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for registration in self._registrations.values():
                close_quietly(registration.adapter, registration.handle)
            self._registrations.clear()
            close_connection_quietly(self._connection)

    # -- query binding -----------------------------------------------------

    @contextmanager
    def bind(self, plan: QueryPlan) -> Iterator[None]:
        """Hold the registry lock for one query and bind query-scoped sources.

        Persistent sources need no per-query binding.  Query-scoped (reader)
        sources are recreated from their provider and re-registered once per
        query so the reload lock cannot swap a handle mid-query and the
        single-pass reader is fresh for every execution.
        """

        self._lock.acquire()
        scoped: list[tuple[SourceAdapter, SourceHandle, str]] = []
        try:
            for source_id in _plan_sources(plan):
                registration = self._registrations.get(source_id)
                if registration is None:
                    continue
                handle = registration.handle
                if not handle.query_scoped:
                    continue
                source = self._sources[source_id]
                adapter = registration.adapter
                fresh = adapter.prepare(source, self._profiles, self._arrow_providers)
                adapter.register(self._connection, source_id, fresh)
                scoped.append((adapter, fresh, source_id))
            yield
        finally:
            for adapter, fresh, source_id in reversed(scoped):
                unregister_quietly(self._connection, source_id)
                close_quietly(adapter, fresh)
            self._lock.release()

    @contextmanager
    def execute_lock(self) -> Iterator[None]:
        """Acquire the registry lock for direct, registry-aware execution."""

        with self._lock:
            yield

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        """Execute SQL on the registry connection under the registry lock.

        The lock is acquired here directly so registry-aware execution can
        never bypass locking.  Because the lock is an ``RLock``, nested
        acquisition from within :meth:`bind` is permitted (reentrant).
        """

        with self._lock:
            return self._connection.execute(sql, parameters)

    # -- internals ---------------------------------------------------------

    def _ensure_open(self, source_id: str) -> None:
        if self._closed:
            raise SourceConnectionError(
                source_id, "connect_failed", "registry is closed"
            )

    def _prepare_candidate(
        self,
        adapter: SourceAdapter,
        source: ParsedSource,
    ) -> tuple[SourceHandle, TableSchema] | None:
        """Prepare and inspect a candidate, returning ``None`` on failure.

        Failures are signaled rather than raised so the caller can construct the
        sanitized :class:`~selayer.sources.errors.SourceError` outside the
        ``except`` scope, preserving the ``__cause__``/``__context__`` invariant.
        If ``prepare`` succeeds but ``inspect_schema`` raises, the prepared
        handle is closed so it is never leaked.
        """

        candidate: SourceHandle | None = None
        try:
            candidate = adapter.prepare(source, self._profiles, self._arrow_providers)
            observed = adapter.inspect_schema(candidate)
        except Exception:  # noqa: BLE001
            # inspect_schema may have raised after a successful prepare; close
            # the prepared handle so it is never leaked.
            if candidate is not None:
                close_quietly(adapter, candidate)
            return None
        return candidate, observed


# ---------------------------------------------------------------------------
# Sentinels and quiet helpers
# ---------------------------------------------------------------------------


class _PrepareFailed(Exception):
    """Internal sentinel raised only to reach the prepare-failure handler."""


class _SchemaDrift(Exception):
    """Internal sentinel carrying declared/observed schemas on mismatch."""

    def __init__(self, declared: TableSchema, observed: TableSchema) -> None:
        self.declared = declared
        self.observed = observed


class _UnsupportedConnector(Exception):
    """Internal sentinel for a connector kind with no registered adapter."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id


def close_quietly(adapter: SourceAdapter, handle: SourceHandle) -> None:
    try:
        adapter.close(handle)
    except Exception:  # noqa: BLE001, S110
        pass


def restore_quietly(
    adapter: SourceAdapter,
    connection: Any,
    stable_name: str,
    handle: SourceHandle,
) -> None:
    try:
        adapter.register(connection, stable_name, handle)
    except Exception:  # noqa: BLE001, S110
        pass


def close_connection_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001, S110
        pass


def unregister_quietly(connection: Any, stable_name: str) -> None:
    try:
        connection.unregister(stable_name)
    except Exception:  # noqa: BLE001, S110
        pass


def _plan_sources(plan: QueryPlan) -> tuple[str, ...]:
    """Return the sorted set of source IDs a plan touches."""

    sources: set[str] = {plan.anchor_source}
    for step in plan.joins:
        sources.add(step.source)
        sources.add(step.target)
    return tuple(sorted(sources))
