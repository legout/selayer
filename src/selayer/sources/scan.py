"""Bounded public source scan-session contract.

This module owns the public, immutable scan-session surface that
:meth:`~selayer.sources.registry.SourceRegistry.open_scan_session` produces:

* :class:`SourceConsistency` — the closed scan-consistency enum (re-exported
  from :mod:`selayer.sources.base`, where :class:`SourceHandle` carries it as
  the single canonical adapter token).
* :class:`SourceSnapshot` — an immutable, repr-safe derived view over a
  source's ``(consistency, snapshot_id, schema_fingerprint)`` triple.  It is
  *not* a second snapshot authority: ``snapshot_id`` derives from the existing
  canonical :attr:`SourceHandle.snapshot` token and ``schema_fingerprint``
  from the existing schema helper.
* :class:`SourceScanSession` — a bounded, context-managed session that streams
  typed :class:`pyarrow.RecordBatch` objects from one registered source while
  holding the registry lifecycle lock for its full lifetime.

Secrecy / safety invariants:

* **No raw connection or handle on the session.**  The session stores only the
  stream reader, two cleanup/inspection callbacks, and safe identifiers.  No
  execution-engine connection, adapter handle, resource, or profile value is
  ever exposed as a public attribute.
* **Repr-safe.**  Free-form token fields (``snapshot_id``, ``schema_fingerprint``)
  are redacted in :class:`SourceSnapshot`'s repr exactly like
  :class:`SourceStatus`; only the closed-set ``consistency`` token renders.
* **Sanitized failures.**  Every DuckDB / adapter failure surfaced by
  :meth:`SourceScanSession.iter_batches` and
  :meth:`SourceScanSession.recheck_snapshot` is a
  :class:`~selayer.sources.errors.SourceError` constructed *outside* an active
  ``except`` scope so ``__cause__``/``__context__`` remain ``None``.

The session is constructed by the registry (see
:meth:`~selayer.sources.registry.SourceRegistry.open_scan_session`); callers
never build one directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Self

import pyarrow as pa

from selayer.sources.base import (
    SourceConsistency,
    _render,
    _repr_consistency,
    _repr_literal,
)
from selayer.sources.errors import SourceConnectionError
from selayer.sources.schema import TableSchema

__all__ = [
    "SourceConsistency",
    "SourceScanSession",
    "SourceSnapshot",
]


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Immutable, repr-safe derived view over a source's snapshot triple.

    A *derived* public view — never a second authority:

    * ``consistency`` mirrors :attr:`SourceHandle.consistency`;
    * ``snapshot_id`` mirrors the canonical :attr:`SourceHandle.snapshot`
      token;
    * ``schema_fingerprint`` is derived through
      :func:`~selayer.sources.schema.schema_fingerprint`.

    Free-form token fields are redacted in the repr (mirroring
    :class:`~selayer.sources.base.SourceStatus`); only the closed-set
    ``consistency`` token renders.
    """

    consistency: SourceConsistency
    snapshot_id: str | None
    schema_fingerprint: str

    def __repr__(self) -> str:
        return _render(
            "SourceSnapshot",
            [
                ("consistency", _repr_consistency(self.consistency)),
                ("snapshot_id", _repr_literal(self.snapshot_id)),
                ("schema_fingerprint", _repr_literal(self.schema_fingerprint)),
            ],
        )


