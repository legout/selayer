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

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from selayer.compilation.duckdb import quote_identifier
from selayer.expressions.validation import references
from selayer.sources.adapters.arrow import ArrowDatasetAdapter
from selayer.sources.adapters.database import (
    DuckDbAdapter,
    PostgresAdapter,
    SqliteAdapter,
)
from selayer.sources.adapters.delta import DeltaAdapter
from selayer.sources.adapters.iceberg import IcebergAdapter
from selayer.sources.base import (
    QueryBinding,
    ReloadResult,
    SourceAdapter,
    SourceConsistency,
    SourceFilter,
    SourceHandle,
    SourceScanRequirement,
    SourceStatus,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import connector_kind
from selayer.sources.errors import (
    SourceConnectionError,
    SourceDependencyError,
    SourceReloadError,
    SourceSchemaError,
)
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfileResolver,
)
from selayer.sources.scan import SourceScanSession, SourceSnapshot
from selayer.sources.schema import TableSchema, compare_schemas, schema_fingerprint

# Default target row count for streamed scan-session batches.  Callers may
# override per session via ``open_scan_session(batch_size=...)``.
_DEFAULT_SCAN_BATCH_SIZE = 1024


def _dependency_code(error: BaseException) -> str | None:
    """Return a sanitized known dependency code, if present."""

    return error.code if isinstance(error, SourceDependencyError) else None


# ---------------------------------------------------------------------------
# Scan-session internal helpers (no caller SQL)
# ---------------------------------------------------------------------------


