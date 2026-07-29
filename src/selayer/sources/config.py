"""Frozen, slotted connector configurations and a closed union type.

Each connector type maps to exactly one frozen ``@dataclass(slots=True)`` with
structural equality.  The :data:`SourceConnector` union is closed: every
adapter receives one of these concrete configs, never an arbitrary option
mapping.  :func:`connector_kind` returns the YAML discriminator for a config
without requiring ``isinstance`` switches outside this module.
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
    """Redact embedded URI userinfo (credentials) from a location string.

    Only the ``userinfo@`` segment of a URI authority is removed; the scheme,
    host, port, and path are preserved so diagnostics remain useful.  Strings
    without a ``scheme://user:password@host`` authority (local paths, plain
    references) are returned unchanged.
    """

    return _URI_USERINFO.sub(r"\1", location)


@dataclass(frozen=True, slots=True)
class ParquetConfig:
    """A Parquet file-or-directory source."""

    location: str
    credential_profile: str | None = None

    def __repr__(self) -> str:
        # Locations may carry URI userinfo (``s3://key:secret@bucket``);
        # redact it so credentials never surface in diagnostics.
        return (
            f"ParquetConfig(location={_sanitize_location(self.location)!r},"
            f" credential_profile={self.credential_profile!r})"
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
        # See ParquetConfig: redact any URI userinfo from the location.
        return (
            f"CsvConfig(location={_sanitize_location(self.location)!r},"
            f" credential_profile={self.credential_profile!r},"
            f" delimiter={self.delimiter!r},"
            f" quote_char={self.quote_char!r},"
            f" escape_char={self.escape_char!r},"
            f" has_header={self.has_header!r})"
        )


@dataclass(frozen=True, slots=True)
class DeltaConfig:
    """A Delta Lake table source identified by a directory location."""

    location: str
    credential_profile: str | None = None

    def __repr__(self) -> str:
        # See ParquetConfig: redact any URI userinfo from the location.
        return (
            f"DeltaConfig(location={_sanitize_location(self.location)!r},"
            f" credential_profile={self.credential_profile!r})"
        )


@dataclass(frozen=True, slots=True)
class IcebergConfig:
    """An Iceberg table source identified by catalog, namespace, and table."""

    catalog_profile: str
    namespace: tuple[str, ...]
    table: str


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    """A SQLite database file source."""

    location: str
    relation: str

    def __repr__(self) -> str:
        # See ParquetConfig: redact any URI userinfo from the location.
        return (
            f"SqliteConfig(location={_sanitize_location(self.location)!r},"
            f" relation={self.relation!r})"
        )


@dataclass(frozen=True, slots=True)
class DuckDbConfig:
    """A DuckDB database file source."""

    location: str
    relation: str
    read_only: bool = True

    def __repr__(self) -> str:
        # See ParquetConfig: redact any URI userinfo from the location.
        return (
            f"DuckDbConfig(location={_sanitize_location(self.location)!r},"
            f" relation={self.relation!r},"
            f" read_only={self.read_only!r})"
        )


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """A PostgreSQL relation accessed via a named connection profile."""

    connection_profile: str
    relation: str


@dataclass(frozen=True, slots=True)
class PyArrowConfig:
    """An in-memory PyArrow table registered under a named handle."""

    handle: str


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
