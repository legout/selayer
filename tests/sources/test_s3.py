"""S3 transport profile tests: credential resolution, secrecy, and validation.

These tests cover ``s3_filesystem(profile)`` — the internal factory in
:mod:`selayer.sources.adapters.arrow` that resolves a
:class:`~selayer.sources.profiles.RuntimeProfile` to a
:class:`pyarrow.fs.S3FileSystem`.

Three credential paths are exercised:

* **explicit credentials** — ``access_key``/``secret_key`` (and optional
  ``session_token``) are passed frozen to PyArrow.
* **boto3 default chain** — when no explicit credentials are present, boto3's
  standard chain resolves credentials.
* **boto3 role session** — when ``role_arn`` is present, STS assumes the role
  and returns temporary credentials.

Every failure path is asserted to be secret-free: the secret sentinel appears
in none of ``error.args``, ``repr(error)``, the formatted traceback,
``__cause__``, or ``__context__``.
"""

from __future__ import annotations

import io
import traceback
import types
from collections.abc import Callable, Mapping
from typing import NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from selayer.model import SemanticLayer
from selayer.sources.adapters.arrow import s3_filesystem
from selayer.sources.profiles import (
    MappingProfileResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)

# ---------------------------------------------------------------------------
# Secret sentinels
# ---------------------------------------------------------------------------

# Every secrecy test constructs a profile whose values are these sentinels,
# triggers a failure, and asserts the sentinel never appears in any rendered
# error surface (error.args repr, formatted traceback, cause, context).
_ACCESS = "AKIA_ACCESS_SENTINEL"
_SECRET = "SUPER_SECRET_SENTINEL"
_TOKEN = "TOKEN_SECRET_SENTINEL"
_USERINFO = "USERINFO_SECRET_SENTINEL"
# Sentinel carried by the hostile ``str`` subclass's dunders.  If validation
# invokes any dunder (``__str__``/``__hash__``/``__eq__``/``__repr__``) on a
# hostile instance before rejecting it by exact builtin type, the dunder
# raises :class:`RuntimeError` carrying this sentinel, surfacing it in the
# propagated exception's ``args`` and the formatted traceback.
_HOSTILE = "HOSTILE_STR_DUNDER_SENTINEL"


def _assert_no_secret_leak(error: BaseException, *sentinels: str) -> None:
    """Assert no sentinel appears in any rendered error surface.

    The checked surfaces are: ``repr(error)``, ``repr(error.args)``, the
    formatted traceback (including the raise frame's source context),
    ``repr(error.__cause__)``, and ``repr(error.__context__)``.  A constant
    error message (no echoed profile value) and raising outside any
    ``except`` scope keep every surface sentinel-free.
    """

    tb_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    surfaces = [
        repr(error),
        repr(error.args),
        tb_text,
        repr(error.__cause__),
        repr(error.__context__),
    ]
    for surface in surfaces:
        for sentinel in sentinels:
            assert sentinel not in surface, (
                f"secret sentinel {sentinel!r} leaked into error surface"
            )


# ---------------------------------------------------------------------------
# Hostile str subclass
# ---------------------------------------------------------------------------


class _LeakyStr(str):
    """Hostile ``str`` subclass whose dunders raise carrying ``_HOSTILE``.

    If any validation path invokes ``__str__``, ``__repr__``, ``__hash__``,
    or ``__eq__`` on an instance *before* rejecting it by exact builtin type,
    the dunder raises :class:`RuntimeError` carrying ``_HOSTILE``, surfacing
    the sentinel in the propagated exception's ``args`` and the formatted
    traceback.  The exact-``str`` type guard in
    :func:`~selayer.sources.adapters.arrow._validate_s3_profile` rejects the
    instance before any dunder runs.
    """

    __slots__ = ()

    def __str__(self) -> str:
        raise RuntimeError(_HOSTILE)

    def __repr__(self) -> str:
        raise RuntimeError(_HOSTILE)

    def __hash__(self) -> int:
        raise RuntimeError(_HOSTILE)

    def __eq__(self, other: object) -> bool:
        raise RuntimeError(_HOSTILE)


# ---------------------------------------------------------------------------
# Step 1: profile → filesystem secrecy tests
# ---------------------------------------------------------------------------


