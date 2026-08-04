"""Delta adapter tests for deltalake-backed Delta Lake sources and DuckDB pushdown.

These tests exercise the real :class:`~selayer.sources.adapters.delta.DeltaAdapter`
and the registry-backed lifecycle for Delta Lake tables.  Fixtures create
deterministic Delta tables in ``tmp_path`` so every test is self-contained.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.dataset as padataset
import pyarrow.fs as pafs
import pytest
from deltalake import write_deltalake

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.adapters.delta import DeltaAdapter
from selayer.sources.base import SourceHandle
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import DeltaConfig
from selayer.sources.errors import SourceDependencyError, SourceSchemaError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema
from selayer.verification import PhysicalCheck, verify

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _events_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


_EVENTS_PA_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("value", pa.int64(), nullable=False),
    ]
)


def _events_table(rows: dict[str, list[int]]) -> pa.Table:
    """Build a non-nullable Arrow table matching the declared events schema."""

    return pa.Table.from_arrays(
        [pa.array(rows[field.name], field.type) for field in _EVENTS_PA_SCHEMA],
        schema=_EVENTS_PA_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Layer factory
# ---------------------------------------------------------------------------


@pytest.fixture
def delta_layer_factory() -> Callable[[str | Path], SemanticLayer]:
    """Return a SemanticLayer factory backed by a Delta source at *location*."""

    def factory(location: str | Path) -> SemanticLayer:
        return SemanticLayer(
            1,
            "delta_test",
            "",
            "",
            {
                "events": DataSource(
                    name="events",
                    connector=DeltaConfig(str(location)),
                    schema=_events_schema(),
                    grain=("id",),
                )
            },
            {},
            {
                "event_value": Fact.from_expression(
                    "event_value", "events", "events.value", "integer"
                )
            },
            {"total_value": Measure("total_value", "event_value", "sum")},
            {
                "total_value": Metric.from_expression(
                    "total_value", "total_value", ("total_value",)
                )
            },
            {},
        )

    return factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles() -> RuntimeProfileResolver:
    return MappingProfileResolver({})


@pytest.fixture
def providers() -> ArrowProviderResolver:
    return MappingArrowProviderResolver({})


# ---------------------------------------------------------------------------
# Step 1: reload publishes latest snapshot
# ---------------------------------------------------------------------------


def test_delta_reload_publishes_latest_snapshot(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    with QueryEngine(layer) as engine:
        first = engine.source_status("events")
        assert engine.query(["total_value"])["total_value"].item() == 10

        write_deltalake(
            location,
            _events_table({"id": [2], "value": [20]}),
            mode="append",
        )
        result = engine.reload_source("events")

        assert result.old_generation == first.generation
        assert result.snapshot != first.snapshot
        assert engine.query(["total_value"])["total_value"].item() == 30


def test_delta_reopen_pins_original_version(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = DeltaAdapter()
    baseline = adapter.prepare(source, profiles, providers)
    assert baseline.consistency.value == "reopenable_snapshot"
    assert baseline.snapshot is not None

    write_deltalake(
        location,
        _events_table({"id": [2], "value": [20]}),
        mode="append",
    )
    reopened = adapter.reopen(source, profiles, providers, baseline.snapshot)
    connection = duckdb.connect(":memory:")
    try:
        adapter.register(connection, "events", reopened)
        assert connection.execute('SELECT sum("value") FROM "events"').fetchone() == (10,)
        assert reopened.snapshot == baseline.snapshot
    finally:
        adapter.close(reopened)
        adapter.close(baseline)
        connection.close()


# ---------------------------------------------------------------------------
# Registers a pyarrow Dataset
# ---------------------------------------------------------------------------


def test_delta_registers_pyarrow_dataset(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, providers)

    # The resource wraps a DeltaTable and a PyArrow Dataset; only the Dataset
    # is registered on the connection.
    dataset = handle.resource.dataset  # type: ignore[attr-defined]
    assert isinstance(dataset, padataset.Dataset)

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)
    assert connection.execute('SELECT sum("value") FROM "events"').fetchone() == (10,)
    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# DuckDB Arrow scan pushdown (projection + filter)
# ---------------------------------------------------------------------------


def test_delta_explain_contains_arrow_scan_projection_and_filter(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1, 2, 3], "value": [5, 15, 3]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )
    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, providers)

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)

    explain = "\n".join(
        row[1]
        for row in connection.execute(
            'EXPLAIN SELECT "id" FROM "events" WHERE "value" > 10'
        ).fetchall()
    )

    assert "ARROW_SCAN" in explain
    assert "id" in explain
    assert "value" in explain
    assert connection.execute(
        'SELECT "id" FROM "events" WHERE "value" > 10'
    ).fetchall() == [(2,)]

    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# Schema mismatch preserves old snapshot
# ---------------------------------------------------------------------------


def test_delta_schema_mismatch_preserves_old_snapshot(
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    """An observed schema with an extra column is rejected; the old generation
    remains queryable and no location/profile secret reaches any error surface."""

    # The location path deliberately carries a ``secret`` token so the
    # sanitized-error assertions are meaningful.
    location = tmp_path / "events_secret"
    write_deltalake(location, _events_table({"id": [1, 2, 3], "value": [1, 2, 3]}))

    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        SemanticLayer(
            1,
            "drift",
            "",
            "",
            {
                "events": DataSource(
                    name="events",
                    connector=DeltaConfig(str(location)),
                    schema=_events_schema(),
                    grain=("id",),
                )
            },
            {},
            {},
            {},
            {},
            {},
        ),
        connection,
        profiles,
        providers,
    )

    assert registry.status("events").generation == 1
    assert registry.execute('SELECT sum("id") FROM "events"').fetchone() == (6,)

    # Overwrite the Delta table with a drifted schema (extra column).
    drifted_schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("value", pa.int64(), nullable=False),
            pa.field("extra", pa.int64(), nullable=False),
        ]
    )
    write_deltalake(
        location,
        pa.table(
            {"id": [1, 2, 3], "value": [1, 2, 3], "extra": [9, 9, 9]},
            schema=drifted_schema,
        ),
        mode="overwrite",
        schema_mode="overwrite",
    )

    with pytest.raises(SourceSchemaError) as caught:
        registry.reload_source("events")

    assert caught.value.code == "schema_mismatch"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)

    # The failed reload did not swap the registration: the generation is
    # unchanged and the previously registered data is still queryable.
    assert registry.status("events").generation == 1
    assert registry.execute('SELECT sum("id") FROM "events"').fetchone() == (6,)

    registry.close()


# ---------------------------------------------------------------------------
# Missing deltalake extra surfaces as SourceDependencyError
# ---------------------------------------------------------------------------


def test_missing_deltalake_extra_is_source_dependency_error(
    monkeypatch,
    tmp_path: Path,
    profiles: RuntimeProfileResolver,
    providers: ArrowProviderResolver,
) -> None:
    location = tmp_path / "events_secret"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location)),
        schema=_events_schema(),
        grain=("id",),
    )

    # Simulate the optional ``delta`` extra not being installed.
    monkeypatch.setattr("selayer.sources.adapters.delta._DeltaTable", None)

    adapter = DeltaAdapter()
    with pytest.raises(SourceDependencyError) as caught:
        adapter.prepare(source, profiles, providers)

    assert caught.value.code == "missing_delta_dependency"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)


# ---------------------------------------------------------------------------
# S3 profile builds filesystem
# ---------------------------------------------------------------------------


def test_delta_s3_profile_builds_filesystem(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A delta source with ``credential_profile`` resolves an S3 filesystem.

    The adapter *wraps* the resolved filesystem in a
    :class:`pyarrow.fs.SubTreeFileSystem` rooted at the table directory before
    passing it to ``DeltaTable.to_pyarrow_dataset``, so the relative file paths
    in the Delta log resolve through the subtree root.  A
    :class:`pyarrow.fs.LocalFileSystem` stands in for the S3FileSystem so the
    test needs no Docker.
    """

    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))

    resolved_profiles: list[str] = []
    # The fake S3FileSystem is a *plain* LocalFileSystem — the adapter must
    # add the SubTreeFileSystem rooting itself, not the test.
    recording_fs = pafs.LocalFileSystem()

    def fake_s3_filesystem(_profile: object) -> pafs.FileSystem:
        resolved_profiles.append("s3_profile")
        return recording_fs

    monkeypatch.setattr(
        "selayer.sources.adapters.delta.s3_filesystem", fake_s3_filesystem
    )

    # Record the SubTreeFileSystem the adapter constructs so the rooting and
    # base filesystem are asserted, not merely that no exception was raised.
    subtree_calls: list[tuple[str, pafs.FileSystem]] = []
    real_subtree = pafs.SubTreeFileSystem

    def recording_subtree(base_path: str, base_fs: pafs.FileSystem) -> pafs.FileSystem:
        subtree_calls.append((base_path, base_fs))
        return real_subtree(base_path, base_fs)

    monkeypatch.setattr(
        "selayer.sources.adapters.delta.pafs.SubTreeFileSystem", recording_subtree
    )

    source = ParsedSource(
        name="events",
        connector=DeltaConfig(str(location), credential_profile="s3_profile"),
        schema=_events_schema(),
        grain=("id",),
    )
    profiles = MappingProfileResolver(
        {"s3_profile": {"access_key": "AKIA_SECRET", "secret_key": "shh_secret"}}
    )

    adapter = DeltaAdapter()
    handle = adapter.prepare(source, profiles, MappingArrowProviderResolver({}))

    assert resolved_profiles == ["s3_profile"]
    # The adapter rooted exactly one SubTreeFileSystem at the table directory,
    # wrapping the resolved S3 filesystem as its base.
    assert len(subtree_calls) == 1
    base_path, base_fs = subtree_calls[0]
    assert base_path == str(location)
    assert base_fs is recording_fs

    connection = duckdb.connect(":memory:")
    adapter.register(connection, "events", handle)
    assert connection.execute('SELECT sum("value") FROM "events"').fetchone() == (10,)
    adapter.close(handle)
    connection.close()


