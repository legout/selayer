"""Exact bounded aggregate source profiling.

This module owns the deterministic profiling surface that turns a bounded
:class:`~selayer.sources.scan.SourceScanSession` into an *exact aggregate*
profile without ever exposing model-visible values:

* :class:`ProfileRunner` — one-pass local accumulation with a bounded on-disk
  spill.  It streams typed Arrow batches one at a time into a restrictive
  temporary directory under the ignored session workspace, runs exact DuckDB
  aggregates over the spilled data, and deletes the spill once the aggregate
  artifact is committed.
* :class:`SourceProfile` — the model-safe aggregate artifact: row count, nulls,
  distinct counts, numeric/date/timestamp ranges, grain duplicate counts,
  schema fingerprint, snapshot id, consistency mode, outcome, and stable
  per-batch content hashes.  No top values, example rows, spill paths, or
  source locations ever appear in :meth:`SourceProfile.to_dict`,
  :meth:`SourceProfile.fingerprint`, or any diagnostic.

Secrecy / safety invariants:

* **Bounded spill.**  A single batch is held in memory at a time; the rest are
  on disk under a mode-``0700`` directory with mode-``0600`` files.  Spill
  paths and values are never rendered.
* **Exact aggregates only.**  DuckDB computes ``COUNT``, ``COUNT(DISTINCT)``,
  and ``MIN``/``MAX`` over the full spilled relation, so the profile is exact,
  not sampled.
* **No partial claims.**  Timeout, cancellation, and partial iterator failures
  discard every partial aggregate and record an ``unavailable`` outcome with a
  stable reason.  A bounded partial scan (reopenable only) preserves its spill
  for resume but emits no aggregate claims.
* **Consistency rules.**  Only reopenable profiles may resume, and only with
  the same snapshot token and batch hashes; transaction and live profiles
  always restart.
* **Model-safe metadata.**  Ranges are reported only for numeric, decimal,
  date, and timestamp columns and are rendered as canonical strings; free-text
  and binary columns get null/distinct counts but never a value-bearing range.

The runner consumes a real :class:`SourceScanSession` (opened by a dedicated
discovery registry) and never accepts SQL, credentials, or a connection.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa
import pyarrow.dataset as padataset

from selayer.compilation.duckdb import quote_identifier
from selayer.sources.base import SourceConsistency
from selayer.sources.schema import (
    DecimalType,
    DictionaryType,
    DurationType,
    FixedSizeBinaryType,
    FixedSizeListType,
    LargeListType,
    ListType,
    MapType,
    ScalarType,
    StructType,
    TimeType,
    TimestampType,
    schema_fingerprint,
)

if TYPE_CHECKING:
    import threading

    from selayer.sources.scan import SourceScanSession

__all__ = [
    "DEFAULT_PROFILE_TIMEOUT_SECONDS",
    "ColumnProfile",
    "ProfileCheckpoint",
    "ProfileMode",
    "ProfileOutcome",
    "ProfileRunner",
    "ProfileUnavailableReason",
    "SourceProfile",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default per-source profile deadline in seconds (15 minutes).
DEFAULT_PROFILE_TIMEOUT_SECONDS: float = 900.0

#: Scalar logical-type names that carry an orderable numeric range.
_NUMERIC_SCALARS: frozenset[str] = frozenset(
    {
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float16",
        "float32",
        "float64",
    }
)

#: Scalar logical-type names that carry an orderable calendar range.
_DATE_SCALARS: frozenset[str] = frozenset({"date32", "date64"})

_SPILL_GLOB: str = "*.arrow"


# --------------------------------------------------------------------------- #
# Enums                                                                       #
# --------------------------------------------------------------------------- #


class ProfileMode(StrEnum):
    """Resume semantics derived from a source's scan consistency."""

    REOPENABLE = "reopenable"
    TRANSACTION = "transaction"
    LIVE = "live"


