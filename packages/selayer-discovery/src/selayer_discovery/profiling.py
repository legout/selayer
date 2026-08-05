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
* **No partial claims.**  Timeout, cancellation, partial iterator failures,
  an unsupported column type, and a checkpoint/stream batch-hash mismatch all
  discard every partial aggregate (and the spill) and record an ``unavailable``
  outcome with a stable reason.  A bounded partial scan (reopenable only)
  preserves its spill for resume but emits no aggregate claims.
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
import heapq
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
    TimestampType,
    TimeType,
    schema_fingerprint,
)
from selayer_discovery.model import SCHEMA_VERSION

if TYPE_CHECKING:
    from selayer.sources.scan import SourceScanSession

__all__ = [
    "DEFAULT_PROFILE_TIMEOUT_SECONDS",
    "MAX_REVEALED_DISTINCT_VALUES",
    "MAX_SAMPLE_BYTES_PER_SESSION",
    "MAX_SAMPLE_BYTES_PER_SOURCE",
    "MAX_SAMPLE_FIELDS_PER_SOURCE",
    "MAX_SAMPLE_ROWS_PER_SOURCE",
    "ColumnProfile",
    "ContextExportError",
    "FieldClassification",
    "FieldPolicy",
    "FieldTransform",
    "PolicyActivation",
    "ProfileCheckpoint",
    "ProfileMode",
    "ProfileOutcome",
    "ProfilePolicyError",
    "ProfileRunner",
    "ProfileUnavailableReason",
    "RedactedSampleExport",
    "SampleCaps",
    "SamplePolicy",
    "SourceProfile",
    "build_context_export",
    "classify_field",
    "hard_deny_scan",
    "propose_policy",
    "select_sample_rows",
    "verify_activation",
]

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default per-source profile deadline in seconds (15 minutes).
DEFAULT_PROFILE_TIMEOUT_SECONDS: float = 900.0

#: Maximum time to wait for a watchdog thread to exit after the scan finishes.
_WATCHDOG_JOIN_SECONDS: float = 5.0