# ---------------------------------------------------------------------------
# SubTreeFileSystem root path derivation (s3:// stripping)
# ---------------------------------------------------------------------------


def test_delta_s3_root_path_strips_scheme_and_trailing_slash() -> None:
    """``_s3_root_path`` strips ``s3://`` and any trailing slash.

    The wrapped S3FileSystem owns the ``s3://`` scheme/host, so the
    SubTreeFileSystem root is a clean ``bucket/prefix`` path.  Local paths are
    returned unchanged (trailing slash stripped) so local behavior is exact.
    """

    from selayer.sources.adapters.delta import _s3_root_path

    assert _s3_root_path("s3://my-bucket/data/events/") == "my-bucket/data/events"
    assert _s3_root_path("s3://my-bucket/data/events") == "my-bucket/data/events"
    # A local path has no scheme to strip; only a trailing slash is removed.
    assert _s3_root_path("/local/path/events.delta") == "/local/path/events.delta"
    assert _s3_root_path("/local/path/events.delta/") == "/local/path/events.delta"


# ---------------------------------------------------------------------------
# Storage options credential modes
# ---------------------------------------------------------------------------


def test_delta_storage_options_explicit_credentials() -> None:
    """Explicit access/secret/session are forwarded plus region/endpoint."""

    from selayer.sources.adapters.delta import _delta_storage_options

    profile = RuntimeProfile(
        "s3",
        {
            "access_key": "AKIA_EXPLICIT",
            "secret_key": "SECRET_EXPLICIT",
            "session_token": "TOKEN_EXPLICIT",
            "region": "us-east-1",
            "endpoint_override": "http://minio:9000",
        },
    )
    assert _delta_storage_options(profile) == {
        "AWS_ACCESS_KEY_ID": "AKIA_EXPLICIT",
        "AWS_SECRET_ACCESS_KEY": "SECRET_EXPLICIT",
        "AWS_SESSION_TOKEN": "TOKEN_EXPLICIT",
        "AWS_REGION": "us-east-1",
        "AWS_ENDPOINT_URL": "http://minio:9000",
    }