class ProfileOutcome(StrEnum):
    """Terminal outcome of a profile run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ProfileUnavailableReason(StrEnum):
    """Stable reason an aggregate profile is unavailable."""

    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ITERATOR_FAILURE = "iterator_failure"


# --------------------------------------------------------------------------- #
# Profile value types                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Model-safe per-column aggregate.

    ``min_value``/``max_value`` are canonical strings reported only for
    numeric, decimal, date, and timestamp columns (``has_range``); they never
    hold a free-text or binary value.
    """

    name: str
    type_name: str
    null_count: int | None
    distinct_count: int | None
    min_value: str | None
    max_value: str | None
    has_range: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "null_count": self.null_count,
            "distinct_count": self.distinct_count,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "has_range": self.has_range,
        }


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Exact aggregate profile of one source scan.

    The profile is model-safe: :meth:`to_dict` and :meth:`fingerprint` expose
    only aggregate counts, ranges, digests, and outcome metadata.  No raw row,
    top value, example row, spill path, or source location is ever rendered.
    """

    source_id: str
    schema_fingerprint: str
    consistency: str
    snapshot_id: str | None
    mode: ProfileMode
    outcome: ProfileOutcome
    unavailable_reason: ProfileUnavailableReason | None
    row_count: int | None
    batch_count: int
    batch_hashes: tuple[str, ...]
    columns: tuple[ColumnProfile, ...]
    grain_duplicate_count: int | None

    @property
    def is_available(self) -> bool:
        """``True`` only when the profile committed exact aggregates."""

        return self.outcome is ProfileOutcome.COMPLETED

    @property
    def fingerprint(self) -> str:
        """Canonical SHA-256 fingerprint of the aggregate content.

        Volatile fields are excluded so two profiles of the same source data
        always share a fingerprint.
        """

        payload: dict[str, object] = {
            "source_id": self.source_id,
            "schema_fingerprint": self.schema_fingerprint,
            "consistency": self.consistency,
            "snapshot_id": self.snapshot_id,
            "mode": self.mode.value,
            "outcome": self.outcome.value,
            "unavailable_reason": (
                self.unavailable_reason.value
                if self.unavailable_reason is not None
                else None
            ),
            "row_count": self.row_count,
            "batch_hashes": list(self.batch_hashes),
            "columns": [col.to_dict() for col in self.columns],
            "grain_duplicate_count": self.grain_duplicate_count,
        }
        return _fingerprint(payload)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic, model-safe JSON mapping."""

        return {
            "source_id": self.source_id,
            "schema_fingerprint": self.schema_fingerprint,
            "consistency": self.consistency,
            "snapshot_id": self.snapshot_id,
            "mode": self.mode.value,
            "outcome": self.outcome.value,
            "unavailable_reason": (
                self.unavailable_reason.value
                if self.unavailable_reason is not None
                else None
            ),
            "row_count": self.row_count,
            "batch_count": self.batch_count,
            "batch_hashes": list(self.batch_hashes),
            "grain_duplicate_count": self.grain_duplicate_count,
            "columns": [col.to_dict() for col in self.columns],
        }

    def checkpoint(self) -> ProfileCheckpoint:
        """Return a resume checkpoint for this profile.

        Only meaningful for a :attr:`ProfileOutcome.PARTIAL` profile of a
        reopenable source; callers use it to resume the remaining batches.
        """

        return ProfileCheckpoint(
            snapshot_id=self.snapshot_id,
            mode=self.mode,
            batch_hashes=self.batch_hashes,
        )


@dataclass(frozen=True, slots=True)
class ProfileCheckpoint:
    """Resume descriptor for a reopenable partial profile."""

    snapshot_id: str | None
    mode: ProfileMode
    batch_hashes: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _fingerprint(payload: object) -> str:
    """Canonical SHA-256 hex digest using the discovery normalizer."""

    from selayer_discovery.canonical import fingerprint

    return fingerprint(payload)