def test_s3_profile_builds_filesystem_without_repr_leak(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_filesystem(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", fake_filesystem
    )
    profile = RuntimeProfile(
        "analytics_s3",
        {
            "access_key": "ACCESS_SECRET",
            "secret_key": "SECRET_SECRET",
            "session_token": "TOKEN_SECRET",
            "region": "eu-central-1",
            "endpoint_override": "http://127.0.0.1:9000",
            "scheme": "http",
        },
    )

    s3_filesystem(profile)

    assert captured["access_key"] == "ACCESS_SECRET"
    assert captured["secret_key"] == "SECRET_SECRET"
    assert captured["session_token"] == "TOKEN_SECRET"
    assert captured["region"] == "eu-central-1"
    assert captured["endpoint_override"] == "http://127.0.0.1:9000"
    assert captured["scheme"] == "http"
    assert "SECRET_SECRET" not in repr(profile)


def test_s3_defaults_to_https(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_filesystem(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", fake_filesystem
    )
    profile = RuntimeProfile(
        "plain",
        {"access_key": "AKIAEXAMPLE", "secret_key": "examplesecret"},
    )
    s3_filesystem(profile)

    assert captured["scheme"] == "https"
    assert captured["endpoint_override"] is None


def test_boto_default_chain_credentials(monkeypatch) -> None:
    pytest.importorskip("boto3")
    from selayer.sources.adapters import arrow as arrow_mod

    session_captured: dict[str, object] = {}

    class _Frozen:
        access_key = "CHAIN_ACCESS_KEY"
        secret_key = "CHAIN_SECRET_KEY"
        token = "CHAIN_TOKEN"

    class _Creds:
        def get_frozen_credentials(self) -> _Frozen:
            return _Frozen()

    class _Session:
        def __init__(self, **kwargs: object) -> None:
            session_captured.update(kwargs)

        def get_credentials(self) -> _Creds:
            return _Creds()

    fake_boto3 = types.SimpleNamespace(Session=_Session)
    monkeypatch.setattr(arrow_mod, "boto3", fake_boto3)

    fs_captured: dict[str, object] = {}

    def fake_filesystem(**kwargs: object) -> object:
        fs_captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", fake_filesystem
    )

    profile = RuntimeProfile(
        "default_chain",
        {"region": "us-west-2", "profile_name": "dev"},
    )
    s3_filesystem(profile)

    assert fs_captured["access_key"] == "CHAIN_ACCESS_KEY"
    assert fs_captured["secret_key"] == "CHAIN_SECRET_KEY"
    assert fs_captured["session_token"] == "CHAIN_TOKEN"
    assert fs_captured["region"] == "us-west-2"
    assert session_captured["profile_name"] == "dev"


def test_boto_role_session_credentials(monkeypatch) -> None:
    pytest.importorskip("boto3")
    from selayer.sources.adapters import arrow as arrow_mod

    sts_captured: dict[str, object] = {}

    class _STS:
        def assume_role(self, **kwargs: object) -> Mapping[str, object]:
            sts_captured.update(kwargs)
            return {
                "Credentials": {
                    "AccessKeyId": "ROLE_ACCESS_KEY",
                    "SecretAccessKey": "ROLE_SECRET_KEY",
                    "SessionToken": "ROLE_TOKEN",
                }
            }

    class _Client:
        def __init__(self, service_name: str, **_kwargs: object) -> None:
            assert service_name == "sts"
            self._sts = _STS()

        def assume_role(self, **kwargs: object) -> Mapping[str, object]:
            return self._sts.assume_role(**kwargs)

    fake_boto3 = types.SimpleNamespace(client=_Client)
    monkeypatch.setattr(arrow_mod, "boto3", fake_boto3)

    fs_captured: dict[str, object] = {}

    def fake_filesystem(**kwargs: object) -> object:
        fs_captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", fake_filesystem
    )

    role_arn = "arn:aws:iam::123456789012:role/selayer"
    profile = RuntimeProfile(
        "role_session",
        {
            "role_arn": role_arn,
            "external_id": "external-id-value",
            "session_name": "selayer-session",
            "region": "eu-west-1",
        },
    )
    s3_filesystem(profile)

    assert fs_captured["access_key"] == "ROLE_ACCESS_KEY"
    assert fs_captured["secret_key"] == "ROLE_SECRET_KEY"
    assert fs_captured["session_token"] == "ROLE_TOKEN"
    assert fs_captured["region"] == "eu-west-1"
    assert sts_captured["RoleArn"] == role_arn
    assert sts_captured["ExternalId"] == "external-id-value"
    assert sts_captured["RoleSessionName"] == "selayer-session"