def test_delta_storage_options_profile_name_uses_aws_profile() -> None:
    """A named/default profile emits ``AWS_PROFILE`` (not resolved creds)."""

    from selayer.sources.adapters.delta import _delta_storage_options

    profile = RuntimeProfile(
        "s3",
        {"profile_name": "dev", "region": "us-west-2"},
    )
    assert _delta_storage_options(profile) == {
        "AWS_PROFILE": "dev",
        "AWS_REGION": "us-west-2",
    }


def test_delta_storage_options_default_chain_propagates_region_only() -> None:
    """No credentials and no profile name propagate region/endpoint only."""

    from selayer.sources.adapters.delta import _delta_storage_options

    profile = RuntimeProfile("s3", {"region": "eu-central-1"})
    assert _delta_storage_options(profile) == {"AWS_REGION": "eu-central-1"}


def test_delta_storage_options_role_arn(monkeypatch) -> None:
    """``role_arn`` assumes the role via STS and forwards temporary creds."""

    pytest.importorskip("boto3")
    from selayer.sources.adapters import arrow as arrow_mod
    from selayer.sources.adapters.delta import _delta_storage_options

    class _STS:
        def assume_role(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["RoleArn"] == "arn:aws:iam::1:role/selayer"
            assert kwargs["RoleSessionName"] == "selayer-session"
            return {
                "Credentials": {
                    "AccessKeyId": "ROLE_AK",
                    "SecretAccessKey": "ROLE_SK",
                    "SessionToken": "ROLE_TOK",
                }
            }

    class _Client:
        def __init__(self, service_name: str, **_kwargs: object) -> None:
            assert service_name == "sts"
            self._sts = _STS()

        def assume_role(self, **kwargs: object) -> dict[str, object]:
            return self._sts.assume_role(**kwargs)

    fake_boto3 = types.SimpleNamespace(client=_Client)
    monkeypatch.setattr(arrow_mod, "boto3", fake_boto3)

    profile = RuntimeProfile(
        "s3",
        {
            "role_arn": "arn:aws:iam::1:role/selayer",
            "session_name": "selayer-session",
            "region": "eu-west-1",
        },
    )
    assert _delta_storage_options(profile) == {
        "AWS_ACCESS_KEY_ID": "ROLE_AK",
        "AWS_SECRET_ACCESS_KEY": "ROLE_SK",
        "AWS_SESSION_TOKEN": "ROLE_TOK",
        "AWS_REGION": "eu-west-1",
    }


def test_delta_storage_options_role_arn_without_boto3_is_sanitized(
    monkeypatch,
) -> None:
    """``role_arn`` without boto3 raises a constant error with no leak."""

    from selayer.sources.adapters import arrow as arrow_mod
    from selayer.sources.adapters.delta import _delta_storage_options

    monkeypatch.setattr(arrow_mod, "boto3", None)

    role_secret = "arn:aws:iam::1:role/SECRETROLE"
    profile = RuntimeProfile(
        "s3",
        {"role_arn": role_secret, "region": "eu-west-1"},
    )
    with pytest.raises(ValueError) as caught:
        _delta_storage_options(profile)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SECRETROLE" not in repr(caught.value)
    assert "SECRETROLE" not in repr(caught.value.args)


def test_delta_storage_options_hostile_subclass_is_rejected() -> None:
    """A hostile ``str`` subclass credential is rejected before any dunder."""

    from selayer.sources.adapters import arrow as arrow_mod
    from selayer.sources.adapters.delta import _delta_storage_options

    hostile = "HOSTILE_STR_DUNDER_SENTINEL"

    class _LeakyStr(str):
        __slots__ = ()

        def __repr__(self) -> str:
            raise RuntimeError(hostile)

        def __hash__(self) -> int:
            raise RuntimeError(hostile)

        def __eq__(self, other: object) -> bool:
            raise RuntimeError(hostile)

    profile = RuntimeProfile(
        "s3",
        {"access_key": _LeakyStr("AKIA"), "secret_key": "shh"},
    )
    with pytest.raises(ValueError) as caught:
        _delta_storage_options(profile)

    assert hostile not in repr(caught.value)
    # The sentinel must not be imported into the arrow module namespace.
    assert not hasattr(arrow_mod, "_LeakyStr")


# ---------------------------------------------------------------------------
# Status contains integer version only
# ---------------------------------------------------------------------------


def test_delta_status_contains_integer_version_only(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    with QueryEngine(layer) as engine:
        status = engine.source_status("events")
        snapshot = status.snapshot
        assert snapshot is not None
        # The snapshot is a bare integer version string — no path, URI, or
        # other detail.
        assert snapshot.isdigit()
        assert "/" not in snapshot
        assert ":" not in snapshot
        assert "." not in snapshot

        # After an append the version increments and the snapshot changes.
        write_deltalake(
            location,
            _events_table({"id": [2], "value": [20]}),
            mode="append",
        )
        engine.reload_source("events")
        new_status = engine.source_status("events")
        assert new_status.snapshot is not None
        assert new_status.snapshot.isdigit()
        assert new_status.snapshot != snapshot


# ---------------------------------------------------------------------------
# Close after reload and engine close
# ---------------------------------------------------------------------------


def test_delta_handles_close_after_reload_and_engine_close(
    monkeypatch,
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    """Reload and engine close clear both old and current Delta resources.

    Spy objects wrap ``DeltaAdapter.prepare``/``close`` to capture every
    handle, proving that the *old* DeltaTable and Dataset resources are
    cleared after a reload and the *current* ones are cleared after the engine
    closes — not merely that no exception is raised.
    """

    prepared: list[SourceHandle] = []
    closed: list[SourceHandle] = []
    real_prepare = DeltaAdapter.prepare
    real_close = DeltaAdapter.close

    def recording_prepare(
        self: DeltaAdapter,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        providers: ArrowProviderResolver,
    ) -> SourceHandle:
        handle = real_prepare(self, source, profiles, providers)
        prepared.append(handle)
        return handle

    def recording_close(self: DeltaAdapter, handle: SourceHandle) -> None:
        closed.append(handle)
        real_close(self, handle)

    monkeypatch.setattr(DeltaAdapter, "prepare", recording_prepare)
    monkeypatch.setattr(DeltaAdapter, "close", recording_close)

    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    engine = QueryEngine(layer)
    assert engine.query(["total_value"])["total_value"].item() == 10

    # The first handle carries populated DeltaTable and Dataset resources.
    assert len(prepared) == 1
    first = prepared[0]
    assert first.resource.table is not None  # type: ignore[attr-defined]
    assert first.resource.dataset is not None  # type: ignore[attr-defined]

    write_deltalake(
        location,
        _events_table({"id": [2], "value": [20]}),
        mode="append",
    )
    engine.reload_source("events")
    assert engine.query(["total_value"])["total_value"].item() == 30

    # Two handles have been prepared (old + current); the OLD handle's
    # DeltaTable and Dataset were cleared by the reload swap.
    assert len(prepared) == 2
    current = prepared[1]
    assert current.resource.table is not None  # type: ignore[attr-defined]
    assert current.resource.dataset is not None  # type: ignore[attr-defined]
    assert first in closed
    assert first.resource.table is None  # type: ignore[attr-defined]
    assert first.resource.dataset is None  # type: ignore[attr-defined]

    # Closing the engine clears the CURRENT handle's resources too.
    engine.close()
    assert current in closed
    assert current.resource.table is None  # type: ignore[attr-defined]
    assert current.resource.dataset is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Step 7: Delta grain audit smoke test
# ---------------------------------------------------------------------------


def test_delta_grain_audit_reports_clean_full_scan(
    tmp_path: Path,
    delta_layer_factory: Callable[[str | Path], SemanticLayer],
) -> None:
    """A full grain audit over a Delta source reports connector kind,
    generation, schema fingerprint, a safe integer snapshot, full-scan scope,
    and zero leaked location text.
    """

    location = tmp_path / "events.delta"
    write_deltalake(location, _events_table({"id": [1, 2, 3], "value": [10, 20, 30]}))
    layer = delta_layer_factory(location)

    report = verify(layer, PhysicalCheck())
    outcome = next(
        item for item in report.outcomes if item.check_id == "source.events.grain"
    )
    assert outcome.status == "passed"
    assert outcome.scope == "full_scan"
    assert outcome.evidence["connector"] == "delta"
    assert outcome.evidence["generation"] == 1
    assert isinstance(outcome.evidence["schema_fingerprint"], str)
    snapshot = outcome.evidence["snapshot"]
    assert snapshot is not None
    assert isinstance(snapshot, str)
    assert snapshot.isdigit()
    assert outcome.evidence["row_count"] == 3
    assert outcome.evidence["null_grain_rows"] == 0
    assert outcome.evidence["duplicate_grain_groups"] == 0
    rendered = repr(report.to_dict())
    assert str(location) not in rendered