#: Spill batch filename: ``batch-{zero-padded index}-{full 64-hex digest}.arrow``.
#: The full digest lets resume validate that preserved spill files exactly match
#: a checkpoint's batch hashes before reusing them.
_SPILL_GLOB: str = "*.arrow"
_SPILL_FILE_RE: re.Pattern[str] = re.compile(
    r"\Abatch-(\d+)-([0-9a-f]{64})\.arrow\Z"
)

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
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    UNSUPPORTED_TYPE = "unsupported_type"


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

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ColumnProfile:
        """Reconstruct a column profile from its :meth:`to_dict` mapping."""

        return cls(
            name=str(data["name"]),
            type_name=str(data["type_name"]),
            null_count=_opt_int(data.get("null_count")),
            distinct_count=_opt_int(data.get("distinct_count")),
            min_value=_opt_str(data.get("min_value")),
            max_value=_opt_str(data.get("max_value")),
            has_range=bool(data.get("has_range", False)),
        )


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

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SourceProfile:
        """Reconstruct a source profile from its :meth:`to_dict` mapping."""

        raw_columns = data.get("columns", ())
        if isinstance(raw_columns, str) or not isinstance(raw_columns, Sequence):
            raise ProfilePolicyError(CODE_PROFILE_INVALID)
        columns = tuple(
            ColumnProfile.from_dict(col)  # type: ignore[arg-type]
            for col in raw_columns
            if isinstance(col, Mapping)
        )
        raw_hashes = data.get("batch_hashes", ())
        if isinstance(raw_hashes, str) or not isinstance(raw_hashes, Sequence):
            raise ProfilePolicyError(CODE_PROFILE_INVALID)
        batch_hashes = tuple(str(h) for h in raw_hashes)
        reason_raw = data.get("unavailable_reason")
        return cls(
            source_id=str(data["source_id"]),
            schema_fingerprint=str(data["schema_fingerprint"]),
            consistency=str(data["consistency"]),
            snapshot_id=_opt_str(data.get("snapshot_id")),
            mode=ProfileMode(str(data["mode"])),
            outcome=ProfileOutcome(str(data["outcome"])),
            unavailable_reason=(
                ProfileUnavailableReason(str(reason_raw))
                if reason_raw is not None
                else None
            ),
            row_count=_opt_int(data.get("row_count")),
            batch_count=_as_int(data.get("batch_count"), 0),
            batch_hashes=batch_hashes,
            columns=columns,
            grain_duplicate_count=_opt_int(data.get("grain_duplicate_count")),
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


def _profile_supported(logical_type: object) -> bool:
    """Return ``True`` when a column type can be exactly, safely aggregated.

    The profiler guarantees exact ``COUNT``/``COUNT(DISTINCT)`` and an
    orderable ``MIN``/``MAX`` range only for scalar, decimal, and timestamp
    logical types.  Every other type (time, duration, interval, fixed-size and
    large binary, list/large-list/fixed-size-list, struct, map, dictionary, and
    unknown types) cannot be truthfully aggregated, so a source carrying one
    yields an ``unavailable`` outcome rather than a completed profile with
    degraded (``None``) counts.
    """

    return isinstance(logical_type, (ScalarType, DecimalType, TimestampType))


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

        # A column type the profiler cannot exactly/safely aggregate yields a
        # stable ``unavailable`` outcome with no aggregate claims (never a
        # completed profile carrying degraded ``None`` counts).  Discard any
        # preserved spill so a prior partial run can never leak through.
        if self._has_unsupported_type():
            self._clear_spill()
            return self._profile_unavailable(
                mode,
                ProfileUnavailableReason.UNSUPPORTED_TYPE,
                schema_fp,
                consistency,
                snapshot_id,
            )

        resume_checkpoint = (
            checkpoint
            if checkpoint is not None and self._resume_allowed(checkpoint, mode)
            else None
        )
        if resume_checkpoint is not None and not self._spill_matches_checkpoint(
            resume_checkpoint
        ):
            # The preserved spill does not correspond to the checkpoint's batch
            # hashes (stale or corrupt from a prior run).  Resume is unsafe:
            # restart from scratch so old batches can never pollute the new
            # aggregate.  This is the only mismatch that can safely restart,
            # because nothing has been streamed yet.
            resume_checkpoint = None
        if resume_checkpoint is None:
            # A fresh run (or an invalidated checkpoint) discards any stale
            # spill so old batches can never pollute the new aggregate.
            self._clear_spill()

        batch_hashes = self._stream(resume_checkpoint, mode)
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

    def _has_unsupported_type(self) -> bool:
        """Return ``True`` if any column type cannot be exactly aggregated."""

        return any(
            not _profile_supported(field.type)
            for field in self._session.schema.fields
        )

    def _spill_matches_checkpoint(
        self, checkpoint: ProfileCheckpoint
    ) -> bool:
        """Return ``True`` when preserved spill files match the checkpoint.

        Each preserved spill filename embeds the full batch digest; this parses
        them and requires the set of files to correspond *exactly* (contiguous
        indices ``0..N-1`` and matching digests) to ``checkpoint.batch_hashes``.
        A stale or corrupt spill (from a crashed or different prior run) fails
        so resume restarts from scratch instead of reusing wrong data.
        """

        files = self._spill_files()
        if len(files) != len(checkpoint.batch_hashes):
            return False
        by_index: dict[int, str] = {}
        for path in files:
            match = _SPILL_FILE_RE.match(path.name)
            if match is None:
                return False
            try:
                idx = int(match.group(1))
            except (TypeError, ValueError):
                return False
            digest = match.group(2)
            if idx in by_index:
                return False
            by_index[idx] = digest
        for pos, expected in enumerate(checkpoint.batch_hashes):
            if by_index.get(pos) != expected:
                return False
        return True

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
        # Deadline watchdog: if a single ``next(iterator)`` blocks past the
        # deadline (not only the between-batch check below), the watchdog flags
        # the timeout and calls :meth:`SourceScanSession.cancel` so the blocked
        # read is interrupted by closing the reader — consistent with the
        # session's own cancellation/cleanup path.  ``finished`` stops the
        # watchdog once streaming ends.
        timed_out = threading.Event()
        finished = threading.Event()
        watchdog = threading.Thread(
            target=self._watchdog,
            args=(deadline, timed_out, finished),
            daemon=True,
        )
        watchdog.start()
        try:
            while True:
                if (
                    self._cancel_event is not None
                    and self._cancel_event.is_set()
                ):
                    outcome, reason = (
                        ProfileOutcome.UNAVAILABLE,
                        ProfileUnavailableReason.CANCELLED,
                    )
                    break
                if timed_out.is_set() or time.monotonic() >= deadline:
                    outcome, reason = (
                        ProfileOutcome.UNAVAILABLE,
                        ProfileUnavailableReason.TIMEOUT,
                    )
                    break
                try:
                    batch = next(iterator)
                except StopIteration:
                    # A watchdog cancel closes the reader, ending the stream
                    # without raising; treat that as the flagged timeout.
                    if timed_out.is_set():
                        outcome, reason = (
                            ProfileOutcome.UNAVAILABLE,
                            ProfileUnavailableReason.TIMEOUT,
                        )
                    break
                except Exception:  # noqa: BLE001 - any scan failure is unavailable
                    if timed_out.is_set():
                        outcome, reason = (
                            ProfileOutcome.UNAVAILABLE,
                            ProfileUnavailableReason.TIMEOUT,
                        )
                    else:
                        outcome, reason = (
                            ProfileOutcome.UNAVAILABLE,
                            ProfileUnavailableReason.ITERATOR_FAILURE,
                        )
                    break
                digest = _hash_batch(batch)
                if checkpoint is not None and pos < len(checkpoint.batch_hashes):
                    # Exact checkpoint batch-hash match is required for every
                    # prefix batch: a changed prefix means the (allegedly
                    # reopenable) snapshot is inconsistent.  Never reuse the
                    # preserved spill or combine it with the new stream.
                    if digest != checkpoint.batch_hashes[pos]:
                        outcome, reason = (
                            ProfileOutcome.UNAVAILABLE,
                            ProfileUnavailableReason.CHECKPOINT_MISMATCH,
                        )
                        break
                    # Reuse the prior partial run's spill file; do not rewrite.
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
        finally:
            finished.set()
            watchdog.join(timeout=_WATCHDOG_JOIN_SECONDS)
        return outcome, reason, _StreamResult(tuple(hashes), iterator)

    def _watchdog(
        self,
        deadline: float,
        timed_out: threading.Event,
        finished: threading.Event,
    ) -> None:
        """Cancel the scan when the deadline expires.

        Blocks on ``finished.wait(remaining)`` so a normal completion wakes it
        immediately; when the deadline passes first it flags ``timed_out`` and
        calls :meth:`SourceScanSession.cancel`, closing the reader so a blocked
        ``read_next_batch`` is interrupted rather than waited out.
        """

        while not finished.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out.set()
                self._session.cancel()
                return
            if finished.wait(timeout=remaining):
                return

    def _drain(self, iterator: Iterator[pa.RecordBatch]) -> None:
        """Exhaust a (likely cancelled) iterator so its cleanup finalizes."""

        try:
            for _ in iterator:
                pass
        except Exception:  # noqa: BLE001 - best-effort cleanup
            return

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
        path = self._spill_root / f"batch-{index:06d}-{digest}.arrow"
        with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(
            sink, batch.schema
        ) as writer:
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


# =========================================================================== #
# Task 11: sample-policy schema, conservative classification, named          #
# activation, deterministic redacted samples, and model-context export.       #
# =========================================================================== #
#
# This section adds the value-exposure surface on top of the Task 10 exact
# aggregate profile.  A conservative local classifier inspects field name/type
# patterns and emits suggested classifications *without ever returning raw
# values*.  The named approver activates an exact policy fingerprint; the
# activation binds the normalized approver, policy hash, profile hash, and
# snapshot ids, and any change stales it transitively through the session
# dependency index.  Deterministic, bounded samples are selected by the N
# smallest session-salted hashes of source id plus declared grain values, then
# transformed (omit/redact/hash/bucket/reveal) under hard publication caps and
# a final canary/hard-deny scan.
#
# Secrecy invariants:
# * The classifier returns labels and reasons, never matching values.
# * Credential and private-key classifications are non-overridable hard deny;
#   they can never be revealed.
# * Samples default to ``omit``; only explicit approval exposes any value.
# * Hard publication caps (rows/fields/bytes/values) are enforced before any
#   context export.
# * A final canary scan rejects any output containing a hard-deny pattern.

#: Maximum selected sample rows per source.
MAX_SAMPLE_ROWS_PER_SOURCE: int = 20

#: Maximum exposed (non-omit) fields per source.
MAX_SAMPLE_FIELDS_PER_SOURCE: int = 50

#: Maximum sample output bytes per source.
MAX_SAMPLE_BYTES_PER_SOURCE: int = 64 * 1024

#: Maximum sample output bytes per session (across all sources).
MAX_SAMPLE_BYTES_PER_SESSION: int = 256 * 1024

#: Maximum revealed distinct values per approved low-cardinality field.
MAX_REVEALED_DISTINCT_VALUES: int = 100

# Stable policy/export diagnostic codes (rendered; never leak raw causes).
CODE_PROFILE_INVALID = "discovery.profile.policy_invalid"
CODE_PROFILE_ACTOR = "discovery.profile.actor_mismatch"
CODE_PROFILE_STALE = "discovery.profile.policy_stale"
CODE_PROFILE_CANARY = "discovery.profile.canary_leak"
CODE_PROFILE_CAP = "discovery.profile.cap_exceeded"
CODE_PROFILE_GRAIN = "discovery.profile.grain_required"
CODE_PROFILE_UNAVAILABLE = "discovery.profile.not_available"

# Conservative field-classification label constants (rendered in suggestions;
# never values).
_LABEL_CREDENTIAL = "credential"
_LABEL_PRIVATE_KEY = "private_key"
_LABEL_IDENTIFIER = "identifier"
_LABEL_PERSONAL = "personal"
_LABEL_EMAIL = "email"
_LABEL_FREE_TEXT = "free_text"
_LABEL_NUMERIC = "numeric"
_LABEL_TEMPORAL = "temporal"

# Hard-deny patterns that must NEVER appear in exported context, regardless of
# policy (credentials and private keys are non-overridable).  These are matched
# against the serialized redacted payload as a final defense.
_HARD_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN(?: |\r?\n)[A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

_FIELD_NAME_RE: re.Pattern[str] = re.compile(r"[a-z0-9]+")

#: Stable identifier shape for safe policy ids (mirrors the session node id).
_POLICY_SAFE_ID_RE: re.Pattern[str] = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")

#: 64-character lowercase hex digest shape.
_HEX64_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")


def _as_int(value: object, default: int) -> int:
    """Return ``value`` as int when possible, else ``default``."""

    if type(value) is int:
        return value
    if type(value) is bool:
        return default
    return default

# Credential/private-key name fragments (matched against the lowercased,
# tokenized field name).
_PRIVATE_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {"privatekey", "private_key", "privkey", "priv_key", "private", "sshkey"}
)
_CREDENTIAL_FRAGMENTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "credential",
        "apikey",
        "api_key",
        "accesskey",
        "access_key",
        "accesstoken",
        "access_token",
        "clientsecret",
        "client_secret",
    }
)
_IDENTIFIER_FRAGMENTS: frozenset[str] = frozenset(
    {"ssn", "social", "serial", "serialno", "serial_number", "vin", "iban"}
)
_PERSONAL_FRAGMENTS: frozenset[str] = frozenset(
    {
        "firstname",
        "first_name",
        "lastname",
        "last_name",
        "fullname",
        "full_name",
        "username",
        "user",
        "customer",
        "person",
    }
)
_EMAIL_FRAGMENTS: frozenset[str] = frozenset({"email", "e-mail", "mail"})
_NUMERIC_TYPES: frozenset[str] = _NUMERIC_SCALARS | {"decimal"}
_TEMPORAL_TYPES: frozenset[str] = _DATE_SCALARS | {"timestamp"}


