"""Exact bounded aggregate profile tests.

These tests pin the Task 10 profiling contract:

* :class:`~selayer_discovery.profiling.ProfileRunner` produces exact aggregate
  profiles from a :class:`~selayer.sources.scan.SourceScanSession` across
  multiple batches — row count, nulls, distinct counts, numeric/date/timestamp
  ranges, and grain duplicate counts — without ever exposing raw values, top
  values, example rows, spill paths, or source locations.
* Spill is bounded (typed Arrow batches written one at a time to a restrictive
  directory), aggregated exactly in DuckDB, and deleted once the aggregate
  artifact is committed.
* Consistency rules: only reopenable profiles may resume, and only with the
  same snapshot token and batch hashes; transaction and live profiles restart.
* Timeout (default 900 s/source) and cooperative cancellation interrupt the
  scan, discard partial aggregates, and record an ``unavailable`` outcome with
  a stable code. Partial iterator failures and unsupported types never produce
  aggregate claims.

The runner is driven directly against real :class:`SourceScanSession` objects
constructed from in-memory Arrow batches so the contract is pinned without the
full registry stack.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest

from selayer.sources.base import SourceConsistency
from selayer.sources.scan import SourceScanSession, SourceSnapshot
from selayer.sources.schema import schema_fingerprint, table_schema_from_arrow

from selayer_discovery.profiling import (
    DEFAULT_PROFILE_TIMEOUT_SECONDS,
    ColumnProfile,
    ProfileCheckpoint,
    ProfileMode,
    ProfileOutcome,
    ProfileRunner,
    ProfileUnavailableReason,
    SourceProfile,
)

# ---------------------------------------------------------------------------
# Schema / batch fixtures
# ---------------------------------------------------------------------------

_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("value", pa.int64()),
        ("name", pa.utf8()),
        ("ratio", pa.float64()),
        ("d", pa.date32()),
        ("ts", pa.timestamp("us")),
    ]
)


def _batch(
    *,
    id_: list[int | None],
    value: list[int | None],
    name: list[str | None],
    ratio: list[float | None],
    d: list[_dt.date | None],
    ts: list[_dt.datetime | None],
) -> pa.RecordBatch:
    return pa.RecordBatch.from_pydict(
        {"id": id_, "value": value, "name": name, "ratio": ratio, "d": d, "ts": ts},
        schema=_SCHEMA,
    )


def _two_batches() -> list[pa.RecordBatch]:
    return [
        _batch(
            id_=[1, 2],
            value=[10, 20],
            name=["a", "b"],
            ratio=[1.5, 2.5],
            d=[_dt.date(2020, 1, 1), _dt.date(2020, 1, 3)],
            ts=[
                _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc),
                _dt.datetime(2020, 1, 1, 0, 0, 2, tzinfo=_dt.timezone.utc),
            ],
        ),
        _batch(
            id_=[2, None],
            value=[20, None],
            name=["b", None],
            ratio=[2.5, None],
            d=[_dt.date(2020, 1, 3), None],
            ts=[
                _dt.datetime(2020, 1, 1, 0, 0, 2, tzinfo=_dt.timezone.utc),
                None,
            ],
        ),
    ]


def _table_schema() -> Any:
    return table_schema_from_arrow(_SCHEMA)


def _make_session(
    batches: list[pa.RecordBatch],
    *,
    source_id: str = "events",
    consistency: SourceConsistency = SourceConsistency.REOPENABLE_SNAPSHOT,
    snapshot_id: str | None = "snap-1",
    schema: Any = None,
) -> SourceScanSession:
    """Construct a real ``SourceScanSession`` directly from Arrow batches."""

    arrow_schema = batches[0].schema if batches else _SCHEMA
    table_schema = schema if schema is not None else table_schema_from_arrow(arrow_schema)
    reader = pa.RecordBatchReader.from_batches(arrow_schema, batches)
    fp = schema_fingerprint(table_schema)

    def _recheck() -> SourceSnapshot:
        return SourceSnapshot(
            consistency=consistency,
            snapshot_id=snapshot_id,
            schema_fingerprint=fp,
        )

    return SourceScanSession(
        source_id=source_id,
        schema=table_schema,
        consistency=consistency,
        snapshot_id=snapshot_id,
        reader=reader,
        release=lambda: None,
        recheck=_recheck,
    )


def _by_name(profile: SourceProfile) -> dict[str, ColumnProfile]:
    return {col.name: col for col in profile.columns}


# ---------------------------------------------------------------------------
# Step 1: exact aggregate profile across multiple batches
# ---------------------------------------------------------------------------


def test_exact_aggregate_profile_across_batches(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    runner = ProfileRunner(session, tmp_path / "spill", grain=("id",))
    profile = runner.run()

    assert profile.source_id == "events"
    assert profile.outcome is ProfileOutcome.COMPLETED
    assert profile.mode is ProfileMode.REOPENABLE
    assert profile.consistency == "reopenable_snapshot"
    assert profile.snapshot_id == "snap-1"
    assert profile.schema_fingerprint == schema_fingerprint(_table_schema())
    assert profile.unavailable_reason is None
    assert profile.is_available

    # Exact row count across both batches.
    assert profile.row_count == 4
    assert profile.batch_count == 2
    assert len(profile.batch_hashes) == 2
    # Batch hashes are stable 64-hex content digests.
    for digest in profile.batch_hashes:
        assert isinstance(digest, str) and len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    cols = _by_name(profile)
    # Numeric ranges + nulls + distinct counts.
    assert cols["id"].null_count == 1
    assert cols["id"].distinct_count == 2
    assert cols["id"].min_value == "1"
    assert cols["id"].max_value == "2"
    assert cols["id"].has_range
    assert cols["id"].type_name == "int64"

    assert cols["value"].null_count == 1
    assert cols["value"].distinct_count == 2
    assert cols["value"].min_value == "10"
    assert cols["value"].max_value == "20"

    assert cols["ratio"].null_count == 1
    assert cols["ratio"].distinct_count == 2
    assert cols["ratio"].min_value == "1.5"
    assert cols["ratio"].max_value == "2.5"
    assert cols["ratio"].type_name == "float64"

    # Date range.
    assert cols["d"].null_count == 1
    assert cols["d"].distinct_count == 2
    assert cols["d"].min_value == "2020-01-01"
    assert cols["d"].max_value == "2020-01-03"
    assert cols["d"].has_range
    assert cols["d"].type_name == "date32"

    # Timestamp range rendered as an ISO-8601 string.
    assert cols["ts"].null_count == 1
    assert cols["ts"].distinct_count == 2
    assert cols["ts"].min_value == "2020-01-01T00:00:00"
    assert cols["ts"].max_value == "2020-01-01T00:00:02"
    assert cols["ts"].type_name == "timestamp"

    # Grain duplicate count: id=2 repeats across the two batches (the NULL
    # id counts as its own distinct grain value).
    assert profile.grain_duplicate_count == 1


def test_multi_column_grain_duplicate_count(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    runner = ProfileRunner(session, tmp_path / "spill", grain=("id", "value"))
    profile = runner.run()
    assert profile.outcome is ProfileOutcome.COMPLETED
    # (id, value) has one repeated pair: (2, 20).
    assert profile.grain_duplicate_count == 1


def test_no_grain_yields_null_duplicate_count(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    runner = ProfileRunner(session, tmp_path / "spill")
    profile = runner.run()
    assert profile.grain_duplicate_count is None


def test_distinct_counts_are_exact_for_repeated_values(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    runner = ProfileRunner(session, tmp_path / "spill")
    profile = runner.run()
    cols = _by_name(profile)
    # ``name`` repeats "a" and "b" across batches; distinct stays exact.
    assert cols["name"].distinct_count == 2


def test_profile_fingerprint_is_stable(tmp_path: Path) -> None:
    session_a = _make_session(_two_batches())
    session_b = _make_session(_two_batches())
    profile_a = ProfileRunner(session_a, tmp_path / "sa").run()
    profile_b = ProfileRunner(session_b, tmp_path / "sb").run()
    assert profile_a.fingerprint == profile_b.fingerprint
    assert isinstance(profile_a.fingerprint, str) and len(profile_a.fingerprint) == 64


# ---------------------------------------------------------------------------
# Step 1: consistency mode, snapshot id, schema fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("consistency", "expected_mode"),
    [
        (SourceConsistency.REOPENABLE_SNAPSHOT, ProfileMode.REOPENABLE),
        (SourceConsistency.TRANSACTION_SNAPSHOT, ProfileMode.TRANSACTION),
        (SourceConsistency.LIVE, ProfileMode.LIVE),
    ],
)
def test_profile_records_consistency_mode(
    tmp_path: Path, consistency: SourceConsistency, expected_mode: ProfileMode
) -> None:
    session = _make_session(_two_batches(), consistency=consistency, snapshot_id="tok")
    profile = ProfileRunner(session, tmp_path / "spill").run()
    assert profile.mode is expected_mode
    assert profile.consistency == consistency.value
    assert profile.snapshot_id == "tok"


def test_live_source_has_null_snapshot_id(tmp_path: Path) -> None:
    session = _make_session(
        _two_batches(), consistency=SourceConsistency.LIVE, snapshot_id=None
    )
    profile = ProfileRunner(session, tmp_path / "spill").run()
    assert profile.snapshot_id is None
    assert profile.mode is ProfileMode.LIVE


# ---------------------------------------------------------------------------
# Step 1: unsupported type never leaks a range
# ---------------------------------------------------------------------------


def test_unsupported_type_has_no_range_but_counts_nulls(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    profile = ProfileRunner(session, tmp_path / "spill").run()
    cols = _by_name(profile)
    # utf8 columns get distinct/null counts but never a value-bearing range.
    assert not cols["name"].has_range
    assert cols["name"].min_value is None
    assert cols["name"].max_value is None
    assert cols["name"].distinct_count == 2
    assert cols["name"].null_count == 1


# ---------------------------------------------------------------------------
# Step 2: bounded spill + cleanup
# ---------------------------------------------------------------------------


def test_spill_files_deleted_after_completed_profile(tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    session = _make_session(_two_batches())
    ProfileRunner(session, spill).run()
    # After a committed aggregate the spill directory is empty.
    assert spill.exists()
    assert list(spill.iterdir()) == []


def test_spill_directory_is_restrictive(tmp_path: Path) -> None:
    import os

    spill = tmp_path / "spill"
    session = _make_session(_two_batches())
    runner = ProfileRunner(session, spill, stop_after_batches=1)
    # A partial run preserves spill so we can inspect its permissions.
    runner.run()
    assert spill.exists()
    if os.name == "posix":
        mode = spill.stat().st_mode & 0o777
        assert mode == 0o700
        for child in spill.iterdir():
            assert (child.stat().st_mode & 0o777) == 0o600


def test_no_spill_paths_or_values_in_diagnostics(tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    session = _make_session(_two_batches())
    profile = ProfileRunner(session, spill).run()
    rendered = json.dumps(profile.to_dict(), sort_keys=True)
    assert str(spill) not in rendered
    assert "spill" not in rendered
    # No raw value-bearing content beyond aggregate ranges.
    assert "SECRET" not in rendered


# ---------------------------------------------------------------------------
# Step 2: partial iterator failure never produces claims
# ---------------------------------------------------------------------------


class _FailingReader:
    """Yields one batch, then raises a raw failure echoing a secret."""

    def __init__(self, schema: pa.Schema, first: pa.RecordBatch) -> None:
        self._schema = schema
        self._first = first
        self._yielded = False

    def read_next_batch(self) -> pa.RecordBatch:
        if not self._yielded:
            self._yielded = True
            return self._first
        raise RuntimeError("injected iterator failure echoing SECRET-VALUE")

    def close(self) -> None:  # pragma: no cover - best-effort
        pass


def test_partial_iterator_failure_is_unavailable(tmp_path: Path) -> None:
    batches = _two_batches()
    schema = batches[0].schema
    table_schema = table_schema_from_arrow(schema)
    reader = _FailingReader(schema, batches[0])
    session = SourceScanSession(
        source_id="events",
        schema=table_schema,
        consistency=SourceConsistency.REOPENABLE_SNAPSHOT,
        snapshot_id="snap-1",
        reader=reader,  # type: ignore[arg-type]
        release=lambda: None,
        recheck=lambda: SourceSnapshot(
            SourceConsistency.REOPENABLE_SNAPSHOT,
            "snap-1",
            schema_fingerprint(table_schema),
        ),
    )
    profile = ProfileRunner(session, tmp_path / "spill").run()
    assert profile.outcome is ProfileOutcome.UNAVAILABLE
    assert profile.unavailable_reason is ProfileUnavailableReason.ITERATOR_FAILURE
    assert profile.row_count is None
    assert profile.columns == ()
    assert profile.grain_duplicate_count is None
    assert not profile.is_available
    # Partial spill was discarded.
    assert list((tmp_path / "spill").iterdir()) == []
    # No secret leaked through the rendered profile.
    assert "SECRET" not in json.dumps(profile.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# Step 3: consistency / resume rules
# ---------------------------------------------------------------------------


def test_reopenable_profile_can_resume_with_matching_checkpoint(
    tmp_path: Path,
) -> None:
    spill = tmp_path / "spill"
    # First (partial) run: spill one batch, preserve spill, no claims.
    session_a = _make_session(_two_batches())
    runner_a = ProfileRunner(session_a, spill, stop_after_batches=1)
    partial = runner_a.run()
    assert partial.outcome is ProfileOutcome.PARTIAL
    assert partial.row_count is None
    assert partial.columns == ()
    assert len(partial.batch_hashes) == 1
    # Spill is preserved for resume.
    assert len(list(spill.iterdir())) == 1

    # Resume with a fresh reopenable session at the same snapshot.
    session_b = _make_session(_two_batches())
    runner_b = ProfileRunner(session_b, spill)
    checkpoint = partial.checkpoint()
    assert checkpoint.snapshot_id == "snap-1"
    assert checkpoint.mode is ProfileMode.REOPENABLE
    assert len(checkpoint.batch_hashes) == 1
    resumed = runner_b.run(checkpoint=checkpoint)
    assert resumed.outcome is ProfileOutcome.COMPLETED
    assert resumed.row_count == 4
    assert len(resumed.batch_hashes) == 2
    # The resumed profile matches a fresh full profile.
    fresh = ProfileRunner(_make_session(_two_batches()), tmp_path / "fresh").run()
    assert resumed.fingerprint == fresh.fingerprint
    # Spill deleted after the committed resume.
    assert list(spill.iterdir()) == []


def test_resume_rejects_mismatched_snapshot_token(tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    session_a = _make_session(_two_batches(), snapshot_id="snap-1")
    partial = ProfileRunner(session_a, spill, stop_after_batches=1).run()
    assert partial.outcome is ProfileOutcome.PARTIAL

    # A different snapshot token invalidates the checkpoint -> fresh restart.
    session_b = _make_session(_two_batches(), snapshot_id="snap-2")
    runner_b = ProfileRunner(session_b, spill)
    checkpoint = ProfileCheckpoint(
        snapshot_id="snap-different",
        mode=ProfileMode.REOPENABLE,
        batch_hashes=partial.batch_hashes,
    )
    resumed = runner_b.run(checkpoint=checkpoint)
    assert resumed.outcome is ProfileOutcome.COMPLETED
    assert resumed.snapshot_id == "snap-2"
    assert resumed.row_count == 4


def test_transaction_profile_restarts_ignoring_checkpoint(tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    # A transaction source cannot resume; a checkpoint is ignored.
    session = _make_session(
        _two_batches(), consistency=SourceConsistency.TRANSACTION_SNAPSHOT, snapshot_id="tx"
    )
    checkpoint = ProfileCheckpoint(
        snapshot_id="tx",
        mode=ProfileMode.REOPENABLE,
        batch_hashes=("0" * 64,),
    )
    profile = ProfileRunner(session, spill).run(checkpoint=checkpoint)
    assert profile.outcome is ProfileOutcome.COMPLETED
    assert profile.mode is ProfileMode.TRANSACTION
    assert profile.row_count == 4
    assert list(spill.iterdir()) == []


# ---------------------------------------------------------------------------
# Step 4: timeout
# ---------------------------------------------------------------------------


class _SlowReader:
    """Yields batches with a configurable delay between them."""

    def __init__(self, schema: pa.Schema, batches: list[pa.RecordBatch], delay: float):
        self._it: Iterator[pa.RecordBatch] = iter(batches)
        self._delay = delay

    def read_next_batch(self) -> pa.RecordBatch:
        time.sleep(self._delay)
        return next(self._it)

    def close(self) -> None:  # pragma: no cover - best-effort
        pass


def _slow_session(
    batches: list[pa.RecordBatch], delay: float, *, snapshot_id: str = "snap-1"
) -> SourceScanSession:
    schema = batches[0].schema
    table_schema = table_schema_from_arrow(schema)
    reader = _SlowReader(schema, batches, delay)
    return SourceScanSession(
        source_id="events",
        schema=table_schema,
        consistency=SourceConsistency.REOPENABLE_SNAPSHOT,
        snapshot_id=snapshot_id,
        reader=reader,  # type: ignore[arg-type]
        release=lambda: None,
        recheck=lambda: SourceSnapshot(
            SourceConsistency.REOPENABLE_SNAPSHOT,
            snapshot_id,
            schema_fingerprint(table_schema),
        ),
    )


def _many_batches(n: int) -> list[pa.RecordBatch]:
    return [
        pa.RecordBatch.from_pydict({"id": [i, i + 1], "value": [i, i + 1]}, schema=pa.schema([("id", pa.int64()), ("value", pa.int64())]))
        for i in range(0, n * 2, 2)
    ]


def test_timeout_cancels_and_records_unavailable(tmp_path: Path) -> None:
    session = _slow_session(_many_batches(20), delay=0.03)
    runner = ProfileRunner(session, tmp_path / "spill", timeout=0.05)
    profile = runner.run()
    assert profile.outcome is ProfileOutcome.UNAVAILABLE
    assert profile.unavailable_reason is ProfileUnavailableReason.TIMEOUT
    assert profile.row_count is None
    assert profile.columns == ()
    assert profile.grain_duplicate_count is None
    # Partial spill discarded.
    assert list((tmp_path / "spill").iterdir()) == []
    assert "SECRET" not in json.dumps(profile.to_dict(), sort_keys=True)


def test_default_timeout_is_900_seconds() -> None:
    assert DEFAULT_PROFILE_TIMEOUT_SECONDS == 900


# ---------------------------------------------------------------------------
# Step 4: cancellation
# ---------------------------------------------------------------------------


class _CancelTriggerReader:
    """Yields batches; arms a cancel event after the first batch."""

    def __init__(
        self,
        schema: pa.Schema,
        batches: list[pa.RecordBatch],
        event: Any,
    ) -> None:
        self._it: Iterator[pa.RecordBatch] = iter(batches)
        self._schema = schema
        self._event = event
        self._armed = False

    def read_next_batch(self) -> pa.RecordBatch:
        batch = next(self._it)
        if not self._armed:
            self._event.set()
            self._armed = True
        return batch

    def close(self) -> None:  # pragma: no cover - best-effort
        pass


def test_cancellation_records_unavailable(tmp_path: Path) -> None:
    import threading

    batches = _many_batches(10)
    schema = batches[0].schema
    table_schema = table_schema_from_arrow(schema)
    event = threading.Event()
    reader = _CancelTriggerReader(schema, batches, event)
    session = SourceScanSession(
        source_id="events",
        schema=table_schema,
        consistency=SourceConsistency.REOPENABLE_SNAPSHOT,
        snapshot_id="snap-1",
        reader=reader,  # type: ignore[arg-type]
        release=lambda: None,
        recheck=lambda: SourceSnapshot(
            SourceConsistency.REOPENABLE_SNAPSHOT,
            "snap-1",
            schema_fingerprint(table_schema),
        ),
    )
    runner = ProfileRunner(
        session, tmp_path / "spill", timeout=30.0, cancel_event=event
    )
    profile = runner.run()
    assert profile.outcome is ProfileOutcome.UNAVAILABLE
    assert profile.unavailable_reason is ProfileUnavailableReason.CANCELLED
    assert profile.row_count is None
    assert profile.columns == ()
    assert list((tmp_path / "spill").iterdir()) == []


def test_preset_cancel_event_is_unavailable(tmp_path: Path) -> None:
    import threading

    event = threading.Event()
    event.set()
    session = _make_session(_two_batches())
    runner = ProfileRunner(
        session, tmp_path / "spill", timeout=30.0, cancel_event=event
    )
    profile = runner.run()
    assert profile.outcome is ProfileOutcome.UNAVAILABLE
    assert profile.unavailable_reason is ProfileUnavailableReason.CANCELLED


# ---------------------------------------------------------------------------
# Step 5: model-safe metadata
# ---------------------------------------------------------------------------


def test_to_dict_is_model_safe_and_deterministic(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    profile = ProfileRunner(session, tmp_path / "spill", grain=("id",)).run()
    payload = profile.to_dict()
    # Deterministic, sorted JSON round-trips.
    encoded = json.dumps(payload, sort_keys=True)
    assert json.loads(encoded) == payload
    expected_top = {
        "source_id",
        "schema_fingerprint",
        "consistency",
        "snapshot_id",
        "mode",
        "outcome",
        "unavailable_reason",
        "row_count",
        "batch_count",
        "batch_hashes",
        "grain_duplicate_count",
        "columns",
    }
    assert set(payload) == expected_top
    columns = cast(list[dict[str, object]], payload["columns"])
    for col in columns:
        assert set(col) == {
            "name",
            "type_name",
            "null_count",
            "distinct_count",
            "min_value",
            "max_value",
            "has_range",
        }
    # No source locations, spill paths, or free values leak.
    assert "location" not in payload
    assert "path" not in payload


def test_unavailable_to_dict_has_no_aggregate_claims(tmp_path: Path) -> None:
    session = _slow_session(_many_batches(20), delay=0.03)
    profile = ProfileRunner(session, tmp_path / "spill", timeout=0.05).run()
    payload = profile.to_dict()
    assert payload["outcome"] == "unavailable"
    assert payload["unavailable_reason"] == "timeout"
    assert payload["row_count"] is None
    assert payload["columns"] == []
    assert payload["grain_duplicate_count"] is None


def test_partial_to_dict_has_no_aggregate_claims(tmp_path: Path) -> None:
    session = _make_session(_two_batches())
    profile = ProfileRunner(session, tmp_path / "spill", stop_after_batches=1).run()
    payload = profile.to_dict()
    assert payload["outcome"] == "partial"
    assert payload["row_count"] is None
    assert payload["columns"] == []
    assert payload["grain_duplicate_count"] is None
    assert len(cast(list[str], payload["batch_hashes"])) == 1
