"""Public source lifecycle surface.

This package-level module re-exports the bounded public scan-session surface
introduced by Task 4 so callers can import the stable types directly from
:mod:`selayer.sources`:

* :class:`~selayer.sources.scan.SourceScanSession` — a bounded, context-managed
  session that streams typed :class:`pyarrow.RecordBatch` objects from one
  registered source.
* :class:`~selayer.sources.scan.SourceSnapshot` — an immutable, repr-safe
  derived view over a source's ``(consistency, snapshot_id, schema_fingerprint)``
  triple.
* :class:`~selayer.sources.base.SourceConsistency` — the closed
  scan-consistency enum (the single canonical adapter token carried on
  :class:`~selayer.sources.base.SourceHandle`).

These types are produced by
:meth:`~selayer.sources.registry.SourceRegistry.open_scan_session` and are
never constructed directly.  Adapter internals, the registry, raw handles,
profile values, and schema-parser internals remain private to the package (see
``tests/sources/test_public_lifecycle_api.py``).
"""

from __future__ import annotations

from selayer.sources.base import SourceConsistency
from selayer.sources.scan import SourceScanSession, SourceSnapshot

__all__ = [
    "SourceConsistency",
    "SourceScanSession",
    "SourceSnapshot",
]