def test_unknown_s3_profile_key_is_rejected() -> None:
    profile = RuntimeProfile(
        "bad",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "rogue_key": "should be rejected",
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET)


def test_invalid_endpoint_is_rejected() -> None:
    profile = RuntimeProfile(
        "bad_endpoint",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "session_token": _TOKEN,
            "endpoint_override": f"http://user:{_USERINFO}@127.0.0.1:9000",
            "scheme": "http",
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _TOKEN, _USERINFO)


def _noop_filesystem(**_kwargs: object) -> object:
    """A stand-in S3FileSystem that never fails, so a hostile value reaching it
    would still produce a successful (non-error) result — making a missing
    rejection observable as ``DID NOT RAISE`` rather than a driver error."""

    return object()


def test_invalid_s3_scheme_is_rejected() -> None:
    """A builtin-but-unsupported scheme is rejected with a constant error."""

    profile = RuntimeProfile(
        "bad_scheme",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "session_token": _TOKEN,
            "scheme": "ftp",
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _TOKEN)


def test_hostile_str_subclass_scheme_is_rejected(monkeypatch) -> None:
    """A hostile ``str`` subclass scheme is rejected by type before any hash.

    The membership test ``scheme not in _VALID_S3_SCHEMES`` invokes
    ``__hash__``; with the value being a hostile subclass whose ``__hash__``
    raises carrying ``_HOSTILE``, the exact-``str`` guard must reject it first.
    """

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", _noop_filesystem
    )
    profile = RuntimeProfile(
        "hostile_scheme",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "session_token": _TOKEN,
            "scheme": _LeakyStr("https"),
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _TOKEN, _HOSTILE)


def test_hostile_str_subclass_endpoint_is_rejected(monkeypatch) -> None:
    """A hostile ``str`` subclass endpoint is rejected by type before ``str()``.

    ``urlparse(str(endpoint))`` invokes ``__str__``; with the value being a
    hostile subclass whose ``__str__`` raises carrying ``_HOSTILE``, the
    exact-``str`` guard must reject it first.
    """

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", _noop_filesystem
    )
    profile = RuntimeProfile(
        "hostile_endpoint",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "session_token": _TOKEN,
            "endpoint_override": _LeakyStr("http://127.0.0.1:9000"),
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _TOKEN, _HOSTILE)


def test_hostile_str_subclass_credential_is_rejected(monkeypatch) -> None:
    """A hostile ``str`` subclass credential is rejected before reaching Arrow.

    Without the exact-``str`` guard a hostile ``access_key`` would pass
    validation and reach the (patched) constructor; the test asserts it is
    rejected with a constant error instead.
    """

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", _noop_filesystem
    )
    profile = RuntimeProfile(
        "hostile_credential",
        {
            "access_key": _LeakyStr(_ACCESS),
            "secret_key": _SECRET,
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _HOSTILE)


def test_s3_filesystem_driver_failure_is_sanitized(monkeypatch) -> None:
    """A driver failure that echoes credentials is sanitized to a constant error.

    ``pyarrow.fs.S3FileSystem`` receives the access key, secret key, and
    session token; a failure there may echo them in the driver exception's
    message/repr.  The call is wrapped so a constant ``ValueError`` is raised
    *outside* any ``except`` scope (keeping ``__cause__``/``__context__``
    ``None``), discarding the driver exception entirely.
    """

    def exploding_filesystem(**_kwargs: object) -> object:
        raise RuntimeError(f"driver error exposing {_SECRET} and {_TOKEN}")

    monkeypatch.setattr(
        "selayer.sources.adapters.arrow.pafs.S3FileSystem", exploding_filesystem
    )
    profile = RuntimeProfile(
        "bad_driver",
        {
            "access_key": _ACCESS,
            "secret_key": _SECRET,
            "session_token": _TOKEN,
        },
    )
    with pytest.raises(Exception) as caught:
        s3_filesystem(profile)
    _assert_no_secret_leak(caught.value, _ACCESS, _SECRET, _TOKEN)


# ---------------------------------------------------------------------------
# Step 2: MinIO reload integration test (testcontainers)
# ---------------------------------------------------------------------------


class MinioSourceFixture(NamedTuple):
    """Typed bundle returned by the ``minio_source_fixture``."""

    layer: SemanticLayer
    profiles: RuntimeProfileResolver
    upload_second_file: Callable[[], None]


def _parquet_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _events_schema_table() -> pa.Table:
    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("value", pa.int64(), nullable=False),
        ]
    )
    return pa.table(
        {"id": pa.array([1], pa.int64()), "value": pa.array([10], pa.int64())},
        schema=schema,
    )


