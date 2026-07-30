"""Closed set of built-in source adapters.

This package owns the concrete adapters that satisfy the private
:class:`~selayer.sources.base.SourceAdapter` protocol.  The adapter registry
is a closed internal mapping keyed by connector kind; no public plugin
registration API is exposed.
"""

from __future__ import annotations

from selayer.sources.adapters.arrow import ArrowDatasetAdapter
from selayer.sources.adapters.database import (
    DuckDbAdapter,
    PostgresAdapter,
    SqliteAdapter,
)
from selayer.sources.adapters.delta import DeltaAdapter
from selayer.sources.adapters.iceberg import IcebergAdapter

__all__ = [
    "ArrowDatasetAdapter",
    "DeltaAdapter",
    "DuckDbAdapter",
    "IcebergAdapter",
    "PostgresAdapter",
    "SqliteAdapter",
]