def _resolve_scan_columns(
    schema: TableSchema,
    columns: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the physical columns to project for a scan session.

    Defaults to every declared schema column (in declaration order) when
    ``columns`` is empty.  Each requested column must be present in the
    observed schema; an unknown column raises ``ValueError`` (the public
    :meth:`SourceRegistry.open_scan_session` surface sanitizes that into a
    ``scan_failed`` :class:`SourceError`).
    """

    if not columns:
        return tuple(field.name for field in schema.fields)
    known = {field.name for field in schema.fields}
    resolved = tuple(columns)
    for column in resolved:
        if column not in known:
            raise ValueError("unknown column")
    return resolved


def _validate_batch_size(batch_size: int) -> None:
    """Validate ``batch_size`` is a positive builtin ``int``.

    A ``bool`` (which subclasses ``int``), a ``float``, a ``str``, and every
    other non-int type are rejected via an *exact* ``type(...) is int`` guard
    so they can never reach DuckDB.  ``type(...) is int`` is used rather than
    ``isinstance`` because ``type(True) is bool`` (not ``int``): a bare
    ``isinstance(..., int)`` would silently accept ``True``.
    """

    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("batch_size must be a positive int")


def _build_select_sql(source_id: str, selected: tuple[str, ...]) -> str:
    """Build a parameter-free SELECT projecting quoted columns internally.

    Every identifier — the requested columns and the source's stable name — is
    double-quoted with embedded quotes doubled through the shared
    :func:`~selayer.compilation.duckdb.quote_identifier` helper.  No caller
    supplies SQL: the only interpolated text is the validated, quoted
    identifiers, so a SQL fragment or credential can never reach the engine.
    """

    column_list = ", ".join(quote_identifier(column) for column in selected)
    return f"SELECT {column_list} FROM {quote_identifier(source_id)}"


if TYPE_CHECKING:
    from selayer.model import SemanticLayer
    from selayer.planning import QueryPlan

__all__ = ["SourceRegistry"]


# Closed built-in adapter mapping keyed by connector kind.  No public plugin
# registration API is exposed.
def _builtin_adapters() -> Mapping[str, SourceAdapter]:
    arrow = ArrowDatasetAdapter()
    delta = DeltaAdapter()
    duckdb_adapter = DuckDbAdapter()
    sqlite = SqliteAdapter()
    postgres = PostgresAdapter()
    mapping: dict[str, SourceAdapter] = {
        "parquet": arrow,
        "csv": arrow,
        "pyarrow": arrow,
        "delta": delta,
        "sqlite": sqlite,
        "duckdb": duckdb_adapter,
        "postgres": postgres,
        "iceberg": IcebergAdapter(),
    }
    return MappingProxyType(mapping)


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
        dependency_code: str | None = None
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
        except Exception as error:  # noqa: BLE001
            init_failed = True
            dependency_code = _dependency_code(error)

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
            if dependency_code is not None:
                raise SourceDependencyError(
                    failed_source_id or "<source>",
                    dependency_code,
                    "a source dependency is unavailable",
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
        dependency_code = None
        try:
            for source_id, adapter, handle in prepared:
                failed_source_id = source_id
                registration_handle = handle
                if handle.query_scoped:
                    # A PyArrow RecordBatchReader is one-shot and is used only
                    # for schema inspection; query binding recreates it. Iceberg
                    # keeps its persistent table metadata handle for scan-time
                    # binding, while its register method is intentionally a no-op.
                    if handle.connector == "pyarrow":
                        close_quietly(adapter, handle)
                        registration_handle = replace(handle, resource=None)
                else:
                    adapter.register(connection, source_id, handle)
                registrations[source_id] = _Registration(
                    adapter, registration_handle, 1
                )
        except Exception as error:  # noqa: BLE001
            init_failed = True
            dependency_code = _dependency_code(error)
        if init_failed:
            for _source_id, adapter, handle in prepared:
                close_quietly(adapter, handle)
            connection.close()
            if dependency_code is not None:
                raise SourceDependencyError(
                    failed_source_id or "<source>",
                    dependency_code,
                    "a source dependency is unavailable",
                )
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

        The lifecycle lock spans state inspection, candidate preparation,
        schema verification, the ``register`` commit, registry publication,
        and generation increment.  Rolled-back candidates and replaced old
        handles are collected while locked and closed *outside* the lock after
        rollback/publication, so the critical section covers only the state
        that must be observed consistently.  Holding the lock through
        preparation prevents a concurrent :meth:`close` from clearing
        registrations or closing the connection while a candidate is being
        prepared — eliminating the close/reload race.  Concurrent
        ``reload_source`` calls serialize and produce strictly increasing
        generations (``1 -> 2 -> 3``).  Every candidate is closed on any
        failure.
        """

        cleanup: list[tuple[SourceAdapter, SourceHandle]] = []
        result: ReloadResult | None = None
        pending_error: BaseException | None = None
        with self._lock:
            try:
                self._ensure_open(source_id)
                if source_id not in self._registrations:
                    raise SourceReloadError(
                        source_id, "reload_failed", "unknown source"
                    )
                source = self._sources[source_id]
                adapter = self._registrations[source_id].adapter

                preparation_failure: _CandidatePreparationFailed | None = None
                try:
                    prepared_candidate = self._prepare_candidate(adapter, source)
                except _CandidatePreparationFailed as failure:
                    preparation_failure = failure
                    prepared_candidate = None
                if preparation_failure is not None:
                    if preparation_failure.candidate is not None:
                        cleanup.append((adapter, preparation_failure.candidate))
                    if preparation_failure.dependency_code is not None:
                        raise SourceDependencyError(
                            source_id,
                            preparation_failure.dependency_code,
                            "a source dependency is unavailable",
                        )
                    raise SourceReloadError(source_id, "reload_failed", "reload failed")
                if prepared_candidate is None:
                    raise SourceReloadError(source_id, "reload_failed", "reload failed")
                candidate, observed = prepared_candidate
                mismatches = compare_schemas(source.schema, observed)
                if mismatches:
                    cleanup.append((adapter, candidate))
                    raise SourceSchemaError(
                        source_id, "schema_mismatch", "schema mismatch"
                    )

                # Snapshot old registration/generation, commit the candidate
                # registration, and publish the incremented generation — all
                # under the lifecycle lock so close cannot race ahead.  A
                # concurrent reload_source blocks and observes the new
                # generation when it acquires the lock, guaranteeing
                # 1 -> 2 -> 3.
                old_registration = self._registrations[source_id]
                old_handle = old_registration.handle
                old_generation = old_registration.generation
                registration_candidate = candidate
                if candidate.query_scoped and candidate.connector == "pyarrow":
                    # Retain only the provider and declared schema metadata;
                    # the inspected one-shot reader is closed after unlock.
                    cleanup.append((adapter, candidate))
                    registration_candidate = replace(candidate, resource=None)
                commit_failed = False
                commit_dependency_code: str | None = None
                try:
                    if not registration_candidate.query_scoped:
                        adapter.register(
                            self._connection, source_id, registration_candidate
                        )
                except Exception as error:  # noqa: BLE001
                    commit_failed = True
                    commit_dependency_code = _dependency_code(error)
                    # register may have mutated the connection before raising,
                    # so restore the previous handle before proceeding.
                    restore_quietly(adapter, self._connection, source_id, old_handle)
                else:
                    self._registrations[source_id] = _Registration(
                        adapter, registration_candidate, old_generation + 1
                    )
                if commit_failed:
                    cleanup.append((adapter, candidate))
                    if commit_dependency_code is not None:
                        raise SourceDependencyError(
                            source_id,
                            commit_dependency_code,
                            "a source dependency is unavailable",
                        )
                    raise SourceReloadError(source_id, "reload_failed", "reload failed")

                # The old handle is no longer referenced by DuckDB; release it
                # outside the lock after publication.
                cleanup.append((adapter, old_handle))
                result = ReloadResult(
                    source_id=source_id,
                    old_generation=old_generation,
                    new_generation=old_generation + 1,
                    schema_fingerprint=schema_fingerprint(observed),
                    snapshot=registration_candidate.snapshot,
                )
            except BaseException as exc:  # noqa: BLE001
                pending_error = exc

        # Close rolled-back candidates and replaced old handles outside the
        # lock.  close() may have already run after unlock, so close_quietly
        # must remain quiet/idempotent — no raw exception or KeyError can
        # escape.
        for _adapter, _handle in cleanup:
            close_quietly(_adapter, _handle)

        if pending_error is not None:
            raise pending_error
        assert result is not None
        return result

    def reload_all(self) -> tuple[ReloadResult, ...]:
        """Atomically reload every source in sorted source-ID order.

        The lifecycle lock spans state inspection, candidate preparation,
        schema verification, registration swaps, generation publication, and
        rollback.  Rolled-back candidates and replaced old handles are
        collected while locked and closed *outside* the lock after
        rollback/publication, so the critical section covers only the state
        that must be observed consistently.  Holding the lock through
        preparation prevents a concurrent :meth:`close` from clearing
        registrations or closing the connection while candidates are being
        prepared — eliminating the close/reload race.  If any ``register``
        raises — even after mutating the connection — the failing source's old
        handle is restored too (not only the already-swapped entries), every
        old registration is restored in reverse order, no generation changes,
        and the old query results are unchanged.  Every candidate is closed on
        any failure.
        """

        cleanup: list[tuple[SourceAdapter, SourceHandle]] = []
        result: tuple[ReloadResult, ...] | None = None
        pending_error: BaseException | None = None
        with self._lock:
            try:
                self._ensure_open("<source>")
                source_ids = sorted(self._sources)
                candidates: dict[
                    str, tuple[SourceAdapter, SourceHandle, TableSchema]
                ] = {}
                prepare_failed_source: str | None = None
                dependency_code: str | None = None
                unexpected_failure = False
                preparation_failure: _CandidatePreparationFailed | None = None
                try:
                    for source_id in source_ids:
                        source = self._sources[source_id]
                        registration = self._registrations[source_id]
                        adapter = registration.adapter
                        try:
                            prepared_candidate = self._prepare_candidate(
                                adapter, source
                            )
                        except _CandidatePreparationFailed as failure:
                            if failure.candidate is not None:
                                cleanup.append((adapter, failure.candidate))
                            prepare_failed_source = source_id
                            preparation_failure = failure
                            break
                        if prepared_candidate is None:
                            prepare_failed_source = source_id
                            break
                        candidate, observed = prepared_candidate
                        mismatches = compare_schemas(source.schema, observed)
                        if mismatches:
                            cleanup.append((adapter, candidate))
                            prepare_failed_source = source_id
                            break
                        registration_candidate = candidate
                        if candidate.query_scoped and candidate.connector == "pyarrow":
                            # Store only provider/schema metadata; the
                            # inspected reader is closed after unlocking.
                            cleanup.append((adapter, candidate))
                            registration_candidate = replace(candidate, resource=None)
                        candidates[source_id] = (
                            adapter,
                            registration_candidate,
                            observed,
                        )
                except SourceDependencyError as dep_error:
                    dependency_code = _dependency_code(dep_error)
                except Exception:  # noqa: BLE001
                    unexpected_failure = True

                if preparation_failure is not None:
                    for adapter, candidate, _observed in candidates.values():
                        cleanup.append((adapter, candidate))
                    if preparation_failure.dependency_code is not None:
                        raise SourceDependencyError(
                            prepare_failed_source or "<source>",
                            preparation_failure.dependency_code,
                            "a source dependency is unavailable",
                        )
                    raise SourceReloadError(
                        prepare_failed_source or "<source>",
                        "reload_all_failed",
                        "reload failed",
                    )

                # On any prepare-phase failure (prepare/schema-mismatch via
                # ``break`` or an unexpected exception), collect every
                # candidate prepared so far for outside-lock cleanup and raise
                # a sanitized SourceReloadError *outside* the except scope so
                # ``__cause__``/``__context__`` remain ``None``.
                if (
                    prepare_failed_source is not None
                    or dependency_code is not None
                    or unexpected_failure
                ):
                    for adapter, candidate, _observed in candidates.values():
                        cleanup.append((adapter, candidate))
                    failed_id = (
                        prepare_failed_source
                        if prepare_failed_source is not None
                        else "<source>"
                    )
                    if dependency_code is not None:
                        raise SourceDependencyError(
                            failed_id,
                            dependency_code,
                            "a source dependency is unavailable",
                        )
                    raise SourceReloadError(
                        failed_id, "reload_all_failed", "reload failed"
                    )

                swapped: list[tuple[str, SourceAdapter, SourceHandle, int]] = []
                commit_failed = False
                commit_dependency_code: str | None = None
                commit_failed_source: str | None = None
                results: list[ReloadResult] = []
                # Swap every registration and publish every generation under
                # the lifecycle lock.  A register failure rolls back the
                # failing source's old handle *and* every already-swapped entry
                # in reverse order, leaving all old registrations, query
                # results, and generations unchanged.
                for source_id in source_ids:
                    adapter, candidate, _observed = candidates[source_id]
                    old = self._registrations[source_id]
                    try:
                        if not candidate.query_scoped:
                            adapter.register(self._connection, source_id, candidate)
                    except Exception as error:  # noqa: BLE001
                        commit_failed = True
                        commit_failed_source = source_id
                        commit_dependency_code = _dependency_code(error)
                        # register may have mutated the connection before
                        # raising, so restore the failing source's old handle
                        # too — not only the previously swapped entries.
                        restore_quietly(
                            adapter, self._connection, source_id, old.handle
                        )
                        break
                    swapped.append((source_id, adapter, old.handle, old.generation))
                if commit_failed:
                    # Restore each already-swapped old handle in reverse order.
                    for source_id, adapter, old_handle, _gen in reversed(swapped):
                        restore_quietly(
                            adapter, self._connection, source_id, old_handle
                        )
                else:
                    # Publish every generation change only after every swap
                    # succeeded, within the same lock scope.
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
                        cleanup.append((adapter, candidate))
                    failed_id = (
                        commit_failed_source
                        if commit_failed_source is not None
                        else "<source>"
                    )
                    if commit_dependency_code is not None:
                        raise SourceDependencyError(
                            failed_id,
                            commit_dependency_code,
                            "a source dependency is unavailable",
                        )
                    raise SourceReloadError(
                        "<source>", "reload_all_failed", "reload failed"
                    )

                # Close old handles after the commit succeeded, outside the
                # lock.
                for _sid, adapter, old_handle, _gen in swapped:
                    cleanup.append((adapter, old_handle))
                result = tuple(results)
            except BaseException as exc:  # noqa: BLE001
                pending_error = exc

        # Close rolled-back candidates and replaced old handles outside the
        # lock.  close() may have already run after unlock, so close_quietly
        # must remain quiet/idempotent — no raw exception or KeyError can
        # escape.
        for _adapter, _handle in cleanup:
            close_quietly(_adapter, _handle)

        if pending_error is not None:
            raise pending_error
        assert result is not None
        return result

    # -- lifecycle: status / close ----------------------------------------

    def status(self, source_id: str) -> SourceStatus:
        with self._lock:
            self._ensure_open(source_id)
            if source_id not in self._registrations:
                raise SourceConnectionError(
                    source_id, "connect_failed", "unknown source"
                )
            registration = self._registrations[source_id]
            return SourceStatus.from_handle(
                registration.handle, registration.generation
            )

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
    def _bind_requirements_locked(
        self,
        requirements: Mapping[str, SourceScanRequirement],
    ) -> Iterator[None]:
        """Private requirement-binding context; the caller MUST hold the lock.

        The single shared requirement-binding path used by both query plans
        (:meth:`bind_requirements`) and scan sessions
        (:meth:`open_scan_session`).  It binds every query-scoped source named
        in ``requirements`` once via ``adapter.bind_query`` (which creates a
        fresh reader, registers it, and returns a cleanup) so the reload lock
        cannot swap a handle mid-query; when ``bind_query`` returns ``None`` it
        falls back to preparing and registering a fresh handle from the
        adapter's provider.  Query-scoped adapter preparation logic is *not*
        duplicated elsewhere.

        Sources are visited in sorted ID order; a binding failure on one
        source aborts the remaining bindings.  Every binding failure (a raw
        PyIceberg/Arrow/registration exception from ``bind_query`` or the
        fallback prepare/register) is caught and surfaced as a sanitized
        :class:`~selayer.sources.errors.SourceConnectionError` (code
        ``bind_failed``) raised *outside* the active ``except`` scope so no
        driver-derived detail can surface and
        ``__cause__``/``__context__`` remain ``None``.

        Cleanup runs in reverse and is hardened the same way: a connector
        cleanup exception never escapes raw, never aborts the remaining
        cleanups, and never skips the (caller-owned) lock release.  The first
        failing binding is surfaced as a sanitized
        ``cleanup_failed`` error, again raised *outside* the active
        ``except`` scope; a binding failure (if any) takes priority.
        """

        prepared: list[tuple[SourceAdapter, SourceHandle, str]] = []
        bindings: list[QueryBinding] = []
        bind_failed_source: str | None = None
        cleanup_failed_source: str | None = None
        try:
            for source_id in sorted(requirements):
                registration = self._registrations.get(source_id)
                if registration is None or not registration.handle.query_scoped:
                    continue
                adapter = registration.adapter
                handle = registration.handle
                requirement = requirements[source_id]
                try:
                    binding = adapter.bind_query(self._connection, handle, requirement)
                    if binding is not None:
                        bindings.append(binding)
                        continue
                    source = self._sources[source_id]
                    fresh = adapter.prepare(
                        source, self._profiles, self._arrow_providers
                    )
                    # Track the fresh handle *before* registration so a
                    # ``register`` failure still closes it (the ``finally``
                    # cleanup runs ``unregister_quietly`` for any partial
                    # registration and ``close_quietly`` for the handle) instead
                    # of leaking the freshly-prepared resource.
                    prepared.append((adapter, fresh, source_id))
                    adapter.register(self._connection, source_id, fresh)
                except Exception:  # noqa: BLE001
                    # Catch every raw binding-stage exception (PyIceberg scan
                    # failure, Arrow reader failure, DuckDB registration
                    # failure, fallback prepare/register failure) so it cannot
                    # escape unsanitized.  The sanitized SourceError is raised
                    # *outside* this except scope below.
                    bind_failed_source = source_id
                    break
            if bind_failed_source is None:
                yield
        finally:
            # Cleanup runs in reverse and must never let a connector cleanup
            # exception escape raw, never abort the remaining cleanups, and
            # never skip the caller-owned lock release.  The first failing
            # binding's source is recorded (outside this except scope) so a
            # sanitized ``cleanup_failed`` error can be raised below with
            # ``__cause__``/``__context__`` left ``None``; a binding failure
            # (if any) still takes priority as the reported error.  The
            # prepared-resource cleanup already runs through the quiet helpers
            # (``unregister_quietly``/``close_quietly``) which suppress per the
            # established cleanup convention, so it cannot abort the remaining
            # cleanups either.
            for binding in reversed(bindings):
                try:
                    binding.cleanup()
                except Exception:  # noqa: BLE001
                    if cleanup_failed_source is None:
                        cleanup_failed_source = binding.source_id
            for adapter, fresh, source_id in reversed(prepared):
                unregister_quietly(self._connection, source_id)
                close_quietly(adapter, fresh)
        if bind_failed_source is not None:
            # Constructed and raised outside the active ``except`` scope so
            # ``__cause__`` and ``__context__`` remain ``None`` and the constant
            # message (looked up from the allowlisted ``bind_failed`` code)
            # never echoes driver text.
            raise SourceConnectionError(
                bind_failed_source,
                "bind_failed",
                "the source could not be bound for the query",
            )
        if cleanup_failed_source is not None:
            # Constructed and raised outside the active ``except`` scope so
            # ``__cause__`` and ``__context__`` remain ``None`` and the constant
            # message (looked up from the allowlisted ``cleanup_failed`` code)
            # never echoes driver text.  A binding failure (handled above)
            # takes priority; this path only fires on the otherwise-successful
            # query path, where a connector cleanup error would otherwise
            # escape raw.
            raise SourceConnectionError(
                cleanup_failed_source,
                "cleanup_failed",
                "the source could not be cleaned up after the query",
            )

    @contextmanager
    def bind_requirements(
        self,
        requirements: Mapping[str, SourceScanRequirement],
    ) -> Iterator[None]:
        """Hold the registry lock and bind the query-scoped sources in a
        requirement map.

        Thin wrapper over the shared private :meth:`_bind_requirements_locked`
        context (which performs the actual binding and cleanup) that surrounds
        it with the registry lifecycle lock.  See that method for the binding,
        cleanup, and sanitization guarantees.
        """

        with self._lock, self._bind_requirements_locked(requirements):
            yield

    @contextmanager
    def bind(self, plan: QueryPlan) -> Iterator[None]:
        """Hold the registry lock for one query and bind query-scoped sources.

        Thin wrapper over :meth:`bind_requirements`: the plan's per-source scan
        requirements (each carrying its declared grain) are computed once and
        restricted to the sources the plan touches, so the requirement
        calculation lives in the caller and the binding body is reusable for
        a direct requirement map (e.g. the physical grain audit).
        """

        requirements = requirements_for_plan(plan)
        selected = {
            source_id: requirements[source_id] for source_id in _plan_sources(plan)
        }
        with self.bind_requirements(selected):
            yield

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

    # -- scan sessions ----------------------------------------------------

    def open_scan_session(
        self,
        source_id: str,
        *,
        columns: tuple[str, ...] = (),
        batch_size: int = _DEFAULT_SCAN_BATCH_SIZE,
    ) -> SourceScanSession:
        """Open a bounded, context-managed scan session over one source.

        Acquires the registry lifecycle lock for the *full* session lifetime
        (from this call until the returned session's context-manager exit),
        binds only the requested source through the same private
        requirement-binding path query plans use
        (:meth:`_bind_requirements_locked` — query-scoped adapters are
        materialized once, never duplicated), validates the selected columns
        against the observed schema, quotes every identifier internally, and
        streams typed :class:`pyarrow.RecordBatch` objects from the source's
        registered stable name.

        **No caller supplies SQL or credentials.**  Identifiers are quoted
        internally and the :class:`~selayer.sources.profiles.RuntimeProfileResolver`
        bound when the registry was constructed is reused; ``open_scan_session``
        accepts no credential override and the returned session exposes no
        resolved profile value.

        **The lock blocks the same registry.**  While a scan session is open,
        :meth:`reload_source`, :meth:`reload_all`, :meth:`close`,
        :meth:`bind`, and :meth:`execute` on *this* registry block until the
        session closes.  ``selayer-discovery`` profiling must therefore
        construct a dedicated registry and connection from the charter's
        runtime profile resolver; proposal verification must construct another
        fresh registry and never run inside an open profile session.

        Every DuckDB and adapter failure (unknown source, unknown column,
        invalid batch size, binding failure, reader creation failure) is
        surfaced as a sanitized :class:`~selayer.sources.errors.SourceError`
        constructed *outside* an active ``except`` scope so
        ``__cause__``/``__context__`` remain ``None``; bindings and the lock
        are released exactly once on any failure.

        Args:
            source_id: stable identifier of the source to scan.
            columns: physical columns to project; defaults to every declared
                schema column.  Each must be present in the observed schema.
            batch_size: target :class:`pyarrow.RecordBatch` row count; must be
                a positive ``int``.

        Returns:
            A :class:`~selayer.sources.scan.SourceScanSession` ready to stream.
        """

        self._lock.acquire()
        binding_ctx: Any = None
        binding_entered = False
        reader: pa.RecordBatchReader | None = None
        handle: SourceHandle | None = None
        # ``pending`` holds a sanitized SourceError constructed outside an
        # active ``except`` scope; it is raised after any partial teardown so
        # ``__cause__``/``__context__`` stay ``None``.
        pending: SourceConnectionError | SourceDependencyError | None = None
        try:
            self._ensure_open(source_id)
            registration = self._registrations.get(source_id)
            if registration is None:
                pending = SourceConnectionError(
                    source_id, "connect_failed", "unknown source"
                )
            else:
                handle = registration.handle
                try:
                    selected = _resolve_scan_columns(handle.schema, columns)
                    _validate_batch_size(batch_size)
                    requirement = SourceScanRequirement(columns=selected)
                except ValueError:
                    pending = SourceConnectionError(
                        source_id, "scan_failed", "invalid scan parameters"
                    )
                    requirement = None
                    selected = ()
                if pending is None:
                    assert requirement is not None
                    binding_ctx = self._bind_requirements_locked(
                        {source_id: requirement}
                    )
                    try:
                        binding_ctx.__enter__()
                        binding_entered = True
                    except SourceConnectionError as error:
                        # Already sanitized by the binding context.
                        pending = error
                    if pending is None:
                        sql = _build_select_sql(source_id, selected)
                        try:
                            reader = self._connection.execute(sql).to_arrow_reader(
                                batch_size=batch_size
                            )
                        except Exception:  # noqa: BLE001 - sanitize DuckDB failure
                            pending = SourceConnectionError(
                                source_id,
                                "scan_failed",
                                "the source could not be scanned",
                            )
        except BaseException:  # noqa: BLE001 - unexpected setup failure
            if pending is None:
                pending = SourceConnectionError(
                    source_id, "scan_failed", "the source could not be scanned"
                )
        if pending is not None:
            # Teardown whatever was acquired, release the lock exactly once,
            # then surface the sanitized error outside every ``except`` scope.
            if binding_entered and binding_ctx is not None:
                with suppress(BaseException):  # best-effort
                    binding_ctx.__exit__(None, None, None)
            self._lock.release()
            raise pending
        assert handle is not None
        assert reader is not None

        # Idempotent release: exits the query-scoped binding context and
        # releases the registry lifecycle lock exactly once.  The stream
        # reader is *not* closed here — the session owns that exclusively via
        # its idempotent ``_close_reader`` (called from ``cancel`` and
        # context-manager exit), so the reader is closed exactly once even
        # when ``cancel`` runs before exit.  Every step suppresses so a
        # connector cleanup exception can never escape raw or skip the lock
        # release.
        released = [False]

        def _release() -> None:
            if released[0]:
                return
            released[0] = True
            if binding_ctx is not None:
                with suppress(BaseException):  # best-effort
                    binding_ctx.__exit__(None, None, None)
            self._lock.release()

        baseline_schema_fingerprint = schema_fingerprint(handle.schema)

        def _recheck() -> SourceSnapshot:
            return self._recheck_source(
                source_id,
                baseline_consistency=handle.consistency,
                baseline_snapshot_id=handle.snapshot,
                baseline_schema_fingerprint=baseline_schema_fingerprint,
            )

        return SourceScanSession(
            source_id=source_id,
            schema=handle.schema,
            consistency=handle.consistency,
            snapshot_id=handle.snapshot,
            reader=reader,
            release=_release,
            recheck=_recheck,
        )

    def _recheck_source(
        self,
        source_id: str,
        *,
        baseline_consistency: SourceConsistency,
        baseline_snapshot_id: str | None,
        baseline_schema_fingerprint: str,
    ) -> SourceSnapshot:
        """Prepare a fresh candidate, verify it matches the session, return it.

        Reuses :meth:`_prepare_candidate` (the same adapter path reload uses)
        so recheck observes exactly what a reload would.  The fresh
        candidate's ``(consistency, snapshot_id, schema_fingerprint)`` triple
        is compared against the session's baseline values *before* the
        candidate is closed; on any mismatch a sanitized
        :class:`~selayer.sources.errors.SourceConnectionError` (code
        ``snapshot_mismatch``) is raised so a caller learns deterministically
        that the snapshot it is streaming is no longer current.  The fresh
        candidate is always closed; the session's own stream is unaffected.
        Called while the scan session holds the lifecycle lock.
        """

        registration = self._registrations.get(source_id)
        if registration is None:
            # Constructed outside any ``except`` scope.
            raise SourceConnectionError(
                source_id, "connect_failed", "unknown source"
            )
        adapter = registration.adapter
        source = self._sources[source_id]
        candidate: SourceHandle | None = None
        observed: TableSchema | None = None
        preparation_failure: _CandidatePreparationFailed | None = None
        try:
            candidate, observed = self._prepare_candidate(adapter, source)
        except _CandidatePreparationFailed as failure:
            preparation_failure = failure
            if failure.candidate is not None:
                close_quietly(adapter, failure.candidate)
        if (
            preparation_failure is not None
            or candidate is None
            or observed is None
        ):
            assert observed is None
            if (
                preparation_failure is not None
                and preparation_failure.dependency_code is not None
            ):
                raise SourceDependencyError(
                    source_id,
                    preparation_failure.dependency_code,
                    "a source dependency is unavailable",
                )
            raise SourceConnectionError(
                source_id, "scan_failed", "the source could not be scanned"
            )
        # Capture the fresh triple from the candidate *before* closing it, then
        # close the candidate unconditionally.  The comparison runs against the
        # session's open-time baseline: a mismatch means the snapshot the
        # session is streaming is no longer current, surfaced as a sanitized,
        # deterministic ``snapshot_mismatch`` error raised outside any
        # ``except`` scope so ``__cause__``/``__context__`` remain ``None``.
        fresh_fingerprint = schema_fingerprint(observed)
        snapshot = SourceSnapshot(
            consistency=candidate.consistency,
            snapshot_id=candidate.snapshot,
            schema_fingerprint=fresh_fingerprint,
        )
        close_quietly(adapter, candidate)
        if (
            snapshot.consistency != baseline_consistency
            or snapshot.snapshot_id != baseline_snapshot_id
            or snapshot.schema_fingerprint != baseline_schema_fingerprint
        ):
            raise SourceConnectionError(
                source_id, "snapshot_mismatch", "the source snapshot has changed"
            )
        return snapshot

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
    ) -> tuple[SourceHandle, TableSchema]:
        """Prepare and inspect a candidate, raising on failure.

        Failures are raised as :class:`_CandidatePreparationFailed` so the
        caller can construct the sanitized
        :class:`~selayer.sources.errors.SourceError` outside the ``except``
        scope, preserving the ``__cause__``/``__context__`` invariant.
        If ``prepare`` succeeds but ``inspect_schema`` raises, the prepared
        handle is closed so it is never leaked.
        """

        candidate: SourceHandle | None = None
        try:
            candidate = adapter.prepare(source, self._profiles, self._arrow_providers)
            observed = adapter.inspect_schema(candidate)
        except SourceDependencyError as error:
            raise _CandidatePreparationFailed(candidate, _dependency_code(error))
        except Exception:  # noqa: BLE001
            raise _CandidatePreparationFailed(candidate, None)
        return candidate, observed


