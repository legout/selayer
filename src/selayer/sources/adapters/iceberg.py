"""PyIceberg table adapter using ``pyiceberg``.

This adapter satisfies the private
:class:`~selayer.sources.base.SourceAdapter` protocol for the ``iceberg``
connector kind.  It loads a PyIceberg table through a named catalog profile,
records snapshot metadata, and creates query-scoped
:class:`pyarrow.RecordBatchReader` bindings on every query execution.

Design pillars:

* **Persistent table metadata, query-scoped readers.**  ``prepare`` loads the
  table and records its current snapshot ID.  ``register`` is a no-op — no
  reusable reader is published.  ``bind_query`` creates a fresh
  ``RecordBatchReader`` from ``table.scan(row_filter=..., selected_fields=...)``,
  registers it under the stable source name for the locked execution, and
  returns a cleanup that unregisters and closes the reader.
* **Safe snapshot IDs only.**  ``table.metadata.current_snapshot_id`` (an
  integer) is converted to ``str`` and stored as the handle snapshot — never the
  catalog URI, table identifier, or any opaque handle.
* **No DuckDB Iceberg extension.**  DuckDB scans the PyArrow
  ``RecordBatchReader`` via its Arrow replacement-scan mechanism; the DuckDB
  ``iceberg`` extension is never loaded or installed.
* **Source-local filter pushdown.**  Only scalar equality, list membership, and
  inclusive range filters with supported literal types are translated into
  PyIceberg row-filter strings.  Unsupported operators or value types produce
  no pushdown expression; compiled DuckDB SQL still evaluates every filter
  with bound parameters as a residual.
* **Schema normalization.**  PyIceberg represents ``string`` columns as
  ``large_string`` in Arrow; the adapter normalizes these to ``string`` so the
  observed schema matches a standard ``utf8`` declaration.
* **Old snapshot preserved on mismatch/failure.**  Reload loads a *separate*
  candidate table handle before publication; a schema mismatch or load failure
  preserves the old registration.
"""

from __future__ import annotations