class FieldTransform(StrEnum):
    """Per-field value transformation in a sample policy.

    The policy defaults every field to :attr:`OMIT`; only explicit named
    approval exposes any value-derived token.
    """

    OMIT = "omit"
    REDACT = "redact"
    HASH = "hash"
    BUCKET = "bucket"
    REVEAL = "reveal"


class ProfilePolicyError(Exception):
    """Sanitized policy/export diagnostic exception.

    Only a stable ``code``, an optional constant ``safe_detail``, and validated
    ``safe_ids`` are ever rendered.  Raw causes are never chained or surfaced
    (``from None`` at every raise site).  Values, canary seeds, and profile
    internals are never echoed.
    """

    def __init__(
        self,
        code: str,
        *,
        safe_detail: str | None = None,
        safe_ids: Iterable[str] = (),
    ) -> None:
        self.code = code
        self.safe_detail = safe_detail
        validated: list[str] = []
        for item in safe_ids:
            if (
                type(item) is str
                and len(item) <= 128
                and _POLICY_SAFE_ID_RE.match(item) is not None
            ):
                validated.append(item)
            if len(validated) >= 16:
                break
        self.safe_ids: tuple[str, ...] = tuple(validated)
        super().__init__(self._render())

    def _render(self) -> str:
        parts: list[str] = [self.code]
        if self.safe_detail is not None:
            parts.append(self.safe_detail)
        if self.safe_ids:
            parts.append("ids=" + ",".join(self.safe_ids))
        return " ".join(parts)

    def __str__(self) -> str:
        return self._render()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"safe_detail={self.safe_detail!r}, safe_ids={self.safe_ids!r})"
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping containing only safe fields."""

        result: dict[str, object] = {"code": self.code}
        if self.safe_detail is not None:
            result["safe_detail"] = self.safe_detail
        if self.safe_ids:
            result["safe_ids"] = list(self.safe_ids)
        return result


class ContextExportError(ProfilePolicyError):
    """Raised when a context export fails the final canary/hard-deny scan."""


# --------------------------------------------------------------------------- #
# Helpers for profile/policy round-tripping                                   #
# --------------------------------------------------------------------------- #


def _opt_int(value: object) -> int | None:
    return value if type(value) is int else None


def _opt_str(value: object) -> str | None:
    return value if type(value) is str else None


def _column_lookup(
    profile: SourceProfile,
) -> dict[str, ColumnProfile]:
    return {col.name: col for col in profile.columns}


# --------------------------------------------------------------------------- #
# Conservative local classification                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FieldClassification:
    """Conservative classifier output for one field.

    ``labels`` are safe classification constants; ``hard_denied`` marks a
    non-overridable credential/private-key field that can never be revealed.
    No matching value is ever returned.
    """

    name: str
    labels: tuple[str, ...]
    hard_denied: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "labels": list(self.labels),
            "hard_denied": self.hard_denied,
            "reason": self.reason,
        }


def _tokenize_name(name: str) -> list[str]:
    """Return the lowercased alphanumeric tokens of a field name."""

    return _FIELD_NAME_RE.findall(name.lower())


def classify_field(name: str, type_name: str) -> FieldClassification:
    """Return a conservative classification for a field.

    Classification uses field name and logical type patterns only — never raw
    values.  Credential and private-key classifications are non-overridable
    hard deny.  The returned labels and reason are safe constants; no matching
    value is ever returned.
    """

    tokens = set(_tokenize_name(name))
    labels: list[str] = []
    reasons: list[str] = []
    hard_denied = False

    private_hits = tokens & _PRIVATE_KEY_FRAGMENTS
    if private_hits or any(
        frag in name.lower() for frag in _PRIVATE_KEY_FRAGMENTS
    ):
        labels.append(_LABEL_PRIVATE_KEY)
        reasons.append(_LABEL_PRIVATE_KEY)
        hard_denied = True

    cred_hits = tokens & _CREDENTIAL_FRAGMENTS
    if cred_hits or any(
        frag in name.lower() for frag in _CREDENTIAL_FRAGMENTS
    ):
        if _LABEL_CREDENTIAL not in labels:
            labels.append(_LABEL_CREDENTIAL)
            reasons.append(_LABEL_CREDENTIAL)
        hard_denied = True

    ident_hits = tokens & _IDENTIFIER_FRAGMENTS
    if ident_hits:
        labels.append(_LABEL_IDENTIFIER)
        reasons.append(_LABEL_IDENTIFIER)

    personal_hits = tokens & _PERSONAL_FRAGMENTS
    if personal_hits or any(
        frag in name.lower() for frag in _PERSONAL_FRAGMENTS
    ):
        labels.append(_LABEL_PERSONAL)
        reasons.append(_LABEL_PERSONAL)

    email_hits = tokens & _EMAIL_FRAGMENTS
    if email_hits or any(frag in name.lower() for frag in _EMAIL_FRAGMENTS):
        labels.append(_LABEL_EMAIL)
        reasons.append(_LABEL_EMAIL)

    low_type = type_name.lower()
    if low_type in _NUMERIC_TYPES:
        labels.append(_LABEL_NUMERIC)
        reasons.append(_LABEL_NUMERIC)
    elif low_type in _TEMPORAL_TYPES:
        labels.append(_LABEL_TEMPORAL)
        reasons.append(_LABEL_TEMPORAL)
    else:
        labels.append(_LABEL_FREE_TEXT)
        reasons.append(_LABEL_FREE_TEXT)

    if not reasons:
        reasons.append(_LABEL_FREE_TEXT)
    return FieldClassification(
        name=name,
        labels=tuple(dict.fromkeys(labels)),
        hard_denied=hard_denied,
        reason=",".join(dict.fromkeys(reasons)),
    )


# --------------------------------------------------------------------------- #
# Policy schema                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SampleCaps:
    """Publication caps.  Project policy may reduce but not increase these."""

    rows: int = MAX_SAMPLE_ROWS_PER_SOURCE
    fields: int = MAX_SAMPLE_FIELDS_PER_SOURCE
    bytes_per_source: int = MAX_SAMPLE_BYTES_PER_SOURCE
    bytes_per_session: int = MAX_SAMPLE_BYTES_PER_SESSION
    revealed_values: int = MAX_REVEALED_DISTINCT_VALUES

    def __post_init__(self) -> None:
        if not all(
            isinstance(v, int) and v > 0
            for v in (
                self.rows,
                self.fields,
                self.bytes_per_source,
                self.bytes_per_session,
                self.revealed_values,
            )
        ):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="invalid caps")
        if self.rows > MAX_SAMPLE_ROWS_PER_SOURCE:
            raise ProfilePolicyError(CODE_PROFILE_CAP, safe_detail="rows")
        if self.fields > MAX_SAMPLE_FIELDS_PER_SOURCE:
            raise ProfilePolicyError(CODE_PROFILE_CAP, safe_detail="fields")
        if self.bytes_per_source > MAX_SAMPLE_BYTES_PER_SOURCE:
            raise ProfilePolicyError(CODE_PROFILE_CAP, safe_detail="bytes_per_source")
        if self.bytes_per_session > MAX_SAMPLE_BYTES_PER_SESSION:
            raise ProfilePolicyError(CODE_PROFILE_CAP, safe_detail="bytes_per_session")
        if self.revealed_values > MAX_REVEALED_DISTINCT_VALUES:
            raise ProfilePolicyError(CODE_PROFILE_CAP, safe_detail="revealed_values")

    def to_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "fields": self.fields,
            "bytes_per_source": self.bytes_per_source,
            "bytes_per_session": self.bytes_per_session,
            "revealed_values": self.revealed_values,
        }

    @classmethod
    def from_dict(cls, data: object) -> SampleCaps:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="caps")
        return cls(
            rows=_as_int(data.get("rows"), MAX_SAMPLE_ROWS_PER_SOURCE),
            fields=_as_int(data.get("fields"), MAX_SAMPLE_FIELDS_PER_SOURCE),
            bytes_per_source=_as_int(
                data.get("bytes_per_source"), MAX_SAMPLE_BYTES_PER_SOURCE
            ),
            bytes_per_session=_as_int(
                data.get("bytes_per_session"), MAX_SAMPLE_BYTES_PER_SESSION
            ),
            revealed_values=_as_int(
                data.get("revealed_values"), MAX_REVEALED_DISTINCT_VALUES
            ),
        )


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """Per-field transformation decision.

    ``hard_denied`` marks a non-overridable credential/private-key field that
    can never use :attr:`FieldTransform.REVEAL`.
    """

    name: str
    transform: FieldTransform
    hard_denied: bool = False
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="field name")
        if not isinstance(self.transform, FieldTransform):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="transform")
        if type(self.hard_denied) is not bool:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="hard_denied")
        if (
            not isinstance(self.labels, tuple)
            or not all(type(item) is str for item in self.labels)
        ):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="labels")
        if self.hard_denied and self.transform is FieldTransform.REVEAL:
            raise ProfilePolicyError(
                CODE_PROFILE_INVALID,
                safe_detail="hard-denied field cannot reveal",
                safe_ids=(self.name,),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "transform": self.transform.value,
            "hard_denied": self.hard_denied,
            "labels": list(self.labels),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FieldPolicy:
        raw_transform = data.get("transform")
        try:
            transform = (
                FieldTransform(str(raw_transform))
                if raw_transform is not None
                else FieldTransform.OMIT
            )
        except ValueError:
            raise ProfilePolicyError(
                CODE_PROFILE_INVALID, safe_detail="transform"
            ) from None
        raw_labels = data.get("labels", ())
        if isinstance(raw_labels, str) or not isinstance(raw_labels, Sequence):
            labels: tuple[str, ...] = ()
        else:
            labels = tuple(str(item) for item in raw_labels)
        return cls(
            name=str(data["name"]),
            transform=transform,
            hard_denied=bool(data.get("hard_denied", False)),
            labels=labels,
        )


@dataclass(frozen=True, slots=True)
class SamplePolicy:
    """Named-approver sample-exposure policy for one source.

    Every field defaults to :attr:`FieldTransform.OMIT`.  The canonical
    :attr:`fingerprint` binds the schema version, source id, declared grain,
    salt id, caps, and per-field transformations (including the hard-denied
    flag) so any edit invalidates the activation and every derived artifact.
    The session salt itself is never stored in the policy.
    """

    schema_version: int = SCHEMA_VERSION
    source_id: str = ""
    grain: tuple[str, ...] = ()
    salt_id: str = ""
    caps: SampleCaps = SampleCaps()
    fields: tuple[FieldPolicy, ...] = ()

    def __post_init__(self) -> None:
        from selayer_discovery.model import SCHEMA_VERSION as _SCHEMA_VERSION

        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="schema version")
        if type(self.source_id) is not str or not self.source_id:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="source id")
        if (
            not isinstance(self.grain, tuple)
            or not all(type(item) is str for item in self.grain)
        ):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="grain")
        if type(self.salt_id) is not str or _HEX64_RE.match(self.salt_id) is None:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="salt id")
        if not isinstance(self.fields, tuple) or not all(
            isinstance(item, FieldPolicy) for item in self.fields
        ):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="fields")
        names = [field.name for field in self.fields]
        if len(set(names)) != len(names):
            raise ProfilePolicyError(
                CODE_PROFILE_INVALID, safe_detail="duplicate field"
            )

    @property
    def fingerprint(self) -> str:
        """Canonical SHA-256 fingerprint of the policy (no salt or values)."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "grain": list(self.grain),
            "salt_id": self.salt_id,
            "caps": self.caps.to_dict(),
            "fields": [field.to_dict() for field in self.fields],
        }
        return _fingerprint(payload)

    def field_map(self) -> dict[str, FieldPolicy]:
        return {field.name: field for field in self.fields}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "grain": list(self.grain),
            "salt_id": self.salt_id,
            "caps": self.caps.to_dict(),
            "fields": [field.to_dict() for field in self.fields],
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SamplePolicy:
        raw_fields = data.get("fields", ())
        if isinstance(raw_fields, str) or not isinstance(raw_fields, Sequence):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="fields")
        fields = tuple(
            FieldPolicy.from_dict(item)  # type: ignore[arg-type]
            for item in raw_fields
            if isinstance(item, Mapping)
        )
        raw_grain = data.get("grain", ())
        if isinstance(raw_grain, str) or not isinstance(raw_grain, Sequence):
            grain: tuple[str, ...] = ()
        else:
            grain = tuple(str(item) for item in raw_grain)
        return cls(
            schema_version=_as_int(data.get("schema_version"), SCHEMA_VERSION),
            source_id=str(data["source_id"]),
            grain=grain,
            salt_id=str(data["salt_id"]),
            caps=SampleCaps.from_dict(data.get("caps")),
            fields=fields,
        )


