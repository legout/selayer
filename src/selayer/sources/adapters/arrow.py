"""PyArrow Dataset adapter for parquet, csv, and programmatic sources.

This adapter satisfies the private
:class:`~selayer.sources.base.SourceAdapter` protocol for the ``parquet``,
``csv``, and ``pyarrow`` connector kinds.  It builds a
:class:`pyarrow.dataset.Dataset` (for file-based and programmatic dataset
sources) or binds a :class:`pyarrow.RecordBatchReader` (for query-scoped
programmatic providers) against the *declared* :class:`~selayer.sources.schema.TableSchema`.

Design pillars:

* **No eager Polars materialization.**  Sources are registered with DuckDB as
  PyArrow Datasets (or readers), so projection and filter pushdown happen
  inside DuckDB's Arrow scanner.  ``polars.read_parquet`` / ``polars.read_csv``
  are never called.
* **Declared schema is authoritative.**  The dataset is built with the declared
  Arrow schema so an observed schema that drifts from the declaration is caught
  by :func:`~selayer.sources.schema.compare_schemas` before registration.
* **Providers are the reloadable unit.**  Programmatic ``pyarrow`` sources
  resolve a zero-argument provider factory and invoke it on every prepare and
  (for query-scoped readers) on every query binding, so the underlying relation
  can be re-opened.
* **Query-scoped readers.**  A provider that yields a
  :class:`pyarrow.RecordBatchReader` produces a query-scoped handle: the reader
  is single-pass, so it is recreated and re-registered once per query.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.dataset as padataset

from selayer.sources.base import (
    QueryBinding,
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
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, table_schema_to_arrow

__all__ = ["ArrowDatasetAdapter"]


def _reader_cleanup(connection: object, stable_name: str) -> Callable[[], None]:
    """Return an idempotent cleanup that deregisters a query-scoped view."""

    def _cleanup() -> None:
        with suppress(Exception):
            # DuckDB's unregister removes the replacement-scan binding; any
            # failure is suppressed because cleanup must never mask the query
            # result or raise out of a context-manager exit.
            connection.unregister(stable_name)  # type: ignore[attr-defined]

    return _cleanup


class ArrowDatasetAdapter:
    """Adapter for parquet/csv files and programmatic PyArrow sources.

    A single instance serves the ``parquet``, ``csv``, and ``pyarrow``
    connector kinds.  File-based connectors build a
    :class:`pyarrow.dataset.Dataset` from ``location`` using the declared
    schema; the ``pyarrow`` connector resolves a provider factory from
    :class:`~selayer.sources.profiles.ArrowProviderResolver` and invokes it.
    """

    __slots__ = ()

    # -- prepare -----------------------------------------------------------

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        connector = source.connector
        arrow_schema = table_schema_to_arrow(source.schema)
        kind = _connector_kind(connector)
        if kind == "pyarrow":
            assert isinstance(connector, PyArrowConfig)
            provider = arrow_providers.resolve(connector.handle, source_id=source.name)
            resource = provider()
            return _handle_for_arrow_object(source, resource, provider)
        if kind == "parquet":
            assert isinstance(connector, ParquetConfig)
            # Build *without* a schema override so the physical Parquet schema
            # is observed by ``inspect_schema`` and any drift (type, extra
            # field, nullability) is caught by ``compare_schemas`` before the
            # source is registered.  DuckDB still gets ARROW_SCAN pushdown over
            # the physical dataset.
            resource = padataset.dataset(connector.location, format="parquet")
        elif kind == "csv":
            assert isinstance(connector, CsvConfig)
            resource = _csv_dataset(connector, arrow_schema)
        else:  # pragma: no cover - registry only dispatches these kinds
            raise TypeError(f"ArrowDatasetAdapter does not serve connector {kind!r}")
        return SourceHandle(
            source_id=source.name,
            connector=kind,
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=False,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        arrow_schema = _resource_schema(resource)
        # The resource carries the *observed* Arrow schema: the physical
        # Parquet schema, the reconciled CSV schema (physical types with
        # declared nullability), or the provider object's schema.  Converting
        # it back to the logical model lets the registry compare it against
        # the declaration and reject any drift before registration.
        from selayer.sources.schema import table_schema_from_arrow

        return table_schema_from_arrow(arrow_schema)

    # -- registration ------------------------------------------------------

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]

    # -- query binding -----------------------------------------------------

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        # Persistent datasets (parquet/csv and provider-backed datasets/scanners/
        # tables) are registered once and benefit from automatic DuckDB Arrow
        # pushdown; they need no per-query binding.  Only query-scoped readers
        # are recreated per query, and that path is driven by the registry which
        # re-invokes the stored provider directly.
        if not handle.query_scoped or handle.cleanup is None:
            return None
        return QueryBinding(
            source_id=handle.source_id,
            stable_name=handle.source_id,
            cleanup=handle.cleanup,
        )

    # -- close -------------------------------------------------------------

    def close(self, handle: SourceHandle) -> None:
        resource = handle.resource
        closer = getattr(resource, "close", None)
        if closer is not None:
            with suppress(Exception):
                closer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connector_kind(connector: SourceConnector) -> str:
    if isinstance(connector, ParquetConfig):
        return "parquet"
    if isinstance(connector, CsvConfig):
        return "csv"
    if isinstance(connector, PyArrowConfig):
        return "pyarrow"
    return type(connector).__name__


def _csv_dataset(connector: CsvConfig, declared_schema: pa.Schema) -> padataset.Dataset:
    parse_options = pacsv.ParseOptions(
        delimiter=connector.delimiter,
        quote_char=connector.quote_char,
        escape_char=connector.escape_char,
    )
    # Probe the *physical* CSV schema without a declared type override so the
    # file's own inferred types are observed.  CSV type inference marks every
    # column ``nullable=True`` regardless of the data, so the probe alone would
    # produce false mismatches against declared non-nullable fields.  For a
    # header-less file there is no header row to read names from, so the
    # declared field names are used positionally.
    if not connector.has_header:
        read_options = pacsv.ReadOptions(
            column_names=list(declared_schema.names), autogenerate_column_names=False
        )
    else:
        read_options = pacsv.ReadOptions(autogenerate_column_names=False)
    probe_format = padataset.CsvFileFormat(
        parse_options=parse_options, read_options=read_options
    )
    physical_schema = padataset.dataset(connector.location, format=probe_format).schema
    # Reconcile by position: keep the physical inferred types (so type drift
    # and any extra columns remain observable) but adopt the declared
    # nullability for declared positions so inference's ``nullable=True`` does
    # not create a false mismatch.  Columns beyond the declaration keep their
    # physical nullability so an extra field still surfaces as drift.
    reconciled_schema = _reconcile_csv_schema(physical_schema, declared_schema)
    final_format = padataset.CsvFileFormat(
        parse_options=parse_options, read_options=read_options
    )
    return padataset.dataset(
        connector.location, schema=reconciled_schema, format=final_format
    )


def _reconcile_csv_schema(
    physical_schema: pa.Schema, declared_schema: pa.Schema
) -> pa.Schema:
    """Adopt declared nullability by position while keeping physical types.

    The physical types (and any extra fields beyond the declaration) are
    preserved so genuine type drift and extra columns stay observable for
    :func:`~selayer.sources.schema.compare_schemas`; only the nullability of
    the declared positions is taken from the declaration, neutralizing CSV
    inference's unconditional ``nullable=True``.
    """

    fields: list[pa.Field] = []
    for index, physical_field in enumerate(physical_schema):
        if index < len(declared_schema):
            declared_field = declared_schema.field(index)
            fields.append(
                pa.field(
                    physical_field.name,
                    physical_field.type,
                    nullable=declared_field.nullable,
                )
            )
        else:
            fields.append(physical_field)
    return pa.schema(fields)


def _handle_for_arrow_object(
    source: ParsedSource,
    resource: ArrowObject,
    provider: Callable[[], ArrowObject],
) -> SourceHandle:
    """Build a handle for a provider-resolved Arrow object.

    Datasets, scanners, and tables are persistent: their provider is retained
    on the handle (as ``resource``-adjacent state via the cleanup slot is not
    appropriate) so the registry can re-invoke it on reload.  Record batch
    readers are single-pass and therefore query-scoped.
    """

    if isinstance(resource, pa.RecordBatchReader):
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=True,
            # The provider is stashed on the handle so the registry can recreate
            # the reader per query without re-resolving the resolver.  It is
            # repr-hidden by ``SourceHandle``.
            cleanup=_reader_recreator(provider),
        )
    return SourceHandle(
        source_id=source.name,
        connector="pyarrow",
        resource=resource,
        schema=source.schema,
        snapshot=None,
        query_scoped=False,
        # Stash the provider so reload can re-invoke it.  Using the cleanup slot
        # is the only repr-hidden mutable carrier on a frozen handle.
        cleanup=_persisted_provider(provider),
    )


def _persisted_provider(
    provider: Callable[[], ArrowObject],
) -> Callable[[], None]:
    """Wrap a provider so it survives on a frozen handle's cleanup slot.

    The cleanup slot is the only ``repr=False`` carrier on
    :class:`~selayer.sources.base.SourceHandle`; the registry reads it back via
    :func:`_read_provider`.  Returning a no-op keeps the persisted provider out
    of any accidental close path.
    """

    def _noop() -> None:
        return None

    _noop.provider = provider  # type: ignore[attr-defined]
    return _noop


def _read_provider(handle: SourceHandle) -> Callable[[], ArrowObject] | None:
    """Return the provider stashed on a handle, if any."""

    cleanup = handle.cleanup
    if cleanup is None:
        return None
    return getattr(cleanup, "provider", None)


def _reader_recreator(
    provider: Callable[[], ArrowObject],
) -> Callable[[], None]:
    """Stash a reader provider for query-scoped recreation on the handle."""

    def _noop() -> None:
        return None

    _noop.provider = provider  # type: ignore[attr-defined]
    return _noop


def _resource_schema(resource: ArrowObject) -> pa.Schema:
    if isinstance(resource, padataset.Dataset):
        return resource.schema
    if isinstance(resource, padataset.Scanner):
        return resource.dataset_schema
    if isinstance(resource, pa.Table):
        return resource.schema
    if isinstance(resource, pa.RecordBatchReader):
        return resource.schema
    raise TypeError(f"unsupported Arrow resource: {type(resource).__name__}")