import math
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from selayer.sources.base import (
    QueryBinding,
    SourceConsistency,
    SourceFilter,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import IcebergConfig
from selayer.sources.errors import SourceDependencyError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, table_schema_from_arrow

# Conditional import: ``pyiceberg`` is an optional extra.  The import exception
# is never retained — ``_load_catalog`` is ``None`` when the package is absent,
# and ``prepare`` converts the absence to a sanitized
# :class:`SourceDependencyError`.
try:
    from pyiceberg.catalog import load_catalog as _load_catalog
except ImportError:
    _load_catalog = None  # type: ignore[assignment, misc]

__all__ = ["IcebergAdapter"]


@dataclass(slots=True)
class _IcebergResource:
    """Internal record holding a PyIceberg table handle.

    Both resource fields are ``repr=False`` so no resource object can surface in
    diagnostics.  The catalog and table are retained so :meth:`close` can
    release them and :meth:`bind_query` can create per-query scans.

    ``snapshot_schema`` carries the Arrow schema of a *specific* pinned
    snapshot (set by :meth:`IcebergAdapter.reopen`), so :meth:`inspect_schema`
    reports that revision's schema rather than the table's current schema.
    When ``None`` (a fresh ``prepare``), :meth:`inspect_schema` falls back to
    ``table.schema().as_arrow()`` — the current schema, which equals the pinned
    snapshot's schema because ``prepare`` records the current snapshot.
    """

    catalog: object = field(repr=False)
    table: object = field(repr=False)
    snapshot_schema: pa.Schema | None = field(repr=False, default=None)


class IcebergAdapter:
    """PyIceberg table adapter with query-scoped ``RecordBatchReader`` bindings.

    ``prepare`` loads a table through the named catalog profile and records its
    snapshot ID.  ``register`` is a no-op.  ``bind_query`` creates a fresh
    ``RecordBatchReader`` with projection and filter pushdown, registers it, and
    returns a cleanup that unregisters and closes the reader.
    """

    __slots__ = ()

    # -- prepare -----------------------------------------------------------

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        del arrow_providers  # Iceberg sources resolve no arrow provider
        catalog, table = _load_table(source, profiles)
        snapshot = _safe_snapshot_id(table)
        # A table with a current snapshot ID can be re-opened at that exact
        # revision via ``reopen`` (a PyIceberg snapshot-id scan).  A table with
        # no current snapshot (empty / metadata-only) cannot be pinned, so it
        # advertises ``LIVE`` rather than claiming a reopenable revision it
        # cannot reacquire.
        consistency = (
            SourceConsistency.REOPENABLE_SNAPSHOT
            if snapshot is not None
            else SourceConsistency.LIVE
        )
        return SourceHandle(
            source_id=source.name,
            connector="iceberg",
            resource=_IcebergResource(catalog=catalog, table=table),
            schema=source.schema,
            snapshot=snapshot,
            query_scoped=True,
            consistency=consistency,
        )

    # -- reopen ------------------------------------------------------------

    def reopen(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
        snapshot_id: str | None,
    ) -> SourceHandle:
        """Reopen the table pinned at *snapshot_id* (a PyIceberg snapshot ID).

        ``snapshot_id`` is the decimal snapshot-ID string recorded on the
        baseline handle.  The table is reloaded and the snapshot's schema is
        captured into the resource's ``snapshot_schema`` so
        :meth:`inspect_schema` reports *that* revision's schema rather than the
        table's current schema.  Reloading a fresh candidate table (rather than
        reusing the published table handle) keeps the published generation
        untouched, mirroring the candidate-first invariant the registry relies
        on.  A deleted/expired snapshot ID (one no longer present in the
        table's metadata) raises a constant ``ValueError`` so the registry can
        surface a sanitized ``snapshot_mismatch`` — the session's pinned
        revision can no longer be reacquired.
        """

        if snapshot_id is None:
            # No revision to pin: this is not a reopenable snapshot after all,
            # so fall back to the latest rather than fabricating a revision.
            return self.prepare(source, profiles, arrow_providers)
        del arrow_providers  # Iceberg sources resolve no arrow provider
        catalog, table = _load_table(source, profiles)
        pinned_schema = _snapshot_arrow_schema(table, snapshot_id)
        return SourceHandle(
            source_id=source.name,
            connector="iceberg",
            resource=_IcebergResource(
                catalog=catalog, table=table, snapshot_schema=pinned_schema
            ),
            schema=source.schema,
            snapshot=snapshot_id,
            query_scoped=True,
            consistency=SourceConsistency.REOPENABLE_SNAPSHOT,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        assert isinstance(resource, _IcebergResource)
        # A reopened handle carries the pinned snapshot's Arrow schema so the
        # reported schema matches *that* revision rather than the table's
        # current schema.  A fresh ``prepare`` leaves ``snapshot_schema`` as
        # ``None`` and falls back to ``table.schema().as_arrow()`` — the
        # current schema, which equals the pinned snapshot's schema because
        # ``prepare`` records the current snapshot.
        if resource.snapshot_schema is not None:
            return table_schema_from_arrow(resource.snapshot_schema)
        raw_schema = resource.table.schema().as_arrow()  # type: ignore[attr-defined]
        return table_schema_from_arrow(_normalize_arrow_schema(raw_schema))

    # -- registration ------------------------------------------------------

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        # No-op: query-scoped readers are created and registered per query by
        # ``bind_query``, not at prepare/reload time.
        return None

    # -- query binding -----------------------------------------------------

    def bind_query(
        self,
        connection: object,
        handle: SourceHandle,
        requirement: SourceScanRequirement,
    ) -> QueryBinding | None:
        resource = handle.resource
        assert isinstance(resource, _IcebergResource)
        table = resource.table

        row_filter = _iceberg_row_filter(requirement.filters)
        selected_fields = requirement.columns
        scan_kwargs: dict[str, object] = {"selected_fields": selected_fields}
        if row_filter is not None:
            scan_kwargs["row_filter"] = row_filter

        reader = table.scan(**scan_kwargs).to_arrow_batch_reader()  # type: ignore[attr-defined]
        stable_name = handle.source_id
        try:
            connection.register(stable_name, reader)  # type: ignore[attr-defined]
        except Exception:
            # ``register`` may have partially mutated the connection before
            # failing.  Unregister best-effort and close the just-created reader
            # so it is never leaked, then re-raise so the registry boundary can
            # convert the raw exception into a sanitized SourceError.
            with suppress(Exception):
                connection.unregister(stable_name)  # type: ignore[attr-defined]
            with suppress(Exception):
                reader.close()  # type: ignore[attr-defined]
            raise

        def _cleanup() -> None:
            with suppress(Exception):
                connection.unregister(stable_name)  # type: ignore[attr-defined]
            with suppress(Exception):
                reader.close()

        return QueryBinding(
            source_id=handle.source_id,
            stable_name=stable_name,
            cleanup=_cleanup,
        )

    # -- close -------------------------------------------------------------

    def close(self, handle: SourceHandle) -> None:
        resource = handle.resource
        if isinstance(resource, _IcebergResource):
            # Release references so the PyIceberg catalog/table handles can be
            # garbage collected.  PyIceberg catalog/table objects have no
            # explicit ``close()``; dropping the Python reference releases the
            # underlying resources.
            resource.catalog = None
            resource.table = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Catalog configuration
# ---------------------------------------------------------------------------


def _catalog_config(profile: object) -> dict[str, str]:
    """Extract PyIceberg catalog configuration from a runtime profile.

    Forwards *every* profile key whose name and value are both exact builtin
    ``str`` instances, so auth/S3/token settings (``s3.access-key-id``,
    ``s3.secret-access-key``, ``s3.session-token``, ``s3.endpoint``, ``region``,
    ``gcs.*``, ``glue.*``, ...) reach ``load_catalog`` as ``**properties``.

    Secrecy invariants (load-bearing):

    * Key *names* and *values* are both validated with ``type(x) is str``
      (never ``isinstance``) so a hostile ``str`` subclass whose ``__repr__``/
      ``__eq__``/``__hash__`` dunders could leak a secret is rejected before
      any value is forwarded.
    * Values are never enumerated, echoed in an error, rendered in a repr, or
      logged: they pass straight from the opaque profile into the returned
      mapping, which is forwarded to ``load_catalog`` and never repr'd.
    * A non-string key or value (including a hostile subclass) is *skipped*
      rather than forwarded, and never surfaces in an exception message.
    * Unknown profile keys are not rejected: PyIceberg accepts arbitrary
      ``**properties`` and ignores those it does not recognize, so forwarding
      all exact-builtin-string pairs keeps ``load_catalog`` inputs compatible
      with PyIceberg's ``**properties`` contract.
    """

    config: dict[str, str] = {}
    keys = getattr(profile, "keys", None)
    if not callable(keys):
        return config
    key_names: Any = keys()
    for raw_key in key_names:
        # Validate the key *name* as an exact builtin ``str``.  Key names are
        # configuration metadata (not secrets), but a hostile ``str`` subclass
        # is rejected here so its dunders can never run against a real value.
        if type(raw_key) is not str:
            continue
        value: object = profile.value(raw_key)  # type: ignore[attr-defined]
        # Validate the value as an exact builtin ``str``; never echo it.
        if type(value) is str:
            config[raw_key] = value
    return config


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _load_table(
    source: ParsedSource, profiles: RuntimeProfileResolver
) -> tuple[object, object]:
    """Load an Iceberg catalog/table without exposing profile values."""
    if _load_catalog is None:
        raise SourceDependencyError(
            source.name,
            "missing_iceberg_dependency",
            "pyiceberg is required for iceberg sources",
        )
    connector = source.connector
    assert isinstance(connector, IcebergConfig)
    profile = profiles.resolve(connector.catalog_profile, source_id=source.name)
    config = _catalog_config(profile)
    catalog = _load_catalog(connector.catalog_profile, **config)
    table = catalog.load_table((*connector.namespace, connector.table))  # type: ignore[attr-defined]
    return catalog, table


def _safe_snapshot_id(table: object) -> str | None:
    """Return the current snapshot ID as a ``str``, or ``None`` when absent."""

    metadata = getattr(table, "metadata", None)
    if metadata is None:
        return None
    snapshot_id = getattr(metadata, "current_snapshot_id", None)
    if snapshot_id is None:
        return None
    return str(snapshot_id)


def _snapshot_arrow_schema(table: object, snapshot_id: str) -> pa.Schema:
    """Return the normalized Arrow schema recorded by one snapshot."""
    metadata = getattr(table, "metadata", None)
    snapshots = getattr(metadata, "snapshots", ()) if metadata is not None else ()
    for snapshot in snapshots:
        if str(getattr(snapshot, "snapshot_id", "")) != snapshot_id:
            continue
        schema_id = getattr(snapshot, "schema_id", None)
        if schema_id is None:
            return _normalize_arrow_schema(table.schema().as_arrow())  # type: ignore[attr-defined]
        schema_by_id = getattr(metadata, "schema_by_id", None)
        if not callable(schema_by_id):
            raise TypeError("snapshot schema is unavailable")
        schema = schema_by_id(schema_id)
        as_arrow = getattr(schema, "as_arrow", None)
        if not callable(as_arrow):
            raise TypeError("snapshot schema is unavailable")
        raw_schema = as_arrow()
        if not isinstance(raw_schema, pa.Schema):
            raise TypeError("snapshot schema is unavailable")
        return _normalize_arrow_schema(raw_schema)
    raise ValueError("snapshot not found")


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------


def _normalize_arrow_type(dtype: pa.DataType) -> pa.DataType:
    """Recursively normalize ``large_string``/``large_binary`` Arrow types.

    PyIceberg represents ``string``/``binary`` columns as ``large_string``/
    ``large_binary`` in Arrow for read efficiency.  This recurses through every
    nested container — ``list``, ``large_list``, ``fixed_size_list``, ``struct``,
    ``map``, and ``dictionary`` — normalizing leaf string/binary types at any
    depth while preserving field names, nullability, metadata, list sizes, map
    key/item field shape, and dictionary index/ordered properties.
    """

    # Leaf normalization: large_string/large_binary -> string/binary.
    if dtype == pa.large_string():
        return pa.string()
    if dtype == pa.large_binary():
        return pa.binary()

    # list<T> / large_list<T> / fixed_size_list<T, n>: recurse into the value
    # field, preserving the list variant and (for fixed-size) the list size.
    if pa.types.is_list(dtype):
        return pa.list_(_normalize_arrow_field(dtype.value_field))
    if pa.types.is_large_list(dtype):
        return pa.large_list(_normalize_arrow_field(dtype.value_field))
    if pa.types.is_fixed_size_list(dtype):
        return pa.list_(_normalize_arrow_field(dtype.value_field), dtype.list_size)

    # struct<...>: recurse into every named child field.
    if pa.types.is_struct(dtype):
        return pa.struct([_normalize_arrow_field(field) for field in dtype])

    # map<key, value>: recurse into the key and item fields, preserving
    # keys_sorted and the key/item field shape.
    if pa.types.is_map(dtype):
        return pa.map_(
            _normalize_arrow_field(dtype.key_field),
            _normalize_arrow_field(dtype.item_field),
            keys_sorted=dtype.keys_sorted,
        )

    # dictionary<index, value, ordered>: recurse into the index and value
    # types, preserving the ordered flag.  The value type is the leaf most
    # likely to carry large_string/large_binary.
    if pa.types.is_dictionary(dtype):
        return pa.dictionary(
            _normalize_arrow_type(dtype.index_type),
            _normalize_arrow_type(dtype.value_type),
            ordered=dtype.ordered,
        )

    # Any other (non-nested, non-large) type is already normalized.
    return dtype


def _normalize_arrow_field(arrow_field: pa.Field) -> pa.Field:
    """Normalize a field's type while preserving name, nullability, metadata."""

    return pa.field(
        arrow_field.name,
        _normalize_arrow_type(arrow_field.type),
        nullable=arrow_field.nullable,
        metadata=arrow_field.metadata,
    )


def _normalize_arrow_schema(schema: pa.Schema) -> pa.Schema:
    """Recursively normalize ``large_string``/``large_binary`` in a schema.

    PyIceberg represents ``string``/``binary`` columns as ``large_string``/
    ``large_binary`` in Arrow for read efficiency, including inside nested
    types (``list<string>``, ``struct<a: string>``, ``map<string, binary>``,
    ...).  Normalizing these to ``string``/``binary`` at every depth lets a
    standard ``utf8``/``binary`` declaration match the observed schema during
    ``compare_schemas``.  Field names, nullability, metadata, list sizes, map
    key/item shape, and dictionary properties are all preserved.
    """

    return pa.schema(
        [_normalize_arrow_field(arrow_field) for arrow_field in schema],
        metadata=schema.metadata,
    )


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------

# Operators that can be pushed down to PyIceberg.
_PUSHDOWN_OPERATORS: frozenset[str] = frozenset({"eq", "in", "ge", "le"})


def _iceberg_literal(value: object) -> str | None:
    """Render a scalar as a PyIceberg expression literal string.

    Returns ``None`` for unsupported types so the caller knows no pushdown
    expression can be generated for that value.  Only *exact* builtin types are
    accepted: ``type(value) is str/int/float/bool`` (not ``isinstance``) so a
    hostile subclass whose dunders could leak a secret is rejected.
    Non-finite floats (``nan``/``inf``/``-inf``) also return ``None``: PyIceberg's
    row-filter parser cannot represent them, so the caller skips pushdown for
    that value and DuckDB evaluates the original bound filter as a residual.

    * **strings** are single-quoted with embedded single quotes doubled.
    * **booleans** render as ``TRUE``/``FALSE``.
    * **integers/finite floats** render via ``repr``.
    """

    if type(value) is str:
        return "'" + value.replace("'", "''") + "'"
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if type(value) is int:
        return repr(value)
    if type(value) is float:
        if math.isfinite(value):
            return repr(value)
        return None
    return None


def _iceberg_row_filter(
    filters: tuple[SourceFilter, ...],
) -> str | None:
    """Translate structured source filters into a PyIceberg row-filter string.

    Only ``eq``, ``in``, ``ge``, and ``le`` operators with supported literal
    types are translated.  Unsupported operators or value types are silently
    skipped — the compiled DuckDB SQL still evaluates every filter as a
    residual.  Returns ``None`` when no filter can be pushed down.
    """

    parts: list[str] = []
    for f in filters:
        column: object = getattr(f, "column", None)
        operator: object = getattr(f, "operator", None)
        value: object = getattr(f, "value", None)
        if type(column) is not str or type(operator) is not str:
            continue
        if operator == "eq":
            lit = _iceberg_literal(value)
            if lit is not None:
                parts.append(f"{column} = {lit}")
        elif operator == "in":
            values = value
            if isinstance(values, tuple) and values:
                literals = [_iceberg_literal(v) for v in values]
                non_null_literals = [lit for lit in literals if lit is not None]
                if len(non_null_literals) == len(literals):
                    joined = ", ".join(non_null_literals)
                    parts.append(f"{column} IN ({joined})")
        elif operator == "ge":
            lit = _iceberg_literal(value)
            if lit is not None:
                parts.append(f"{column} >= {lit}")
        elif operator == "le":
            lit = _iceberg_literal(value)
            if lit is not None:
                parts.append(f"{column} <= {lit}")
    if not parts:
        return None
    return " AND ".join(parts)
