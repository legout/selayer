"""Frozen, slotted connector configurations and a closed union type.

Each connector type maps to exactly one frozen ``@dataclass(slots=True)`` with
structural equality.  The :data:`SourceConnector` union is closed: every
adapter receives one of these concrete configs, never an arbitrary option
mapping.  :func:`connector_kind` returns the YAML discriminator for a config
without requiring ``isinstance`` switches outside this module.

**Secret-safe reprs.**  Every connector config — and
:class:`~selayer.sources.catalog.ParsedSource` — defines an explicit
``__repr__`` that routes *every* string-bearing field through the centralized
:func:`_safe` sanitizer (backed by :func:`_sanitize_location`).  The binding
requirement is that credentials and authenticated locations never appear in
reprs, catalogs, or error text; several string fields (``location``,
``table``, ``namespace``, ``handle``, ``grain``) are validated only as
non-empty strings and so could carry embedded URI userinfo, so the sanitizer
is applied defensively to all of them.  For credential-free values the
formatted repr is byte-identical to the default dataclass ``__repr__``, and
dataclass equality / required fields are unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CsvConfig",
    "DeltaConfig",
    "DuckDbConfig",
    "IcebergConfig",
    "ParquetConfig",
    "PostgresConfig",
    "PyArrowConfig",
    "SourceConnector",
    "SqliteConfig",
    "connector_kind",
]


# URI authority userinfo (``scheme://user:password@host``).  Only the
# ``userinfo@`` segment is matched so the scheme, host, port, and path — all
# useful diagnostics — are preserved while embedded credentials are redacted.
# The negated class stops at the first ``/``, ``?``, ``#``, or ``@`` so a path
# that legitimately contains ``@`` is never mistaken for userinfo.
_URI_USERINFO = re.compile(r"(://)[^@/?#]*@")


def _sanitize_location(location: str) -> str:
    """Redact embedded URI userinfo (credentials) from a string.

    Only the ``userinfo@`` segment of a URI authority is removed; the scheme,
    host, port, and path are preserved so diagnostics remain useful.  Strings
    without a ``scheme://user:password@host`` authority (local paths, plain
    references, validated identifiers) are returned unchanged.  This is the
    low-level redactor; :func:`_safe` is the field-level entry point used by
    every config ``__repr__``.
    """

    return _URI_USERINFO.sub(r"\1", location)


def _safe(value: object) -> object:
    """Return a repr-safe projection of a field value.

    The single sanitizer every connector ``__repr__`` routes string-bearing
    fields through:

    * **strings** are userinfo-redacted via :func:`_sanitize_location`;
    * **tuples** (e.g. ``namespace``, ``grain``) are projected element-wise so
      credentials nested inside a sequence are redacted too;
    * every other value (``None``, ``bool``, and nested objects such as a
      connector or schema) passes through unchanged so its own ``__repr__`` is
      used.

    For credential-free values the result is byte-identical to the default
    ``repr``, so dataclass equality semantics and rendered diagnostics are
    unchanged for well-formed inputs.
    """

    if isinstance(value, str):
        return _sanitize_location(value)
    if isinstance(value, tuple):
        return tuple(_safe(item) for item in value)
    return value


def _format_repr(name: str, fields: list[tuple[str, object]]) -> str:
    """Render a ``Name(field=value, ...)`` repr with all values sanitized.

    Each value is routed through :func:`_safe` before being formatted with
    ``!r``, so the rendered output is byte-identical to the default dataclass
    ``__repr__`` for credential-free inputs while guaranteeing no embedded URI
    userinfo can leak for any string-bearing field.
    """

    body = ", ".join(f"{field}={_safe(value)!r}" for field, value in fields)
    return f"{name}({body})"


@dataclass(frozen=True, slots=True)
class ParquetConfig:
    """A Parquet file-or-directory source."""

    location: str
    credential_profile: str | None = None

    def __repr__(self) -> str:
        # Every string-bearing field is routed through the centralized
        # sanitizer so embedded URI userinfo can never surface in diagnostics.
        return _format_repr(
            "ParquetConfig",
            [
                ("location", self.location),
                ("credential_profile", self.credential_profile),
            ],
        )


@dataclass(frozen=True, slots=True)
class CsvConfig:
    """A delimited-text source with configurable framing options."""

    location: str
    credential_profile: str | None = None
    delimiter: str = ","
    quote_char: str = '"'
    escape_char: str | None = None
    has_header: bool = True

    def __repr__(self) -> str:
        return _format_repr(
            "CsvConfig",
            [
                ("location", self.location),
                ("credential_profile", self.credential_profile),
                ("delimiter", self.delimiter),
                ("quote_char", self.quote_char),
                ("escape_char", self.escape_char),
                ("has_header", self.has_header),
            ],
        )


@dataclass(frozen=True, slots=True)
class DeltaConfig:
    """A Delta Lake table source identified by a directory location."""

    location: str
    credential_profile: str | None = None

    def __repr__(self) -> str:
        return _format_repr(
            "DeltaConfig",
            [
                ("location", self.location),
                ("credential_profile", self.credential_profile),
            ],
        )


@dataclass(frozen=True, slots=True)
class IcebergConfig:
    """An Iceberg table source identified by catalog, namespace, and table."""

    catalog_profile: str
    namespace: tuple[str, ...]
    table: str

    def __repr__(self) -> str:
        # ``namespace`` and ``table`` are validated only as non-empty strings,
        # so they could carry URI userinfo; both are sanitized here.
        return _format_repr(
            "IcebergConfig",
            [
                ("catalog_profile", self.catalog_profile),
                ("namespace", self.namespace),
                ("table", self.table),
            ],
        )


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    """A SQLite database file source."""

    location: str
    relation: str

    def __repr__(self) -> str:
        return _format_repr(
            "SqliteConfig",
            [("location", self.location), ("relation", self.relation)],
        )


@dataclass(frozen=True, slots=True)
class DuckDbConfig:
    """A DuckDB database file source."""

    location: str
    relation: str
    read_only: bool = True

    def __repr__(self) -> str:
        return _format_repr(
            "DuckDbConfig",
            [
                ("location", self.location),
                ("relation", self.relation),
                ("read_only", self.read_only),
            ],
        )


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """A PostgreSQL relation accessed via a named connection profile."""

    connection_profile: str
    relation: str

    def __repr__(self) -> str:
        # ``relation`` is validated as a SQL identifier, but the sanitizer is
        # applied defensively so credentials can never surface regardless.
        return _format_repr(
            "PostgresConfig",
            [
                ("connection_profile", self.connection_profile),
                ("relation", self.relation),
            ],
        )


@dataclass(frozen=True, slots=True)
class PyArrowConfig:
    """An in-memory PyArrow table registered under a named handle."""

    handle: str

    def __repr__(self) -> str:
        # ``handle`` is validated only as a non-empty string; sanitize it so
        # embedded URI userinfo can never surface in diagnostics.
        return _format_repr("PyArrowConfig", [("handle", self.handle)])


type SourceConnector = (
    ParquetConfig
    | CsvConfig
    | DeltaConfig
    | IcebergConfig
    | SqliteConfig
    | DuckDbConfig
    | PostgresConfig
    | PyArrowConfig
)


def connector_kind(connector: SourceConnector) -> str:
    """Return the exact YAML type discriminator for a connector config.

    This is the single place that discriminates among connector variants;
    callers outside :mod:`selayer.sources.config` should use this helper
    rather than repeated ``isinstance`` checks.
    """

    match connector:
        case ParquetConfig():
            return "parquet"
        case CsvConfig():
            return "csv"
        case DeltaConfig():
            return "delta"
        case IcebergConfig():
            return "iceberg"
        case SqliteConfig():
            return "sqlite"
        case DuckDbConfig():
            return "duckdb"
        case PostgresConfig():
            return "postgres"
        case PyArrowConfig():
            return "pyarrow"