class SourceScanSession:
    """Bounded, context-managed scan session over one registered source.

    The public surface is intentionally narrow: ``source_id``, ``schema``,
    ``consistency``, ``snapshot_id``, :meth:`iter_batches`,
    :meth:`recheck_snapshot`, :meth:`cancel`, and the context-manager
    protocol.  No execution-engine connection, adapter handle, resource, or
    resolved profile value is ever exposed.

    A session holds the owning registry's lifecycle lock for its full lifetime
    (from ``open_scan_session`` until context-manager exit), so reload, close,
    query-binding, and execute on *that same registry* block while the session
    is open.  Discovery profiling must therefore use a dedicated registry and
    connection; proposal verification must never run inside an open profile
    session.

    The session is not constructed directly: use
    :meth:`~selayer.sources.registry.SourceRegistry.open_scan_session`.

    Attributes:
        source_id: stable identifier of the scanned source.
        schema: declared :class:`TableSchema` of the scanned source.
        consistency: :class:`SourceConsistency` advertised by the source.
        snapshot_id: canonical adapter snapshot token (``None`` for live).
    """

    __slots__ = (
        "_cancelled",
        "_closed",
        "_consistency",
        "_iterator_active",
        "_reader",
        "_recheck",
        "_release",
        "_schema",
        "_snapshot_id",
        "_source_id",
    )

    def __init__(
        self,
        *,
        source_id: str,
        schema: TableSchema,
        consistency: SourceConsistency,
        snapshot_id: str | None,
        reader: pa.RecordBatchReader,
        release: Callable[[], None],
        recheck: Callable[[], SourceSnapshot],
    ) -> None:
        self._source_id = source_id
        self._schema = schema
        self._consistency = consistency
        self._snapshot_id = snapshot_id
        self._reader: pa.RecordBatchReader | None = reader
        # ``release`` is idempotent: it exits the query-scoped binding
        # context and releases the registry lifecycle lock exactly once.  It
        # does *not* close the reader — the session owns that exclusively via
        # ``_close_reader`` so the reader is closed exactly once even when
        # ``cancel`` runs before context-manager exit.
        self._release = release
        # ``recheck`` prepares a fresh candidate through the owning adapter,
        # compares its ``(consistency, snapshot_id, schema_fingerprint)``
        # triple against the session's open-time baseline, and returns the
        # derived :class:`SourceSnapshot` (raising a sanitized
        # ``snapshot_mismatch`` error on any drift) without holding onto the
        # candidate.
        self._recheck = recheck
        self._closed = False
        self._cancelled = False
        self._iterator_active = False

    # -- read-only public surface -----------------------------------------

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def schema(self) -> TableSchema:
        return self._schema

    @property
    def consistency(self) -> SourceConsistency:
        return self._consistency

    @property
    def snapshot_id(self) -> str | None:
        return self._snapshot_id

    # -- streaming --------------------------------------------------------

    def iter_batches(self) -> Iterator[pa.RecordBatch]:
        """Stream :class:`pyarrow.RecordBatch` objects from the source.

        At most one iterator may be active per session at a time; a second
        call while one is active raises a sanitized
        :class:`~selayer.sources.errors.SourceConnectionError`.  After the
        previous iterator is drained (or the session is cancelled) a fresh
        iterator may be opened — though a single-pass source will then yield
        no further batches.

        Raises:
            SourceConnectionError: if the session is closed/cancelled, an
                iterator is already active, or a DuckDB read fails.
        """
        if self._closed or self._cancelled:
            raise SourceConnectionError(
                self._source_id, "scan_failed", "scan session is not usable"
            )
        if self._iterator_active:
            raise SourceConnectionError(
                self._source_id,
                "scan_failed",
                "an iterator is already active for this scan session",
            )
        self._iterator_active = True
        return self._stream()

    def _stream(self) -> Iterator[pa.RecordBatch]:
        """Generator backing :meth:`iter_batches`.

        The flag-based failure handling keeps the sanitized
        :class:`SourceConnectionError` constructed *outside* any active
        ``except`` scope so ``__cause__``/``__context__`` remain ``None``.
        A cancellation (which closes the reader) ends the stream cleanly
        without surfacing a raw driver error.
        """

        reader = self._reader
        if reader is None:
            # Should not happen: :meth:`iter_batches` guards against a closed
            # session.  Defensive return keeps the generator total.
            return
        read_failed = False
        try:
            while True:
                if self._cancelled:
                    break
                try:
                    batch = reader.read_next_batch()
                except StopIteration:
                    break
                except Exception:  # noqa: BLE001 - sanitize any read failure
                    if not self._cancelled:
                        read_failed = True
                    break
                yield batch
        finally:
            self._iterator_active = False
        if read_failed:
            # Raised outside the ``except`` scope so the secrecy invariant
            # (``__cause__``/``__context__`` both ``None``) holds.
            raise SourceConnectionError(
                self._source_id, "scan_failed", "the source could not be scanned"
            )

    # -- snapshot recheck -------------------------------------------------

    def recheck_snapshot(self) -> SourceSnapshot:
        """Prepare a fresh candidate, verify it matches the session, return it.

        The fresh candidate is prepared through the same adapter that owns the
        registered source, its ``(consistency, snapshot_id, schema_fingerprint)``
        triple is compared against the session's open-time baseline, and the
        candidate is closed before returning.  When every field still matches
        the derived :class:`SourceSnapshot` is returned; on any drift a
        sanitized :class:`~selayer.sources.errors.SourceConnectionError`
        (code ``snapshot_mismatch``) is raised so the caller learns
        deterministically that the snapshot it is streaming is no longer
        current.  The session's own stream is unaffected.

        Raises:
            SourceConnectionError: if the session is closed/cancelled, the
                fresh candidate cannot be prepared, or the fresh candidate no
                longer matches the session's snapshot (code
                ``snapshot_mismatch``).
        """
        if self._closed or self._cancelled:
            raise SourceConnectionError(
                self._source_id, "scan_failed", "scan session is not usable"
            )
        return self._recheck()

    # -- cancellation -----------------------------------------------------

    def cancel(self) -> None:
        """Interrupt the active cursor and mark the session unusable.

        Idempotent.  Closes the underlying stream reader *once* (interrupting
        any in-flight read) and marks the session cancelled so further
        :meth:`iter_batches` / :meth:`recheck_snapshot` calls raise.  The
        registry lifecycle lock remains held until the context-manager exits,
        at which point the binding and lock are released exactly once.
        """
        if self._cancelled:
            return
        self._cancelled = True
        self._close_reader()

    # -- context manager --------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._close()

    def _close_reader(self) -> None:
        """Close the stream reader exactly once (idempotent).

        Nulls the stored reader reference before the best-effort ``close`` so
        a second invocation (e.g. ``cancel()`` followed by context-manager
        exit) is a no-op and the reader is never closed twice.
        """
        reader = self._reader
        if reader is None:
            return
        self._reader = None
        with suppress(Exception):  # close is best-effort
            reader.close()

    def _close(self) -> None:
        """Release the reader, binding, and registry lock exactly once."""
        if self._closed:
            return
        self._closed = True
        # The session owns closing the stream reader exactly once; ``_release``
        # then exits the query-scoped binding context and releases the
        # registry lock.  ``_release`` is idempotent and suppresses every
        # cleanup-side error so a connector cleanup exception can never escape
        # raw or skip the lock release.
        self._close_reader()
        self._release()
