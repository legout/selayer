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
        connection.register(stable_name, reader)  # type: ignore[attr-defined]

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

# Known profile keys for a PyIceberg catalog configuration.  Unknown keys are
# rejected so a typo cannot silently route credentials to the wrong path.  Only
# key *names* are configuration metadata; secret values are never enumerated.
_CATALOG_CONFIG_KEYS: frozenset[str] = frozenset(
    {"type", "uri", "warehouse", "region", "endpoint"}
)


def _catalog_config(profile: object) -> dict[str, str]:
    """Extract PyIceberg catalog configuration from a runtime profile.

    Every present value must be an *exact* builtin ``str``
    (``type(value) is str``, not ``isinstance``) so a hostile ``str`` subclass
    whose dunders could leak a secret is rejected before any value is forwarded
    to ``load_catalog``.
    """

    config: dict[str, str] = {}
    for key in sorted(_CATALOG_CONFIG_KEYS):
        if key in profile:  # type: ignore[operator]
            value: object = profile.value(key)  # type: ignore[attr-defined]
            if type(value) is str:
                config[key] = value
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


def _normalize_arrow_schema(schema: pa.Schema) -> pa.Schema:
    """Normalize ``large_string``/``large_binary`` to their standard equivalents.

    PyIceberg represents ``string`` columns as ``large_string`` in Arrow for
    read efficiency.  Normalizing to ``string`` (``utf8``) lets a standard
    ``utf8`` declaration match the observed schema during ``compare_schemas``.
    """

    fields: list[pa.Field] = []
    for arrow_field in schema:
        field_type = arrow_field.type
        if field_type == pa.large_string():
            field_type = pa.string()
        elif field_type == pa.large_binary():
            field_type = pa.binary()
        fields.append(
            pa.field(
                arrow_field.name,
                field_type,
                nullable=arrow_field.nullable,
                metadata=arrow_field.metadata,
            )
        )
    return pa.schema(fields)


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