# ---------------------------------------------------------------------------
# Sentinels and quiet helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CandidatePreparationFailed(Exception):
    """Internal failure carrying candidate ownership to the caller."""

    candidate: SourceHandle | None
    dependency_code: str | None


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


# ---------------------------------------------------------------------------
# Source-scan requirement extraction
# ---------------------------------------------------------------------------


class _ColumnCollector:
    """First-seen-order column accumulator with de-duplication."""

    __slots__ = ("_columns", "_seen")

    def __init__(self) -> None:
        self._columns: list[str] = []
        self._seen: set[str] = set()

    def add(self, column: str) -> None:
        if column not in self._seen:
            self._seen.add(column)
            self._columns.append(column)

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self._columns)


def _is_pushdown_scalar(value: object) -> bool:
    """Return ``True`` for exact builtin scalar types safe for pushdown.

    Non-finite floats (``math.nan``, ``math.inf``, ``-math.inf``) are rejected
    via :func:`math.isfinite` so they never reach PyIceberg's row-filter parser.
    A non-finite bound produces no :class:`SourceFilter`; DuckDB still evaluates
    the original bound filter as a residual.
    """

    if type(value) is float:
        return math.isfinite(value)
    return type(value) in {str, int, bool}


def _translate_planned_filter(
    column: str,
    value: object,
) -> list[SourceFilter]:
    """Translate a planned filter value into zero or more SourceFilters.

    Only scalar equality (``eq``), list membership (``in``), and inclusive range
    (``ge``/``le``) filters with supported literal types are translated.
    Unsupported value types produce no SourceFilter; the compiled DuckDB SQL
    still evaluates every filter as a residual.
    """

    from selayer.planning.types import ListFilter, RangeFilter, ScalarFilter

    if isinstance(value, ScalarFilter):
        if _is_pushdown_scalar(value.value):
            return [SourceFilter(column=column, operator="eq", value=value.value)]
    elif isinstance(value, ListFilter):
        if value.values and all(_is_pushdown_scalar(v) for v in value.values):
            return [SourceFilter(column=column, operator="in", value=value.values)]
    elif isinstance(value, RangeFilter):
        result: list[SourceFilter] = []
        if _is_pushdown_scalar(value.start):
            result.append(SourceFilter(column=column, operator="ge", value=value.start))
        if _is_pushdown_scalar(value.end):
            result.append(SourceFilter(column=column, operator="le", value=value.end))
        return result
    return []