def _mode_from_consistency(consistency: SourceConsistency) -> ProfileMode:
    if consistency is SourceConsistency.REOPENABLE_SNAPSHOT:
        return ProfileMode.REOPENABLE
    if consistency is SourceConsistency.TRANSACTION_SNAPSHOT:
        return ProfileMode.TRANSACTION
    return ProfileMode.LIVE


def _has_range(logical_type: object) -> bool:
    """Return ``True`` when a min/max range is value-safe for the type."""

    if isinstance(logical_type, ScalarType):
        return logical_type.name in _NUMERIC_SCALARS or logical_type.name in _DATE_SCALARS
    return isinstance(logical_type, (DecimalType, TimestampType))


def _type_name(logical_type: object) -> str:
    """Return a canonical, model-safe logical-type label."""

    if isinstance(logical_type, ScalarType):
        return logical_type.name
    if isinstance(logical_type, TimestampType):
        return "timestamp"
    if isinstance(logical_type, DecimalType):
        return "decimal"
    if isinstance(logical_type, TimeType):
        return "time"
    if isinstance(logical_type, DurationType):
        return "duration"
    if isinstance(logical_type, ListType):
        return "list"
    if isinstance(logical_type, LargeListType):
        return "large_list"
    if isinstance(logical_type, FixedSizeListType):
        return "fixed_size_list"
    if isinstance(logical_type, StructType):
        return "struct"
    if isinstance(logical_type, MapType):
        return "map"
    if isinstance(logical_type, DictionaryType):
        return "dictionary"
    if isinstance(logical_type, FixedSizeBinaryType):
        return "fixed_size_binary"
    return "unknown"


def _hash_batch(batch: pa.RecordBatch) -> str:
    """Return the SHA-256 hex digest of a batch's serialized Arrow payload."""

    return hashlib.sha256(batch.serialize()).hexdigest()


def _canonical_scalar(value: object) -> str | None:
    """Render an aggregate scalar as a canonical string, or ``None``.

    Only value-safe orderable scalars (numbers, decimals, dates, timestamps)
    are rendered; everything else yields ``None`` so no free text leaks.
    """

    # ``bool`` is a subclass of ``int``; ranges are not reported for booleans.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else repr(value)
    if isinstance(value, Decimal):
        return str(value)
    # ``datetime`` is a subclass of ``date`` — check it first so a timestamp
    # renders with its time component.
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _restrict(path: Path, mode: int) -> None:
    """Apply owner-only permissions on POSIX (best-effort elsewhere)."""

    if os.name == "posix":
        os.chmod(path, mode)


# --------------------------------------------------------------------------- #
# ProfileRunner                                                               #
# --------------------------------------------------------------------------- #


