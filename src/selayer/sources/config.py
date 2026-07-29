"""Frozen, slotted connector configurations and a closed union type.

Each connector type maps to exactly one frozen ``@dataclass(slots=True)`` with
structural equality.  The :data:`SourceConnector` union is closed: every
adapter receives one of these concrete configs, never an arbitrary option
mapping.  :func:`connector_kind` returns the YAML discriminator for a config
without requiring ``isinstance`` switches outside this module.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class ParquetConfig:
    """A Parquet file-or-directory source."""

    location: str
    credential_profile: str | None = None


@dataclass(frozen=True, slots=True)
class CsvConfig:
    """A delimited-text source with configurable framing options."""

    location: str
    credential_profile: str | None = None
    delimiter: str = ","
    quote_char: str = '"'
    escape_char: str | None = None
    has_header: bool = True


@dataclass(frozen=True, slots=True)
class DeltaConfig:
    """A Delta Lake table source identified by a directory location."""

    location: str
    credential_profile: str | None = None


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


@dataclass(frozen=True, slots=True)
class DuckDbConfig:
    """A DuckDB database file source."""

    location: str
    relation: str
    read_only: bool = True


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