def _events_schema_table_two() -> pa.Table:
    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("value", pa.int64(), nullable=False),
        ]
    )
    return pa.table(
        {"id": pa.array([2], pa.int64()), "value": pa.array([20], pa.int64())},
        schema=schema,
    )


def _docker_available() -> bool:
    """Return ``True`` when the Docker daemon is reachable.

    The Docker SDK's ``ping`` is probed directly so the MinIO fixture skips
    *only* when Docker is genuinely unavailable; a healthy daemon followed by
    a MinIO image/container failure re-raises (and fails CI) rather than being
    masked as a skip.
    """

    try:
        import docker
    except ImportError:
        return False
    try:
        return bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001 - any connection/unavailable error = unavailable
        return False


@pytest.fixture
def minio_source_fixture() -> (
    pytest.fixture  # type: ignore[misc]
):
    """Start MinIO via testcontainers, upload a Parquet file, build a layer.

    Yields a :class:`MinioSourceFixture`.  Skipped *only* when Docker or a
    required dependency (boto3, testcontainers) is genuinely unavailable.  When
    Docker is healthy, a MinIO image/container startup failure re-raises so CI
    fails rather than silently skipping.
    """

    pytest.importorskip("boto3")
    pytest.importorskip("testcontainers")
    if not _docker_available():
        pytest.skip("Docker daemon is not available")
    import boto3

    try:
        from testcontainers.community.minio import MinioContainer
    except ImportError:
        try:
            from testcontainers.minio import (  # type: ignore[no-redef]
                MinioContainer,
            )
        except ImportError:
            pytest.skip("testcontainers[minio] not available")

    # With Docker confirmed healthy, a MinIO image/container startup failure is
    # a *real* failure: it re-raises so CI fails rather than silently skipping.
    minio: MinioContainer = MinioContainer()
    minio.start()
    try:
        config = minio.get_config()
        endpoint = config["endpoint"]
        access_key = config["access_key"]
        secret_key = config["secret_key"]
        endpoint_url = f"http://{endpoint}"

        bucket = "events-bucket"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )
        client.create_bucket(Bucket=bucket)

        client.put_object(
            Bucket=bucket,
            Key="data/part-0.parquet",
            Body=_parquet_bytes(_events_schema_table()),
        )

        def upload_second_file() -> None:
            client.put_object(
                Bucket=bucket,
                Key="data/part-1.parquet",
                Body=_parquet_bytes(_events_schema_table_two()),
            )

        from selayer.model import (
            DataSource,
            Fact,
            Measure,
            Metric,
        )
        from selayer.sources.config import ParquetConfig
        from selayer.sources.schema import (
            FieldSchema,
            ScalarType,
            TableSchema,
        )

        layer = SemanticLayer(
            1,
            "s3_integration",
            "",
            "",
            {
                "events": DataSource(
                    name="events",
                    connector=ParquetConfig(
                        f"s3://{bucket}/data/",
                        credential_profile="minio",
                    ),
                    schema=TableSchema(
                        (
                            FieldSchema("id", ScalarType("int64"), False),
                            FieldSchema("value", ScalarType("int64"), False),
                        )
                    ),
                    grain=("id",),
                )
            },
            {},
            {
                "event_id": Fact.from_expression(
                    "event_id", "events", "events.id", "integer"
                )
            },
            {"event_count": Measure("event_count", "event_id", "count")},
            {
                "row_count": Metric.from_expression(
                    "row_count", "event_count", ("event_count",)
                )
            },
            {},
        )

        yield MinioSourceFixture(
            layer=layer,
            profiles=MappingProfileResolver(
                {
                    "minio": {
                        "access_key": access_key,
                        "secret_key": secret_key,
                        "region": "us-east-1",
                        "endpoint_override": endpoint_url,
                        "scheme": "http",
                    }
                }
            ),
            upload_second_file=upload_second_file,
        )
    finally:
        minio.stop()


@pytest.mark.integration
def test_s3_parquet_reload_discovers_new_objects(
    minio_source_fixture: MinioSourceFixture,
) -> None:
    from selayer.query import QueryEngine

    layer, profiles, upload_second_file = minio_source_fixture
    with QueryEngine(layer, profiles=profiles) as engine:
        assert engine.query(["row_count"])["row_count"].item() == 1
        upload_second_file()
        engine.reload_source("events")
        assert engine.query(["row_count"])["row_count"].item() == 2