def propose_policy(
    profile: SourceProfile,
    grain: Iterable[str],
    *,
    salt_id: str,
) -> tuple[SamplePolicy, tuple[FieldClassification, ...]]:
    """Return a conservative proposed policy plus per-field classifications.

    The proposal defaults every field to :attr:`FieldTransform.OMIT` and marks
    credential/private-key fields as non-overridable hard deny.  No raw value
    is ever inspected or returned.  Requires a completed (available) profile.
    """

    if not profile.is_available:
        raise ProfilePolicyError(
            CODE_PROFILE_UNAVAILABLE, safe_ids=(profile.source_id,)
        ) from None
    known = {col.name for col in profile.columns}
    safe_grain = tuple(col for col in grain if col in known)
    fields: list[FieldPolicy] = []
    classifications: list[FieldClassification] = []
    for col in profile.columns:
        classification = classify_field(col.name, col.type_name)
        classifications.append(classification)
        fields.append(
            FieldPolicy(
                name=col.name,
                transform=FieldTransform.OMIT,
                hard_denied=classification.hard_denied,
                labels=classification.labels,
            )
        )
    policy = SamplePolicy(
        source_id=profile.source_id,
        grain=safe_grain,
        salt_id=salt_id,
        fields=tuple(fields),
    )
    return policy, tuple(classifications)


