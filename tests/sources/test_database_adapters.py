"""Native DuckDB ATTACH adapter tests for SQLite and DuckDB-file sources.

These tests exercise the real
:class:`~selayer.sources.adapters.database.SqliteAdapter` and
:class:`~selayer.sources.adapters.database.DuckDbAdapter` through the
:class:`~selayer.query.QueryEngine` and
:class:`~selayer.sources.registry.SourceRegistry` using real temporary database
files.  No mocks are used for normal reads or reloads; narrow fakes and
``monkeypatch`` are used only for failure and policy injection.

Secrecy contract — every failure path is asserted to be free of the location
or DSN: the sentinel appears in none of ``error.args``, ``repr(error)``, the
formatted traceback, ``__cause__``, or ``__context__``.
"""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from selayer.catalog import CatalogValidationError, SemanticLayer
from selayer.model import DataSource, Fact, Measure, Metric
from selayer.query import QueryEngine
from selayer.sources.adapters import database as dbmod
from selayer.sources.adapters.database import (
    DuckDbAdapter,
    SqliteAdapter,
    _quote_relation,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import DuckDbConfig, PostgresConfig, SqliteConfig
from selayer.sources.errors import (
    SourceConnectionError,
    SourceDependencyError,
    SourceSchemaError,
)
from selayer.sources.profiles import (
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _facts_schema() -> TableSchema:
    """Two-column schema matching a ``facts(id, value)`` database table."""

    return TableSchema(
        (
            FieldSchema("id", ScalarType("int64"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


def _codes_schema() -> TableSchema:
    """Schema matching a SQLite ``codes(code, value)`` table."""

    return TableSchema(
        (
            FieldSchema("code", ScalarType("utf8"), False),
            FieldSchema("value", ScalarType("int64"), False),
        )
    )


# ---------------------------------------------------------------------------
# Secrecy helper
# ---------------------------------------------------------------------------


def _assert_no_secret_leak(error: BaseException, *sentinels: str) -> None:
    """Assert no sentinel appears in any rendered error surface.

    The checked surfaces mirror the S3 test suite: ``repr(error)``,
    ``repr(error.args)``, the formatted traceback (including the raise
    frame's source context), ``repr(__cause__)``, and ``repr(__context__)``.
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
# Resolver helpers
# ---------------------------------------------------------------------------


def _empty_profiles() -> RuntimeProfileResolver:
    return MappingProfileResolver({})


def _empty_providers() -> MappingArrowProviderResolver:
    return MappingArrowProviderResolver({})


# ---------------------------------------------------------------------------
# Layer factories
# ---------------------------------------------------------------------------


@pytest.fixture
def database_layer_factory() -> Callable[[Path, Path], SemanticLayer]:
    """Return a SemanticLayer factory with SQLite ``codes`` and DuckDB ``facts``.

    Both sources expose a ``value`` column; ``total_value`` reads the DuckDB
    ``facts`` source while ``codes_total`` reads the SQLite ``codes`` source.
    """

    def factory(sqlite_path: Path, duckdb_path: Path) -> SemanticLayer:
        return SemanticLayer(
            1,
            "db_test",
            "",
            "",
            {
                "codes": DataSource(
                    "codes",
                    SqliteConfig(str(sqlite_path), "codes"),
                    _codes_schema(),
                    ("code",),
                ),
                "facts": DataSource(
                    "facts",
                    DuckDbConfig(str(duckdb_path), "facts"),
                    _facts_schema(),
                    ("id",),
                ),
            },
            {},
            {
                "codes_value": Fact.from_expression(
                    "codes_value", "codes", "codes.value", "integer"
                ),
                "facts_value": Fact.from_expression(
                    "facts_value", "facts", "facts.value", "integer"
                ),
            },
            {
                "codes_total": Measure("codes_total", "codes_value", "sum"),
                "total_value": Measure("total_value", "facts_value", "sum"),
            },
            {
                "codes_total": Metric.from_expression(
                    "codes_total", "codes_total", ("codes_total",)
                ),
                "total_value": Metric.from_expression(
                    "total_value", "total_value", ("total_value",)
                ),
            },
            {},
        )

    return factory


def _duckdb_layer(
    path: str | Path,
    relation: str = "facts",
    source_name: str = "facts",
    source_key: str | None = None,
) -> SemanticLayer:
    """A single-source SemanticLayer over a DuckDB file relation."""

    return SemanticLayer(
        1,
        "duckdb_test",
        "",
        "",
        {
            source_key or source_name: DataSource(
                source_name,
                DuckDbConfig(str(path), relation),
                _facts_schema(),
                ("id",),
            )
        },
        {},
        {
            "facts_value": Fact.from_expression(
                "facts_value", source_name, f"{source_name}.value", "integer"
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


def _postgres_layer(profile: str, relation: str) -> SemanticLayer:
    """A single-source SemanticLayer over a PostgreSQL relation."""

    return SemanticLayer(
        1,
        "pg_test",
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
# Brief example: SQLite + DuckDB load, query, and reload
# ---------------------------------------------------------------------------


def test_sqlite_and_duckdb_sources_query_and_reload(
    tmp_path: Path,
    database_layer_factory: Callable[[Path, Path], SemanticLayer],
) -> None:
    """Both adapters load real files and reload picks up externally written rows.

    SQLite permits concurrent connections, so the reload-after-external-write
    workflow is exercised through the SQLite ``codes`` source.  The DuckDB
    ``facts`` source reload is verified separately (a DuckDB file cannot be
    mutated through a second connection while attached — see
    ``test_duckdb_attachment_is_read_only``).
    """

    sqlite_path = tmp_path / "reference.sqlite"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            'create table codes (code text primary key, "value" integer not null)'
        )
        connection.execute("insert into codes values ('A', 1)")

    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            'create table facts as select 1::bigint id, 10::bigint "value"'
        )

    layer = database_layer_factory(sqlite_path, duckdb_path)
    with QueryEngine(layer) as engine:
        # Both adapters loaded; the metric reads the DuckDB facts source.
        assert engine.query(["total_value"])["total_value"].item() == 10
        # The SQLite source is registered and queryable through the engine.
        assert engine.query(["codes_total"])["codes_total"].item() == 1

        # Reloading the SQLite source picks up externally written rows
        # (SQLite permits concurrent connections).
        with sqlite3.connect(str(sqlite_path)) as writer:
            writer.execute("insert into codes values ('B', 2)")
            writer.commit()
        codes_result = engine.reload_source("codes")
        assert codes_result.new_generation == codes_result.old_generation + 1
        assert engine.query(["codes_total"])["codes_total"].item() == 3

        # The DuckDB reload mechanism advances the generation even though the
        # file cannot be externally mutated while attached.
        facts_result = engine.reload_source("facts")
        assert facts_result.new_generation == facts_result.old_generation + 1
        assert engine.query(["total_value"])["total_value"].item() == 10


# ---------------------------------------------------------------------------
# Relation segment quoting with reserved identifiers
# ---------------------------------------------------------------------------


def test_relation_segments_are_quoted(tmp_path: Path) -> None:
    """Each dot-separated relation segment is validated and individually quoted.

    A segment that is a SQL reserved word (``order``) is double-quoted so the
    generated reference ``alias."order"`` resolves.  Without per-segment quoting
    DuckDB would raise a parser error on the unquoted reserved word.  The unit
    assertions prove the quoting function output; the behavioral assertion
    proves a reserved-word table queries through QueryEngine.
    """

    # Unit level: every segment is individually double-quoted.
    assert _quote_relation("order") == '"order"'
    assert _quote_relation("main.facts") == '"main"."facts"'
    assert _quote_relation("public.order") == '"public"."order"'

    # Behavioral level: a reserved-word table name queries through QueryEngine.
    duckdb_path = tmp_path / "reserved.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute('create table "order" (id bigint, "value" bigint)')
        connection.execute('insert into "order" values (1, 7)')

    layer = _duckdb_layer(duckdb_path, relation="order")
    with QueryEngine(layer) as engine:
        assert engine.query(["total_value"])["total_value"].item() == 7


# ---------------------------------------------------------------------------
# Invalid semicolon/comment relation is a catalog-level error
# ---------------------------------------------------------------------------


def test_invalid_relation_segment_is_catalog_error(tmp_path: Path) -> None:
    """A relation containing semicolons or comments is rejected before DuckDB.

    The per-segment validator rejects SQL fragments such as
    ``id; DROP TABLE x`` and ``x--comment`` with a *constant* message so the
    fragment text never surfaces.  Through the engine the invalid relation
    surfaces as a sanitized ``SourceConnectionError`` — never as a DuckDB
    parser/catalog error or a successful injection.
    """

    # Unit level: every fragment is rejected with a constant message.
    for fragment in (
        "id; DROP TABLE facts",
        "facts-- comment",
        "facts/* */",
        "facts;",
        "facts/*",
    ):
        with pytest.raises(ValueError, match="invalid relation segment") as caught:
            _quote_relation(fragment)
        assert fragment not in str(caught.value)

    # Engine level: a source with a fragment relation is rejected before any
    # SQL reaches DuckDB.  The error is sanitized and the fragment never
    # appears in any error surface.
    fragment = "facts; DROP TABLE codes--"
    layer = _duckdb_layer(tmp_path / "unused.duckdb", relation=fragment)
    with pytest.raises(SourceConnectionError) as caught:
        QueryEngine(layer)
    assert caught.value.code == "source_initialization_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_no_secret_leak(caught.value, "DROP TABLE")


# ---------------------------------------------------------------------------
# Missing extension dependency error
# ---------------------------------------------------------------------------


def test_programmatic_source_name_cannot_inject_view_sql(tmp_path: Path) -> None:
    """A malicious programmatic source ID is rejected before any view SQL.

    The engine validates the declaration rules before opening any resource, so
    a source key carrying SQL metacharacters is rejected at construction with a
    catalog validation error and never reaches the stable-view CREATE statement.
    """

    path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute('create table facts (id bigint, "value" bigint)')
        connection.execute("insert into facts values (1, 10)")

    malicious = 'facts"; CREATE TABLE injected AS SELECT 1; --'
    with pytest.raises(CatalogValidationError) as caught:
        QueryEngine(_duckdb_layer(path, source_name="facts", source_key=malicious))
    assert any("must match" in issue.message for issue in caught.value.issues)

    # The malicious key never reached the stable-view CREATE statement, so the
    # injection payload created no object in the target database file.
    with duckdb.connect(str(path)) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "select table_name from information_schema.tables"
            ).fetchall()
        }
    assert "injected" not in names


@pytest.mark.parametrize(
    "fragment",
    [
        'facts"; CREATE TABLE injected AS SELECT 1; --',
        "'; DROP TABLE x; --",
        "weird name",
        "Has-Periods",
    ],
)
def test_stable_view_name_validation_rejects_sql_fragments(fragment: str) -> None:
    """The adapter revalidates the stable view name as defense in depth.

    Although the engine now validates declaration rules at construction, the
    adapter still rejects any stable name that is not a catalog-shaped
    identifier before interpolating it into the CREATE VIEW statement, and the
    constant message never echoes the rejected fragment.
    """
    with pytest.raises(ValueError, match="invalid stable name") as caught:
        dbmod._validate_stable_name(fragment)
    assert fragment not in str(caught.value)

    # A catalog-shaped name is accepted unchanged.
    dbmod._validate_stable_name("facts")


def test_missing_extension_is_dependency_error(
    monkeypatch,
    tmp_path: Path,
    database_layer_factory: Callable[[Path, Path], SemanticLayer],
) -> None:
    """A missing DuckDB extension surfaces as a sanitized dependency error.

    The ``sqlite_scanner`` extension is force-simulated as unavailable by
    stubbing ``_try_load`` to return ``False``.  Because SQLite sources carry no
    runtime profile, installation is never permitted, so a sanitized
    ``SourceDependencyError`` (code ``extension_unavailable``) is raised.  The
    file location never reaches any error surface.
    """

    sqlite_path = tmp_path / "SECRET_loc" / "reference.sqlite"
    sqlite_path.parent.mkdir(parents=True)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute('create table codes (code text, "value" integer)')
        connection.execute("insert into codes values ('A', 1)")

    monkeypatch.setattr(dbmod, "_try_load", lambda *_a, **_k: False)

    source = ParsedSource(
        name="codes",
        connector=SqliteConfig(str(sqlite_path), "codes"),
        schema=_codes_schema(),
        grain=("code",),
    )
    adapter = SqliteAdapter()
    with pytest.raises(SourceDependencyError) as caught:
        adapter.prepare(source, _empty_profiles(), _empty_providers())

    assert caught.value.code == "extension_unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_no_secret_leak(caught.value, "SECRET_loc")

    # The public engine preserves the dependency classification rather than
    # collapsing it into generic source initialization failure.
    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute('create table facts (id bigint, "value" bigint)')
    with pytest.raises(SourceDependencyError) as engine_caught:
        QueryEngine(database_layer_factory(sqlite_path, duckdb_path))
    assert engine_caught.value.code == "extension_unavailable"
    assert engine_caught.value.__cause__ is None
    assert engine_caught.value.__context__ is None


# ---------------------------------------------------------------------------
# Offline policy never installs extension
# ---------------------------------------------------------------------------


def test_postgres_extension_install_permission_is_explicit() -> None:
    """The non-secret boolean permission is accepted but never enters the DSN."""

    allowed = MappingProfileResolver(
        {
            "warehouse": {
                "host": "db",
                "dbname": "analytics",
                "allow_extension_install": True,
            }
        }
    ).resolve("warehouse", source_id="facts")
    dbmod._validate_postgres_profile(allowed)
    assert dbmod._extension_allowed(allowed) is True
    assert "allow_extension_install" not in dbmod._build_postgres_dsn(allowed)

    denied = MappingProfileResolver(
        {
            "warehouse": {
                "host": "db",
                "dbname": "analytics",
                "allow_extension_install": "true",
            }
        }
    ).resolve("warehouse", source_id="facts")
    with pytest.raises(ValueError, match="invalid postgres profile value"):
        dbmod._validate_postgres_profile(denied)


def test_offline_policy_never_installs_extension(monkeypatch, tmp_path: Path) -> None:
    """A source with no profile never attempts to INSTALL a missing extension.

    SQLite sources have no runtime profile, so ``allow_extension_install`` is
    always ``False``.  Even when the extension cannot be loaded, ``INSTALL`` is
    never invoked — the adapter raises ``extension_unavailable`` immediately.
    A spy on ``_try_install_and_load`` records any invocation and asserts none
    occurs.
    """

    sqlite_path = tmp_path / "reference.sqlite"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute('create table codes (code text, "value" integer)')

    install_calls: list[str] = []

    def spy_install_and_load(_connection: object, extension: str) -> bool:
        install_calls.append(extension)
        return False

    monkeypatch.setattr(dbmod, "_try_load", lambda *_a, **_k: False)
    monkeypatch.setattr(dbmod, "_try_install_and_load", spy_install_and_load)

    source = ParsedSource(
        name="codes",
        connector=SqliteConfig(str(sqlite_path), "codes"),
        schema=_codes_schema(),
        grain=("code",),
    )
    adapter = SqliteAdapter()
    with pytest.raises(SourceDependencyError) as caught:
        adapter.prepare(source, _empty_profiles(), _empty_providers())

    assert caught.value.code == "extension_unavailable"
    # INSTALL was never attempted because the offline policy forbids it.
    assert install_calls == []


# ---------------------------------------------------------------------------
# Schema mismatch restores old database view
# ---------------------------------------------------------------------------


def test_schema_mismatch_restores_old_database_view(
    tmp_path: Path, monkeypatch
) -> None:
    """A schema drift on reload is rejected; the old view stays queryable.

    A DuckDB file cannot be externally mutated while attached, so the schema
    drift is injected by patching the adapter's ``inspect_schema`` to return a
    drifted schema (an extra ``extra`` column) on the reload candidate.  The
    registry must reject the candidate, leave the generation unchanged, and
    keep the previously registered data queryable.
    """

    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            'create table facts as select 1::bigint id, 10::bigint "value"'
        )

    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        _duckdb_layer(duckdb_path),
        connection,
        _empty_profiles(),
        _empty_providers(),
    )
    try:
        assert registry.status("facts").generation == 1
        assert registry.execute('select sum("value") from "facts"').fetchone() == (10,)

        # Inject a drifted observed schema for the reload candidate only.
        drifted = TableSchema(
            (
                FieldSchema("id", ScalarType("int64"), False),
                FieldSchema("value", ScalarType("int64"), False),
                FieldSchema("extra", ScalarType("int64"), False),
            )
        )
        monkeypatch.setattr(
            DuckDbAdapter, "inspect_schema", lambda self, handle: drifted
        )

        with pytest.raises(SourceSchemaError) as caught:
            registry.reload_source("facts")

        assert caught.value.code == "schema_mismatch"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

        # The failed reload did not swap the registration: the generation is
        # unchanged and the previously registered data is still queryable.
        assert registry.status("facts").generation == 1
        assert registry.execute('select sum("value") from "facts"').fetchone() == (10,)
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# DuckDB attachment is read-only
# ---------------------------------------------------------------------------


def test_duckdb_attachment_is_read_only(tmp_path: Path) -> None:
    """Writes through the stable view are rejected (read-only attachment).

    The adapter always attaches with ``(READ_ONLY)``.  An ``INSERT`` through the
    stable view is rejected by DuckDB, and the original data is preserved.
    """

    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            'create table facts as select 1::bigint id, 10::bigint "value"'
        )

    connection = duckdb.connect(":memory:")
    registry = SourceRegistry.create(
        _duckdb_layer(duckdb_path),
        connection,
        _empty_profiles(),
        _empty_providers(),
    )
    try:
        assert registry.execute('select sum("value") from "facts"').fetchone() == (10,)
        # Writes through the stable view are rejected.
        with pytest.raises(duckdb.Error):
            registry.execute('insert into "facts" values (2, 20)')
        # Original data unchanged.
        assert registry.execute('select sum("value") from "facts"').fetchone() == (10,)
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# Attachment closes on engine close
# ---------------------------------------------------------------------------


def test_attachment_closes_on_engine_close(tmp_path: Path) -> None:
    """After the engine closes, the file handle is released and writable.

    The adapter's ``close`` detaches the alias from the shared connection so
    DuckDB releases the file handle.  A fresh read-write connection succeeds
    after the engine exits, proving no lingering attachment holds the file.
    """

    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute(
            'create table facts as select 1::bigint id, 10::bigint "value"'
        )

    layer = _duckdb_layer(duckdb_path)
    engine = QueryEngine(layer)
    assert engine.query(["total_value"])["total_value"].item() == 10
    engine.close()

    # After close, the file handle is released and a fresh read-write
    # connection succeeds — proving no lingering attachment holds the file.
    with duckdb.connect(str(duckdb_path)) as writer:
        writer.execute("insert into facts values (2, 20)")
        assert writer.execute("select count(*) from facts").fetchone() == (2,)


# ---------------------------------------------------------------------------
# Database errors hide location and DSN
# ---------------------------------------------------------------------------


def test_database_errors_hide_location_and_dsn(tmp_path: Path) -> None:
    """A nonexistent file location and an unreachable DSN never surface in errors.

    The DuckDB case uses a path containing a secret token; the PostgreSQL case
    uses an unreachable port with a secret password.  In both cases the driver
    exception (which echoes the location/DSN) is discarded at the registry
    boundary and only a constant, sanitized ``SourceConnectionError`` surfaces.
    """

    # --- DuckDB: nonexistent file with a secret in the path ---
    secret_path = str(tmp_path / "SECRET_loc_42" / "missing.duckdb")
    with pytest.raises(SourceConnectionError) as duckdb_caught:
        QueryEngine(_duckdb_layer(secret_path))
    assert duckdb_caught.value.code == "source_initialization_failed"
    _assert_no_secret_leak(duckdb_caught.value, "SECRET_loc_42")

    # --- PostgreSQL: unreachable port with secret credentials ---
    secret_user = "SECRET_PG_USER"
    secret_pw = "SECRET_PG_PASSWORD"
    profiles = MappingProfileResolver(
        {
            "warehouse": {
                "host": "127.0.0.1",
                "port": "1",
                "user": secret_user,
                "password": secret_pw,
                "dbname": "test",
            }
        }
    )
    with pytest.raises(SourceConnectionError) as pg_caught:
        QueryEngine(_postgres_layer("warehouse", "public.facts"), profiles=profiles)
    assert pg_caught.value.code == "source_initialization_failed"
    _assert_no_secret_leak(pg_caught.value, secret_user, secret_pw)