def requirements_for_plan(
    plan: QueryPlan,
    *,
    grains: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
) -> Mapping[str, SourceScanRequirement]:
    """Extract per-source scan requirements from a validated query plan.

    The public entry point is the one-argument form
    ``requirements_for_plan(plan)``: it seeds each source's declared row-identity
    (grain) columns from ``plan.source_grains`` (connection-free, credential-free
    metadata populated by :func:`~selayer.planning.planner.plan_query`), then
    collects physical columns from planned dimensions, planned filters, fact
    expression references, and join endpoints — preserving first-seen order with
    a companion set so a column referenced by multiple mechanisms appears once.

    The private keyword-only ``grains`` argument is a fallback for hand-built
    legacy plans whose ``source_grains`` is empty: when present it takes
    precedence over ``plan.source_grains`` for the named sources so a caller
    that knows each source's grain can still supply it.  This keeps the
    registry's query-time binding decoupled from any plan that does not yet
    carry declared grain metadata.

    Translates source-local scalar, list, and range filters into structured
    :class:`SourceFilter` objects for adapter pushdown.

    Metric and measure IDs never appear in the result columns: only physical
    ``source.column`` references extracted from fact expressions, dimensions,
    filters, joins, and the declared source grain are collected.
    """

    plan_sources = _plan_sources(plan)
    plan_source_grains: Mapping[str, tuple[str, ...]] = plan.source_grains
    collectors: dict[str, _ColumnCollector] = {
        source_id: _ColumnCollector() for source_id in plan_sources
    }
    filters_by_source: dict[str, list[SourceFilter]] = {}

    # 1. Grain columns (first, in grain declaration order).  Each source's
    #    grain comes from the explicit ``grains`` fallback when present,
    #    otherwise from the plan's declared ``source_grains`` so the
    #    one-argument public form carries row-identity columns by default.
    for source_id in plan_sources:
        if source_id in grains:
            grain_columns = grains[source_id]
        else:
            grain_columns = plan_source_grains.get(source_id, ())
        for column in grain_columns:
            collectors[source_id].add(column)

    # 2. Planned dimensions.
    for item in plan.dimensions:
        source = item.dimension.source
        if source in collectors:
            collectors[source].add(item.dimension.column)

    # 3. Planned filters (columns + SourceFilter translation).
    for item in plan.filters:
        source = item.dimension.source
        column = item.dimension.column
        if source in collectors:
            collectors[source].add(column)
            filters_by_source.setdefault(source, []).extend(
                _translate_planned_filter(column, item.value)
            )

    # 4. Fact expression references (measure order, then left-to-right).
    for measure in plan.measures:
        for ref in references(measure.expression):
            parts = ref.parts
            if len(parts) == 2:
                source, column = parts[0], parts[1]
                if source in collectors:
                    collectors[source].add(column)

    # 5. Join endpoints.
    for step in plan.joins:
        if step.source in collectors:
            collectors[step.source].add(step.source_column)
        if step.target in collectors:
            collectors[step.target].add(step.target_column)

    result: dict[str, SourceScanRequirement] = {}
    for source_id in plan_sources:
        result[source_id] = SourceScanRequirement(
            columns=collectors[source_id].columns,
            filters=tuple(filters_by_source.get(source_id, ())),
        )
    return MappingProxyType(result)