# --------------------------------------------------------------------------- #
# Named activation                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PolicyActivation:
    """Named-approver activation binding the policy to its exact inputs.

    The activation binds the normalized approver, policy fingerprint, profile
    fingerprint, snapshot id, schema fingerprint, and declared grain.  Any
    change to a bound input stales the activation and every downstream
    artifact through the session dependency index.  ``activated_at`` is a
    calendar date recorded for audit but excluded from the binding
    :attr:`fingerprint`.
    """

    schema_version: int = SCHEMA_VERSION
    session_id: str = ""
    source_id: str = ""
    approver: str = ""
    policy_fingerprint: str = ""
    profile_fingerprint: str = ""
    schema_fingerprint: str = ""
    snapshot_id: str | None = None
    grain: tuple[str, ...] = ()
    activated_at: str = ""

    def __post_init__(self) -> None:
        from selayer_discovery.model import SCHEMA_VERSION as _SCHEMA_VERSION

        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="schema version")
        for field in ("session_id", "source_id", "approver"):
            if type(getattr(self, field)) is not str or not getattr(self, field):
                raise ProfilePolicyError(
                    CODE_PROFILE_INVALID, safe_detail=field.replace("_", " ")
                )
        for hash_field in ("policy_fingerprint", "profile_fingerprint", "schema_fingerprint"):
            value = getattr(self, hash_field)
            if type(value) is not str or _HEX64_RE.match(value) is None:
                raise ProfilePolicyError(
                    CODE_PROFILE_INVALID, safe_detail=hash_field.replace("_", " ")
                )
        if (
            not isinstance(self.grain, tuple)
            or not all(type(item) is str for item in self.grain)
        ):
            raise ProfilePolicyError(CODE_PROFILE_INVALID, safe_detail="grain")

    @property
    def fingerprint(self) -> str:
        """Canonical binding fingerprint (excludes the audit date)."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "approver": self.approver,
            "policy_fingerprint": self.policy_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "snapshot_id": self.snapshot_id,
            "grain": list(self.grain),
        }
        return _fingerprint(payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "approver": self.approver,
            "policy_fingerprint": self.policy_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "snapshot_id": self.snapshot_id,
            "grain": list(self.grain),
            "activated_at": self.activated_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PolicyActivation:
        raw_grain = data.get("grain", ())
        if isinstance(raw_grain, str) or not isinstance(raw_grain, Sequence):
            grain: tuple[str, ...] = ()
        else:
            grain = tuple(str(item) for item in raw_grain)
        snapshot = data.get("snapshot_id")
        return cls(
            schema_version=_as_int(data.get("schema_version"), SCHEMA_VERSION),
            session_id=str(data["session_id"]),
            source_id=str(data["source_id"]),
            approver=str(data["approver"]),
            policy_fingerprint=str(data["policy_fingerprint"]),
            profile_fingerprint=str(data["profile_fingerprint"]),
            schema_fingerprint=str(data["schema_fingerprint"]),
            snapshot_id=snapshot if type(snapshot) is str else None,
            grain=grain,
            activated_at=str(data.get("activated_at", "")),
        )


def activate_policy(
    policy: SamplePolicy,
    profile: SourceProfile,
    *,
    session_id: str,
    approver: str,
    activated_at: str,
) -> PolicyActivation:
    """Validate a policy against its profile and build a named activation.

    Enforces:

    * the profile is completed;
    * every policy field is a known profile column (unknown fields rejected);
    * a hard-denied field can never reveal;
    * ``reveal`` requires a low-cardinality field (distinct count within cap);
    * ``bucket`` requires a ranged (numeric/temporal) field;
    * the exposed-field cap.

    Returns a :class:`PolicyActivation` binding the normalized approver, policy
    fingerprint, profile fingerprint, snapshot id, schema fingerprint, and
    grain.  The approver must already be normalized by the caller.
    """

    if not profile.is_available:
        raise ProfilePolicyError(
            CODE_PROFILE_UNAVAILABLE, safe_ids=(profile.source_id,)
        ) from None
    columns = _column_lookup(profile)
    policy_map = policy.field_map()
    # Unknown fields (not in the profile) are rejected.
    for name in policy_map:
        if name not in columns:
            raise ProfilePolicyError(
                CODE_PROFILE_INVALID,
                safe_detail="unknown field",
                safe_ids=(name,),
            ) from None
    exposed = 0
    for field in policy.fields:
        if field.transform is not FieldTransform.OMIT:
            exposed += 1
        if field.transform is FieldTransform.REVEAL:
            col = columns[field.name]
            # Hard-deny is *derived* from the profile field name/type, never
            # trusted from the policy payload: an untrusted policy that claims
            # ``hard_denied=False`` on a credential/private-key field cannot
            # reveal it. The diagnostic carries no raw value.
            if classify_field(col.name, col.type_name).hard_denied:
                raise ProfilePolicyError(
                    CODE_PROFILE_INVALID,
                    safe_detail="hard-denied field cannot reveal",
                    safe_ids=(field.name,),
                ) from None
            distinct = col.distinct_count
            if distinct is None or distinct > policy.caps.revealed_values:
                raise ProfilePolicyError(
                    CODE_PROFILE_INVALID,
                    safe_detail="reveal requires low cardinality",
                    safe_ids=(field.name,),
                ) from None
        if field.transform is FieldTransform.BUCKET:
            col = columns[field.name]
            if not col.has_range:
                raise ProfilePolicyError(
                    CODE_PROFILE_INVALID,
                    safe_detail="bucket requires a ranged field",
                    safe_ids=(field.name,),
                ) from None
    if exposed > policy.caps.fields:
        raise ProfilePolicyError(
            CODE_PROFILE_CAP, safe_detail="exposed fields"
        ) from None
    return PolicyActivation(
        session_id=session_id,
        source_id=profile.source_id,
        approver=approver,
        policy_fingerprint=policy.fingerprint,
        profile_fingerprint=profile.fingerprint,
        schema_fingerprint=profile.schema_fingerprint,
        snapshot_id=profile.snapshot_id,
        grain=policy.grain,
        activated_at=activated_at,
    )


def verify_activation(
    activation: PolicyActivation,
    policy: SamplePolicy,
    profile: SourceProfile,
    *,
    session_id: str,
    source_id: str,
    approver: str,
) -> None:
    """Verify an activation binds the *current* policy/profile/session inputs.

    Every bound field is checked against the current values; any mismatch
    fails closed with :data:`CODE_PROFILE_STALE` and a constant (value-free)
    ``safe_detail`` naming the stale binding.  Used by the export command to
    reject a context export whose policy, profile, schema, snapshot, grain,
    session/source ids, or normalized approver no longer match the persisted
    activation artifact.
    """

    checks: tuple[tuple[bool, str], ...] = (
        (activation.session_id == session_id, "session"),
        (activation.source_id == source_id, "source"),
        (activation.approver == approver, "approver"),
        (activation.policy_fingerprint == policy.fingerprint, "policy"),
        (activation.profile_fingerprint == profile.fingerprint, "profile"),
        (activation.schema_fingerprint == profile.schema_fingerprint, "schema"),
        (activation.snapshot_id == profile.snapshot_id, "snapshot"),
        (activation.grain == policy.grain, "grain"),
    )
    for ok, detail in checks:
        if not ok:
            raise ProfilePolicyError(
                CODE_PROFILE_STALE, safe_detail=detail
            ) from None


# --------------------------------------------------------------------------- #
# Deterministic sample selection and transformation                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _MaxKey:
    """Invert comparison so a min-heap evicts the *largest* hash.

    Keeps the N smallest salted row hashes in bounded memory.
    """

    value: str

    def __lt__(self, other: _MaxKey) -> bool:
        return self.value > other.value


def _canonical_value_str(value: object) -> str:
    """Return a stable string for any Arrow-derived Python value.

    Used only to build a salted hash key; the value itself never appears in
    output under a non-reveal transform.
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _grain_key(source_id: str, grain_values: Sequence[object], salt: bytes) -> str:
    """Return the salted SHA-256 hex key for one row's grain tuple."""

    parts = [source_id]
    parts.extend(_canonical_value_str(v) for v in grain_values)
    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(salt + b"\x1f" + material).hexdigest()