class ProfileRunner:
    """One-pass bounded-spill aggregate profiler for one scan session.

    Construct one runner per scan session.  :meth:`run` streams every batch
    from the session exactly once, spills each to a restrictive on-disk file,
    runs exact DuckDB aggregates over the spilled relation, and deletes the
    spill once the aggregate is committed.

    Args:
        session: an open :class:`~selayer.sources.scan.SourceScanSession`.
        spill_root: restrictive temporary directory for spilled Arrow batches
            (created with mode ``0700``; files are mode ``0600``).
        timeout: per-source deadline in seconds (default 900).
        grain: grain column names for duplicate counting; empty disables it.
        cancel_event: optional cooperative cancellation signal.  When set
            between batches the scan is cancelled and the profile is recorded
            ``unavailable`` (reason ``cancelled``).
        stop_after_batches: bounded partial-scan seam (reopenable only).  When
            set, the runner stops after spilling that many batches, preserves
            the spill for resume, and records a ``partial`` profile with no
            aggregate claims.
    """

    def __init__(
        self,
        session: SourceScanSession,
        spill_root: Path,
        *,
        timeout: float = DEFAULT_PROFILE_TIMEOUT_SECONDS,
        grain: Iterable[str] = (),
        cancel_event: threading.Event | None = None,
        stop_after_batches: int | None = None,
    ) -> None:
        self._session = session
        self._spill_root = Path(spill_root)
        self._timeout = timeout
        self._grain = tuple(grain)
        self._cancel_event = cancel_event
        self._stop_after_batches = stop_after_batches

    # -- public entry point ---------------------------------------------- #

    def run(
        self,
        *,
        checkpoint: ProfileCheckpoint | None = None,
    ) -> SourceProfile:
        """Run the aggregate profile and return the model-safe result.

        Args:
            checkpoint: optional resume descriptor.  Resume is honored only for
                a reopenable source whose snapshot token and mode match the
                checkpoint; every other case restarts from scratch.
        """

        mode = _mode_from_consistency(self._session.consistency)
        schema_fp = schema_fingerprint(self._session.schema)
        consistency = self._session.consistency.value
        snapshot_id = self._session.snapshot_id

        resume = checkpoint is not None and self._resume_allowed(checkpoint, mode)
        if not resume:
            # A fresh run (or an invalidated checkpoint) discards any stale
            # spill so old batches can never pollute the new aggregate.
            self._clear_spill()

        batch_hashes = self._stream(checkpoint if resume else None, mode)
        outcome, reason, hashes = batch_hashes

        if outcome is ProfileOutcome.UNAVAILABLE:
            # Interrupt the scan, wait for cleanup, and discard every partial
            # aggregate so no incomplete claim can ever surface.
            self._session.cancel()
            self._drain(hashes.iterator)
            self._clear_spill()
            return self._profile_unavailable(
                mode, reason, schema_fp, consistency, snapshot_id
            )

        if outcome is ProfileOutcome.PARTIAL:
            # Reopenable partial scan: preserve the spill for resume but emit
            # no aggregate claims.
            return self._profile_partial(mode, hashes.hashes, schema_fp, consistency, snapshot_id)

        # Completed: run exact aggregates, then delete the committed spill.
        profile = self._aggregate(
            mode, hashes.hashes, schema_fp, consistency, snapshot_id
        )
        self._clear_spill()
        return profile

    # -- resume policy --------------------------------------------------- #

    def _resume_allowed(
        self,
        checkpoint: ProfileCheckpoint,
        mode: ProfileMode,
    ) -> bool:
        """Resume is allowed only for a matching reopenable snapshot."""

        return (
            mode is ProfileMode.REOPENABLE
            and checkpoint.mode is ProfileMode.REOPENABLE
            and checkpoint.snapshot_id == self._session.snapshot_id
        )

    # -- streaming + spill ----------------------------------------------- #

    def _stream(
        self,
        checkpoint: ProfileCheckpoint | None,
        mode: ProfileMode,
    ) -> tuple[ProfileOutcome, ProfileUnavailableReason | None, _StreamResult]:
        deadline = time.monotonic() + self._timeout
        hashes: list[str] = []
        index = self._existing_spill_count()
        pos = 0
        outcome = ProfileOutcome.COMPLETED
        reason: ProfileUnavailableReason | None = None
        iterator = self._session.iter_batches()
        while True:
            if self._cancel_event is not None and self._cancel_event.is_set():
                outcome, reason = (
                    ProfileOutcome.UNAVAILABLE,
                    ProfileUnavailableReason.CANCELLED,
                )
                break
            if time.monotonic() >= deadline:
                outcome, reason = (
                    ProfileOutcome.UNAVAILABLE,
                    ProfileUnavailableReason.TIMEOUT,
                )
                break
            try:
                batch = next(iterator)
            except StopIteration:
                break
            except Exception:  # noqa: BLE001 - any scan failure is unavailable
                outcome, reason = (
                    ProfileOutcome.UNAVAILABLE,
                    ProfileUnavailableReason.ITERATOR_FAILURE,
                )
                break
            digest = _hash_batch(batch)
            already_spilled = (
                checkpoint is not None
                and pos < len(checkpoint.batch_hashes)
                and digest == checkpoint.batch_hashes[pos]
            )
            if already_spilled:
                # Reuse the prior partial run's spill file; do not rewrite it.
                hashes.append(digest)
            else:
                self._spill_batch(batch, index, digest)
                hashes.append(digest)
                index += 1
            pos += 1
            if (
                mode is ProfileMode.REOPENABLE
                and self._stop_after_batches is not None
                and len(hashes) >= self._stop_after_batches
            ):
                outcome = ProfileOutcome.PARTIAL
                break
        return outcome, reason, _StreamResult(tuple(hashes), iterator)

    def _drain(self, iterator: Iterator[pa.RecordBatch]) -> None:
        """Exhaust a (likely cancelled) iterator so its cleanup finalizes."""

        try:
            for _ in iterator:
                pass
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    # -- spill filesystem ------------------------------------------------ #

    def _ensure_spill_dir(self) -> None:
        self._spill_root.mkdir(parents=True, exist_ok=True)
        _restrict(self._spill_root, 0o700)

    def _clear_spill(self) -> None:
        """Delete every spilled batch and (re)create the restrictive dir."""

        if self._spill_root.exists():
            for child in self._spill_root.iterdir():
                if child.is_dir():
                    self._remove_tree(child)
                else:
                    child.unlink(missing_ok=True)
        self._ensure_spill_dir()

    @staticmethod
    def _remove_tree(root: Path) -> None:
        for child in root.iterdir():
            if child.is_dir():
                ProfileRunner._remove_tree(child)
            else:
                child.unlink(missing_ok=True)
        root.rmdir()

    def _spill_files(self) -> list[Path]:
        if not self._spill_root.exists():
            return []
        return sorted(self._spill_root.glob(_SPILL_GLOB))

    def _existing_spill_count(self) -> int:
        return len(self._spill_files())

    def _spill_batch(self, batch: pa.RecordBatch, index: int, digest: str) -> None:
        self._ensure_spill_dir()
        path = self._spill_root / f"batch-{index:06d}-{digest[:16]}.arrow"
        with pa.OSFile(str(path), "wb") as sink:
            with pa.ipc.new_file(sink, batch.schema) as writer:
                writer.write_batch(batch)
        _restrict(path, 0o600)

    # -- exact DuckDB aggregates ----------------------------------------- #

    def _aggregate(
        self,
        mode: ProfileMode,
        batch_hashes: tuple[str, ...],
        schema_fingerprint: str,
        consistency: str,
        snapshot_id: str | None,
    ) -> SourceProfile:
        fields = self._session.schema.fields
        files = self._spill_files()
        if not files:
            row_count = 0
            columns = tuple(self._empty_columns(fields))
            grain_dup: int | None = 0 if self._grain else None
        else:
            dataset = padataset.dataset([str(path) for path in files], format="ipc")
            connection = duckdb.connect()
            connection.register("spill", dataset)
            row_count = self._row_count(connection)
            columns = tuple(self._column_profiles(connection, fields, row_count))
            grain_dup = self._grain_duplicates(connection, row_count)
        return SourceProfile(
            source_id=self._session.source_id,
            schema_fingerprint=schema_fingerprint,
            consistency=consistency,
            snapshot_id=snapshot_id,
            mode=mode,
            outcome=ProfileOutcome.COMPLETED,
            unavailable_reason=None,
            row_count=row_count,
            batch_count=len(batch_hashes),
            batch_hashes=batch_hashes,
            columns=columns,
            grain_duplicate_count=grain_dup,
        )

    def _empty_columns(
        self, fields: tuple[Any, ...]
    ) -> Iterator[ColumnProfile]:
        for field in fields:
            yield ColumnProfile(
                name=field.name,
                type_name=_type_name(field.type),
                null_count=0,
                distinct_count=0,
                min_value=None,
                max_value=None,
                has_range=_has_range(field.type),
            )

    def _row_count(self, connection: Any) -> int:
        value = self._scalar(connection, "SELECT COUNT(*) FROM spill")
        return value if isinstance(value, int) else 0

    def _column_profiles(
        self,
        connection: Any,
        fields: tuple[Any, ...],
        row_count: int,
    ) -> Iterator[ColumnProfile]:
        for field in fields:
            name = field.name
            quoted = quote_identifier(name)
            non_null = self._scalar(connection, f"SELECT COUNT({quoted}) FROM spill")
            null_count: int | None
            if isinstance(non_null, int):
                null_count = max(row_count - non_null, 0)
            else:
                null_count = None
            distinct = self._scalar(
                connection, f"SELECT COUNT(DISTINCT {quoted}) FROM spill"
            )
            distinct_count = distinct if isinstance(distinct, int) else None
            min_value: str | None = None
            max_value: str | None = None
            ranged = _has_range(field.type)
            if ranged:
                min_raw = self._scalar(connection, f"SELECT MIN({quoted}) FROM spill")
                max_raw = self._scalar(connection, f"SELECT MAX({quoted}) FROM spill")
                min_value = _canonical_scalar(min_raw)
                max_value = _canonical_scalar(max_raw)
            yield ColumnProfile(
                name=name,
                type_name=_type_name(field.type),
                null_count=null_count,
                distinct_count=distinct_count,
                min_value=min_value,
                max_value=max_value,
                has_range=ranged,
            )

    def _grain_duplicates(self, connection: Any, row_count: int) -> int | None:
        if not self._grain:
            return None
        # Grain columns are caller-supplied identifiers; restrict them to the
        # declared schema columns (mirroring the registry's scan-column
        # validation) before quoting, so an unknown name can never reach DuckDB.
        known = {field.name for field in self._session.schema.fields}
        safe_grain = tuple(col for col in self._grain if col in known)
        if not safe_grain:
            return None
        grain_cols = ", ".join(quote_identifier(col) for col in safe_grain)
        distinct_rows = self._scalar(
            connection,
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {grain_cols} FROM spill)",
        )
        if not isinstance(distinct_rows, int):
            return None
        return max(row_count - distinct_rows, 0)

    @staticmethod
    def _scalar(connection: Any, sql: str) -> object:
        try:
            row = connection.execute(sql).fetchone()
        except Exception:  # noqa: BLE001 - graceful per-column degradation
            return None
        if row is None:
            return None
        return row[0]

    # -- non-completed profile builders ---------------------------------- #

    def _profile_unavailable(
        self,
        mode: ProfileMode,
        reason: ProfileUnavailableReason | None,
        schema_fingerprint: str,
        consistency: str,
        snapshot_id: str | None,
    ) -> SourceProfile:
        return SourceProfile(
            source_id=self._session.source_id,
            schema_fingerprint=schema_fingerprint,
            consistency=consistency,
            snapshot_id=snapshot_id,
            mode=mode,
            outcome=ProfileOutcome.UNAVAILABLE,
            unavailable_reason=reason,
            row_count=None,
            batch_count=0,
            batch_hashes=(),
            columns=(),
            grain_duplicate_count=None,
        )

    def _profile_partial(
        self,
        mode: ProfileMode,
        batch_hashes: tuple[str, ...],
        schema_fingerprint: str,
        consistency: str,
        snapshot_id: str | None,
    ) -> SourceProfile:
        return SourceProfile(
            source_id=self._session.source_id,
            schema_fingerprint=schema_fingerprint,
            consistency=consistency,
            snapshot_id=snapshot_id,
            mode=mode,
            outcome=ProfileOutcome.PARTIAL,
            unavailable_reason=None,
            row_count=None,
            batch_count=len(batch_hashes),
            batch_hashes=batch_hashes,
            columns=(),
            grain_duplicate_count=None,
        )


@dataclass(frozen=True, slots=True)
class _StreamResult:
    """Internal result of the streaming phase."""

    hashes: tuple[str, ...]
    iterator: Iterator[pa.RecordBatch]
