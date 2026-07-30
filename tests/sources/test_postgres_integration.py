"""PostgreSQL adapter integration tests via testcontainers.

These tests exercise the real
:class:`~selayer.sources.adapters.database.PostgresAdapter` through the
:class:`~selayer.query.QueryEngine` against a live PostgreSQL instance started
via ``testcontainers[postgres]``.  They are marked ``@pytest.mark.integration``
and skip cleanly when ``psycopg`` or the Docker daemon is unavailable,
consistent with the repository's S3 integration-test conventions.

Secrecy contract — connection failure and credential-leakage tests assert that
the DSN (including the password) never appears in any rendered error surface.
The DSN is never written to assertion output.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import NamedTuple

import pytest

from selayer.catalog import SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.config import PostgresConfig
from selayer.sources.errors import SourceConnectionError
from selayer.sources.profiles import MappingProfileResolver, RuntimeProfileResolver
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _facts_schema() -> TableSchema:
    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


# ---------------------------------------------------------------------------
# Secrecy helper
# ---------------------------------------------------------------------------


def _assert_no_secret_leak(error: BaseException, *sentinels: str) -> None:
    """Assert no sentinel appears in any rendered error surface."""

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
# Layer factory
# ---------------------------------------------------------------------------


def _postgres_layer(profile: str, relation: str = "public.facts") -> SemanticLayer:
    """A single-source SemanticLayer over a PostgreSQL relation."""

    return SemanticLayer(
        1,
        "pg_integration",
        "",
        "",
        {
            "facts": DataSource(
                "facts",
                PostgresConfig(profile, relation),
                _facts_schema(),
                ("id",),
            )
        },
        {},
        {
            "facts_value": Fact.from_expression(
                "facts_value", "facts", "facts.value", "integer"
            )
        },
        {"total_value": Measure("total_value", "facts_value", "sum")},
        {
            "total_value": Metric.from_expression(
                "total_value", "total_value", ("total_value",)
            )
        },
        {},
    )


# ---------------------------------------------------------------------------
# Docker availability
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return ``True`` when the Docker daemon is reachable.

    Mirrors the S3 integration test's probe so this fixture skips *only* when
    Docker is genuinely unavailable.
    """

    try:
        import docker
    except ImportError:
        return False
    try:
        return bool(docker.from_env().ping())
    except Exception:  # noqa: BLE001 - any connection error = unavailable
        return False


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


class PostgresSourceFixture(NamedTuple):
    """Typed bundle returned by the ``postgres_source_fixture``."""

    layer: SemanticLayer
    profiles: RuntimeProfileResolver
    insert_row: Callable[[int, int], None]
    host: str
    port: str
    dbname: str
    user: str
    password: str


@pytest.fixture
def postgres_source_fixture() -> (
    pytest.fixture  # type: ignore[misc]
):
    """Start PostgreSQL via testcontainers, create a ``facts`` table, build a layer.

    Yields a :class:`PostgresSourceFixture`.

    ``psycopg`` is an optional extra (``postgres``); a missing import skips the
    test.  The Docker daemon availability probe is the only other skip gate;
    with Docker healthy, any container startup failure re-raises so CI fails
    rather than silently skipping.  Connection details are used only to
    construct the runtime profile and the ``insert_row`` callback; they are
    never printed or written to assertion output.
    """

    psycopg = pytest.importorskip("psycopg")
    from testcontainers.community.postgres import PostgresContainer

    if not _docker_available():
        pytest.skip("Docker daemon is not available")

    container: PostgresContainer = PostgresContainer("postgres:16")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = str(container.get_exposed_port(5432))
        dbname = container.dbname
        user = container.username
        password = container.password

        # Create the facts table and seed one row.
        conninfo = (
            f"host={host} port={port} dbname={dbname} user={user} password={password}"
        )
        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'create table facts (id bigint not null, "value" bigint not null)'
                )
                cur.execute("insert into facts values (1, 10)")
            conn.commit()

        def insert_row(row_id: int, value: int) -> None:
            with psycopg.connect(conninfo) as conn:
                with conn.cursor() as cur:
                    cur.execute("insert into facts values (%s, %s)", (row_id, value))
                conn.commit()

        layer = _postgres_layer("warehouse", "public.facts")
        profiles = MappingProfileResolver(
            {
                "warehouse": {
                    "host": host,
                    "port": port,
                    "dbname": dbname,
                    "user": user,
                    "password": password,
                }
            }
        )

        yield PostgresSourceFixture(
            layer=layer,
            profiles=profiles,
            insert_row=insert_row,
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
        )
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_postgres_relation_reads_current_rows_and_reloads_metadata(
    postgres_source_fixture: PostgresSourceFixture,
) -> None:
    """The postgres scanner reads live rows and reload advances the generation.

    ``postgres_scanner`` pushes each query to PostgreSQL, so a row inserted
    directly into the table is visible immediately (without reload).  The
    reload re-introspects the relation and publishes a new generation.
    """

    layer, profiles, insert_row = (
        postgres_source_fixture.layer,
        postgres_source_fixture.profiles,
        postgres_source_fixture.insert_row,
    )
    with QueryEngine(layer, profiles=profiles) as engine:
        assert engine.query(["total_value"])["total_value"].item() == 10
        insert_row(2, 20)
        # postgres_scanner reads live; the new row is visible before reload.
        assert engine.query(["total_value"])["total_value"].item() == 30
        status = engine.reload_source("facts")
        assert status.new_generation == status.old_generation + 1


@pytest.mark.integration
def test_postgres_connection_failure_is_sanitized(
    postgres_source_fixture: PostgresSourceFixture,
) -> None:
    """An unreachable port surfaces as a sanitized error with no DSN leak.

    A profile pointing at the container host but an unused port causes the
    DuckDB postgres scanner connection to fail.  The driver exception (which
    echoes the DSN) is discarded at the registry boundary; only a constant
    ``SourceConnectionError`` surfaces.
    """

    fixture = postgres_source_fixture
    sentinel_user = "CONNFAIL_USER_SENTINEL"
    sentinel_pw = "CONNFAIL_PW_SENTINEL"
    bad_profiles = MappingProfileResolver(
        {
            "warehouse": {
                "host": fixture.host,
                "port": "1",
                "dbname": fixture.dbname,
                "user": sentinel_user,
                "password": sentinel_pw,
            }
        }
    )
    with pytest.raises(SourceConnectionError) as caught:
        QueryEngine(_postgres_layer("warehouse"), profiles=bad_profiles)
    assert caught.value.code == "source_initialization_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_no_secret_leak(caught.value, sentinel_user, sentinel_pw)


@pytest.mark.integration
def test_postgres_wrong_password_hides_credentials(
    postgres_source_fixture: PostgresSourceFixture,
) -> None:
    """A wrong password surfaces as a sanitized error with no credential leak.

    A profile pointing at the live container with a deliberately wrong password
    (a secret sentinel) causes PostgreSQL to reject authentication.  The
    sentinel never appears in any rendered error surface.
    """

    fixture = postgres_source_fixture
    secret_password = "WRONG_PASSWORD_SENTINEL"
    bad_profiles = MappingProfileResolver(
        {
            "warehouse": {
                "host": fixture.host,
                "port": fixture.port,
                "dbname": fixture.dbname,
                "user": fixture.user,
                "password": secret_password,
            }
        }
    )
    with pytest.raises(SourceConnectionError) as caught:
        QueryEngine(_postgres_layer("warehouse"), profiles=bad_profiles)
    assert caught.value.code == "source_initialization_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_no_secret_leak(caught.value, secret_password)