def _row_grain_values(
    row: Mapping[str, object], grain: tuple[str, ...]
) -> tuple[object, ...]:
    return tuple(row.get(name) for name in grain)


def select_sample_rows(
    session: SourceScanSession,
    policy: SamplePolicy,
    salt: bytes,
) -> list[dict[str, object]]:
    """Return the deterministic bounded sample rows for one source.

    Rows are ordered by the N smallest session-salted hashes of the source id
    plus the declared grain values (deterministic for a fixed salt and
    snapshot).  A valid declared grain is required: a source without one
    cannot emit samples.  Raw values are returned untransformed; the caller
    applies the activated transforms and publication caps.

    Args:
        session: an open :class:`~selayer.sources.scan.SourceScanSession`.
        policy: the activated sample policy (carries the grain and row cap).
        salt: the session salt bytes (never returned or rendered).

    Raises:
        ProfilePolicyError: ``discovery.profile.grain_required`` when no valid
            grain column is declared.
    """

    known = {field.name for field in session.schema.fields}
    grain = tuple(col for col in policy.grain if col in known)
    if not grain:
        raise ProfilePolicyError(
            CODE_PROFILE_GRAIN, safe_ids=(session.source_id,)
        ) from None
    max_rows = min(policy.caps.rows, MAX_SAMPLE_ROWS_PER_SOURCE)
    heap: list[tuple[_MaxKey, int, dict[str, object]]] = []
    seq = 0
    for batch in session.iter_batches():
        for raw_row in batch.to_pylist():
            row = {k: v for k, v in raw_row.items() if k in known}  # type: ignore[union-attr]
            key = _grain_key(
                session.source_id, _row_grain_values(row, grain), salt
            )
            heapq.heappush(heap, (_MaxKey(key), seq, row))
            if len(heap) > max_rows:
                heapq.heappop(heap)
            seq += 1
    kept = sorted(heap, key=lambda item: item[0].value)
    return [row for _, _, row in kept]


