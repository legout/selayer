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

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from selayer.sources.base import (
    QueryBinding,
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

    Both fields are ``repr=False`` so no resource object can surface in
    diagnostics.  The catalog and table are retained so :meth:`close` can
    release them and :meth:`bind_query` can create per-query scans.
    """

    catalog: object = field(repr=False)
    table: object = field(repr=False)


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
        if _load_catalog is None:
            # Raised outside any ``except`` scope so ``__cause__`` and
            # ``__context__`` remain ``None``.
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
        snapshot = _safe_snapshot_id(table)

        return SourceHandle(
            source_id=source.name,
            connector="iceberg",
            resource=_IcebergResource(catalog=catalog, table=table),
            schema=source.schema,
            snapshot=snapshot,
            query_scoped=True,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        assert isinstance(resource, _IcebergResource)
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


def _safe_snapshot_id(table: object) -> str | None:
    """Return the current snapshot ID as a ``str``, or ``None`` when absent."""

    metadata = getattr(table, "metadata", None)
    if metadata is None:
        return None
    snapshot_id = getattr(metadata, "current_snapshot_id", None)
    if snapshot_id is None:
        return None
    return str(snapshot_id)


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

    * **strings** are single-quoted with embedded single quotes doubled.
    * **booleans** render as ``TRUE``/``FALSE``.
    * **integers/floats** render via ``repr``.
    """

    if type(value) is str:
        return "'" + value.replace("'", "''") + "'"
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if type(value) is int:
        return repr(value)
    if type(value) is float:
        return repr(value)
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
                if all(lit is not None for lit in literals):
                    joined = ", ".join(literals)  # type: ignore[arg-type]
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