def _bucket_label(value: object, col: ColumnProfile) -> str:
    """Return a coarse, value-safe bucket label for a ranged value.

    Maps the value into one of four equal-width buckets across the field's
    observed [min, max] range.  The exact value never appears in the label.
    """

    if value is None:
        return "null"
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return "bucketed"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "bucketed"
    min_raw = _parse_range_endpoint(col.min_value)
    max_raw = _parse_range_endpoint(col.max_value)
    if min_raw is None or max_raw is None:
        return "bucketed"
    span = max_raw - min_raw
    if span <= 0:
        return "b0"
    ratio = (numeric - min_raw) / span
    if ratio <= 0.25:
        return "b0"
    if ratio <= 0.5:
        return "b1"
    if ratio <= 0.75:
        return "b2"
    return "b3"


def _parse_range_endpoint(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Date/timestamp endpoints: derive an ordinal proxy from the string so
        # temporal values still bucket deterministically without leaking.
        try:
            return float(sum(ord(ch) for ch in raw))
        except (TypeError, ValueError):
            return None


def _transform_value(
    value: object,
    transform: FieldTransform,
    field_name: str,
    salt: bytes,
    col: ColumnProfile,
) -> tuple[bool, object]:
    """Apply one transform.  Returns ``(included, value)``.

    ``included`` is ``False`` for :attr:`FieldTransform.OMIT` (the field is
    dropped); otherwise the transformed value is returned.  A null original
    yields a structural ``None`` token for redact/hash/bucket.
    """

    if transform is FieldTransform.OMIT:
        return False, None
    if value is None:
        return True, None
    if transform is FieldTransform.REDACT:
        return True, "non_null"
    if transform is FieldTransform.HASH:
        material = salt + b"\x1f" + field_name.encode("utf-8")
        material += b"\x1f" + _canonical_value_str(value).encode("utf-8")
        return True, hashlib.sha256(material).hexdigest()
    if transform is FieldTransform.BUCKET:
        return True, _bucket_label(value, col)
    # REVEAL
    return True, value


def hard_deny_scan(payload: bytes) -> str | None:
    """Scan serialized payload bytes for hard-deny patterns.

    Returns the matched pattern name (a safe constant) or ``None``.  Used as a
    final defense before publishing model context: a credential or private key
    must never appear, regardless of policy.
    """

    for index, pattern in enumerate(_HARD_DENY_PATTERNS):
        if pattern.search(payload.decode("utf-8", errors="ignore")) is not None:
            return f"hard_deny_{index}"
    return None


def _reveal_value_matches_hard_deny(value: object) -> str | None:
    """Return a constant pattern label if a revealed raw value is sensitive.

    Conservative per-value shape check applied to ``reveal`` outputs only: a
    raw value that matches a credential/private-key/token shape under an
    innocuous field name must never be published, even when the field name is
    not itself classified as hard-denied.  Returns ``None`` when the value is
    safe to reveal.  The matched label is a constant; no raw value is returned.
    """

    text = _canonical_value_str(value)
    if not text:
        return None
    for index, pattern in enumerate(_HARD_DENY_PATTERNS):
        if pattern.search(text) is not None:
            return f"hard_deny_{index}"
    return None


@dataclass(frozen=True, slots=True)
class RedactedSampleExport:
    """Model-safe redacted sample export for one source.

    :meth:`to_dict` exposes only transformed values, aggregate counts, and
    binding fingerprints — never raw source values, the salt, spill paths, or
    source locations.
    """

    schema_version: int
    session_id: str
    source_id: str
    policy_fingerprint: str
    profile_fingerprint: str
    schema_fingerprint: str
    snapshot_id: str | None
    grain: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]
    row_count: int
    field_count: int
    bytes: int
    canary_scan: str

    @property
    def fingerprint(self) -> str:
        """Canonical SHA-256 fingerprint of the value-bearing payload."""

        return _fingerprint(self.payload())

    def payload(self) -> dict[str, object]:
        """Return the value-bearing payload (rows + grain) for hashing/caps."""

        return {
            "rows": [dict(row) for row in self.rows],
            "grain": list(self.grain),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "policy_fingerprint": self.policy_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "snapshot_id": self.snapshot_id,
            "grain": list(self.grain),
            "rows": [dict(row) for row in self.rows],
            "row_count": self.row_count,
            "field_count": self.field_count,
            "bytes": self.bytes,
            "canary_scan": self.canary_scan,
            "fingerprint": self.fingerprint,
        }


def build_context_export(
    session: SourceScanSession,
    policy: SamplePolicy,
    profile: SourceProfile,
    salt: bytes,
    *,
    session_id: str,
    session_bytes_used: int = 0,
) -> RedactedSampleExport:
    """Build a bounded, transformed, canary-scanned context export.

    Selects the deterministic sample rows, applies the activated transforms,
    enforces the per-source and session byte caps, and runs a final
    canary/hard-deny scan.  Raises :class:`ContextExportError` if any hard-deny
    pattern escapes or a cap is exceeded after row trimming.
    """

    if not profile.is_available:
        raise ProfilePolicyError(
            CODE_PROFILE_UNAVAILABLE, safe_ids=(profile.source_id,)
        ) from None
    columns = _column_lookup(profile)
    field_map = policy.field_map()
    rows = select_sample_rows(session, policy, salt)
    # Apply transforms, dropping omitted fields.
    exposed_names = [
        field.name
        for field in policy.fields
        if field.transform is not FieldTransform.OMIT
    ]
    transformed: list[dict[str, object]] = []
    for row in rows:
        out: dict[str, object] = {}
        for name in exposed_names:
            field = field_map[name]
            col = columns[name]
            if field.transform is FieldTransform.REVEAL:
                # Hard-deny is derived from the profile field name/type, never
                # trusted from the policy payload: an untrusted policy claiming
                # ``hard_denied=False`` on a credential/private-key field
                # cannot reveal it. The diagnostic carries no raw value.
                if classify_field(col.name, col.type_name).hard_denied:
                    raise ContextExportError(
                        CODE_PROFILE_INVALID,
                        safe_detail="hard-denied field cannot reveal",
                        safe_ids=(profile.source_id,),
                    ) from None
                # Conservative per-value shape check: a raw value that matches a
                # credential/private-key/token shape under an innocuous name
                # must never be revealed, regardless of the field name.
                matched = _reveal_value_matches_hard_deny(row.get(name))
                if matched is not None:
                    raise ContextExportError(
                        CODE_PROFILE_CANARY,
                        safe_detail=matched,
                        safe_ids=(profile.source_id,),
                    ) from None
            included, value = _transform_value(
                row.get(name), field.transform, name, salt, col
            )
            if included:
                out[name] = value
        transformed.append(out)
    # Enforce per-source byte cap by trimming rows from the largest-hash end.
    payload_bytes = _payload_size(transformed, policy.grain)
    while (
        payload_bytes > policy.caps.bytes_per_source
        and len(transformed) > 1
    ):
        transformed.pop()
        payload_bytes = _payload_size(transformed, policy.grain)
    # A single remaining row that still exceeds the per-source cap cannot be
    # reduced by trimming; reject it rather than publish an over-cap row.
    if payload_bytes > policy.caps.bytes_per_source:
        raise ContextExportError(
            CODE_PROFILE_CAP,
            safe_detail="bytes_per_source",
            safe_ids=(profile.source_id,),
        ) from None
    # Enforce the session cap across multiple exports. Reject when the prior
    # usage has already exhausted the budget, and when a single row cannot fit
    # the remaining budget (mirrors the per-source single-row rule).
    if session_bytes_used >= policy.caps.bytes_per_session:
        raise ContextExportError(
            CODE_PROFILE_CAP,
            safe_detail="bytes_per_session",
            safe_ids=(profile.source_id,),
        ) from None
    budget = policy.caps.bytes_per_session - session_bytes_used
    while len(transformed) > 1 and _payload_size(transformed, policy.grain) > budget:
        transformed.pop()
    payload_bytes = _payload_size(transformed, policy.grain)
    if payload_bytes > budget:
        raise ContextExportError(
            CODE_PROFILE_CAP,
            safe_detail="bytes_per_session",
            safe_ids=(profile.source_id,),
        ) from None
    # Final canary/hard-deny scan over the value-bearing payload.
    payload_raw = _payload_bytes(transformed, policy.grain)
    matched = hard_deny_scan(payload_raw)
    if matched is not None:
        raise ContextExportError(
            CODE_PROFILE_CANARY,
            safe_detail=matched,
            safe_ids=(profile.source_id,),
        ) from None
    return RedactedSampleExport(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        source_id=profile.source_id,
        policy_fingerprint=policy.fingerprint,
        profile_fingerprint=profile.fingerprint,
        schema_fingerprint=profile.schema_fingerprint,
        snapshot_id=profile.snapshot_id,
        grain=policy.grain,
        rows=tuple(transformed),
        row_count=len(transformed),
        field_count=len(exposed_names),
        bytes=payload_bytes,
        canary_scan="passed",
    )


def _payload_size(rows: Sequence[Mapping[str, object]], grain: tuple[str, ...]) -> int:
    return len(_payload_bytes(rows, grain))


def _payload_bytes(
    rows: Sequence[Mapping[str, object]], grain: tuple[str, ...]
) -> bytes:
    import json as _json

    payload = {"rows": [dict(row) for row in rows], "grain": list(grain)}
    return _json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
