from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import duckdb
import pytest

import selayer
from selayer import (
    CatalogValidationError,
    DataSource,
    Dimension,
    QueryEngine,
    QueryExecutionError,
    SemanticLayer,
)
from selayer.sources.config import PyArrowConfig
from selayer.sources.errors import SourceConnectionError
from selayer.sources.profiles import MappingArrowProviderResolver
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema


def test_query_engine_exposes_resolved_plan(valid_catalog_path: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    plan = engine.plan(["gross_margin"], ["product_category"])
    assert plan.anchor_source == "order_items"


def test_query_engine_executes_compiled_query(valid_catalog_path: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    result = engine.query(["gross_margin"], ["product_category"])
    assert result.columns == ["product_category", "gross_margin"]


def test_query_engine_binds_filters(valid_catalog_path: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    unfiltered = engine.query(["gross_margin"], ["product_category"])
    result = engine.query(
        ["gross_margin"], ["product_category"], {"product_category": "Books"}
    )
    assert result.columns == ["product_category", "gross_margin"]
    assert result["product_category"].unique().to_list() == ["Books"]
    assert result.height < unfiltered.height


def test_query_engine_normalizes_mutable_inputs(valid_catalog_path: Path) -> None:
    metrics = ["gross_margin"]
    dimensions = ["product_category"]
    values = ["Books"]
    filters: dict[str, object] = {"product_category": values}
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    plan = engine.plan(metrics, dimensions, filters)
    metrics.append("changed")
    dimensions.append("changed")
    values.append("changed")
    assert tuple(item.id for item in plan.metrics) == ("gross_margin",)
    assert tuple(item.id for item in plan.dimensions) == ("product_category",)


def test_query_engine_rejects_invalid_direct_layer_before_duckdb(
    valid_layer: SemanticLayer,
) -> None:
    """The engine validates a programmatic layer before opening any resource."""
    source = valid_layer.data_sources["orders"]
    bad = replace(
        valid_layer,
        data_sources={
            **valid_layer.data_sources,
            "orders": replace(source, grain=("id", "id")),
        },
    )
    with (
        patch("selayer.query.duckdb.connect") as connect,
        pytest.raises(CatalogValidationError) as raised,
    ):
        QueryEngine(bad)
    connect.assert_not_called()
    assert raised.value.issues[0].code == "catalog.grain.duplicate_column"


def test_query_execution_error_does_not_leak_bound_values(
    valid_catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An execution error never leaks the driver diagnostic or bound value.

    After the planner admits only correctly typed values, the execution
    boundary is exercised through a failing connection whose diagnostic
    carries the secret; the parameterized bound value is a benign integer.
    """
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)
    secret = "UNIQUE_SECRET_BOUND_VALUE"

    class FailingConnection:
        def __init__(self) -> None:
            self.sql: str | None = None

        def execute(self, sql: str, _parameters: tuple[object, ...]) -> object:
            self.sql = sql
            raise duckdb.ConversionException(f"Conversion Error: bound {secret}")

    monkeypatch.setattr(engine._registry, "_connection", FailingConnection())
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"], filters={"stock": 1})
    error = caught.value
    formatted = "".join(__import__("traceback").format_exception(error))
    assert UUID(error.query_id).version == 4
    assert secret not in str(error)
    assert secret not in error.message
    assert secret not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in formatted


@pytest.mark.parametrize(
    "diagnostic",
    [
        "prefix Conversion Error: leaked",
        "Conversion ErrorSuffix: leaked",
    ],
)
def test_diagnostic_category_requires_an_anchored_token(
    valid_catalog_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)

    class FailingConnection:
        def __init__(self) -> None:
            self.sql: str | None = None

        def execute(self, sql: str, _parameters: tuple[object, ...]) -> object:
            self.sql = sql
            raise duckdb.ConversionException(diagnostic)

    connection = FailingConnection()
    monkeypatch.setattr(engine._registry, "_connection", connection)
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"], filters={"stock": 1})
    error = caught.value
    assert connection.sql is not None
    formatted = "".join(__import__("traceback").format_exception(error))
    for leaked in (connection.sql, diagnostic):
        assert leaked not in str(error)
        assert leaked not in error.message
        assert leaked not in repr(error.args)
        assert leaked not in formatted
    assert error.message == (
        "query execution failed: parameterized query failed (DuckDB Error)"
    )


def test_parameterized_query_errors_expose_only_allowlisted_diagnostic_category(
    valid_catalog_path: Path,
) -> None:
    """A reachable real-DuckDB error exposes only the allowlisted category.

    A 128-bit integer overflow is the one out-of-range scenario that survives
    the planner's type check (the value is a valid ``int``), so it still reaches
    DuckDB and exercises the execution-boundary sanitizer against a real driver
    diagnostic. The driver context and the bound value never reach the message.
    """
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)
    value = 10**100
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"], filters={"stock": value})
    error = caught.value
    formatted = "".join(__import__("traceback").format_exception(error))
    assert "Invalid Input Error" in error.message
    assert "parameterized query" in error.message
    assert "SQL:" not in error.message
    assert "128-bit" not in error.message
    assert "128-bit" not in formatted
    for printable in (str(value), repr(value)):
        assert printable not in str(error)
        assert printable not in repr(error.args)
        assert printable not in error.message
        assert printable not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.message in formatted


def test_parameterized_errors_reach_execution_with_immutable_parameters_and_sanitize(
    valid_catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)

    raw_diagnostic = "Conversion Error: raw SQL and bound secret must not escape"

    class FailingConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, parameters: tuple[object, ...]) -> object:
            self.calls.append((sql, parameters))
            raise duckdb.ConversionException(raw_diagnostic)

    connection = FailingConnection()
    monkeypatch.setattr(engine._registry, "_connection", connection)
    # Correctly typed integer-family values exercise the scalar/list/range
    # shapes the planner now admits; each is frozen before reaching execution.
    secret = 4242424242
    cases: tuple[tuple[object, tuple[object, ...], tuple[str, ...]], ...] = (
        (secret, (secret,), (str(secret),)),
        ([secret], (secret,), (str(secret), f"[{secret}]", f"({secret},)")),
        (
            (secret, secret),
            (secret, secret),
            (str(secret), f"({secret}, {secret})"),
        ),
    )
    for value, expected_parameters, leak_sentinels in cases:
        with pytest.raises(QueryExecutionError) as caught:
            engine.query(["gross_margin"], filters={"stock": value})
        error = caught.value
        formatted = "".join(__import__("traceback").format_exception(error))
        sql, parameters = connection.calls[-1]
        assert parameters == expected_parameters
        assert sql.startswith('WITH "aggregated"')
        for leaked in (sql, raw_diagnostic, *leak_sentinels):
            assert leaked not in str(error)
            assert leaked not in error.message
            assert leaked not in repr(error.args)
            assert leaked not in formatted
        assert "SQL:" not in error.message
        assert error.__cause__ is None
        assert error.__context__ is None


def test_parameterized_errors_never_format_values_or_driver_messages(
    valid_catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)

    raw_diagnostic = "Conversion Error: leaked raw diagnostic and <redacted> marker"

    class FailingConnection:
        def __init__(self) -> None:
            self.sql: str | None = None

        def execute(self, sql: str, _parameters: tuple[object, ...]) -> object:
            self.sql = sql
            raise duckdb.ConversionException(raw_diagnostic)

    connection = FailingConnection()
    monkeypatch.setattr(engine._registry, "_connection", connection)
    secret = 4242424242
    secret_values: tuple[tuple[object, tuple[str, ...]], ...] = (
        (secret, (str(secret),)),
        ([secret], (str(secret), f"[{secret}]", f"({secret},)")),
        ((secret, secret), (str(secret), f"({secret}, {secret})")),
    )
    for secret_value, leak_sentinels in secret_values:
        with pytest.raises(QueryExecutionError) as caught:
            engine.query(["gross_margin"], filters={"stock": secret_value})
        error = caught.value
        assert connection.sql is not None
        formatted = "".join(__import__("traceback").format_exception(error))
        for leaked in (connection.sql, raw_diagnostic, *leak_sentinels):
            assert leaked not in str(error)
            assert leaked not in error.message
            assert leaked not in repr(error.args)
            assert leaked not in formatted
        assert "Conversion Error" in error.message
        assert "parameterized query" in error.message
        assert "SQL:" not in error.message
        assert error.__cause__ is None
        assert error.__context__ is None


def test_parameterized_errors_use_generic_category_for_unknown_driver_error(
    valid_catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    layer = replace(
        layer,
        dimensions={
            **layer.dimensions,
            "stock": Dimension("stock", "products", "in_stock", "integer"),
        },
    )
    engine = QueryEngine(layer)

    raw_diagnostic = "Parser Error: raw details must not escape"

    class FailingConnection:
        def __init__(self) -> None:
            self.sql: str | None = None

        def execute(self, sql: str, _parameters: tuple[object, ...]) -> object:
            self.sql = sql
            raise duckdb.Error(raw_diagnostic)

    connection = FailingConnection()
    monkeypatch.setattr(engine._registry, "_connection", connection)
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"], filters={"stock": 1})
    error = caught.value
    assert connection.sql is not None
    formatted = "".join(__import__("traceback").format_exception(error))
    for leaked in (connection.sql, raw_diagnostic):
        assert leaked not in str(error)
        assert leaked not in error.message
        assert leaked not in repr(error.args)
        assert leaked not in formatted
    assert error.message == (
        "query execution failed: parameterized query failed (DuckDB Error)"
    )


def test_query_execution_errors_have_distinct_uuid_ids(
    valid_catalog_path: Path,
) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    engine.close()
    ids = []
    for _ in range(2):
        with pytest.raises(QueryExecutionError) as caught:
            engine.query(["gross_margin"])
        ids.append(UUID(caught.value.query_id))
    assert ids[0].version == ids[1].version == 4
    assert ids[0] != ids[1]


def test_source_loading_error_is_sanitized_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing pyarrow provider surfaces a sanitized, closed connection error."""

    class TrackingConnection:
        closed = False

        def register(self, *_args: object) -> None:
            raise AssertionError("source loader should fail before registration")

        def close(self) -> None:
            self.closed = True

    connection = TrackingConnection()
    monkeypatch.setattr("selayer.query.duckdb.connect", lambda *_args: connection)
    credential_path = "https://user:password@private.example/data.parquet"

    def failing_provider() -> object:
        raise OSError(f"cannot read {credential_path}")

    providers = MappingArrowProviderResolver({"events": failing_provider})
    layer = SemanticLayer(
        1,
        "test",
        "",
        "",
        {
            "events": DataSource(
                name="events",
                connector=PyArrowConfig("events"),
                schema=TableSchema((FieldSchema("id", ScalarType("int64"), False),)),
                grain=("id",),
            )
        },
        {},
        {},
        {},
        {},
        {},
    )
    with pytest.raises(SourceConnectionError) as caught:
        QueryEngine(layer, arrow_providers=providers)
    error = caught.value
    assert connection.closed
    assert credential_path not in str(error)
    assert credential_path not in error.message
    assert credential_path not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.code == "source_initialization_failed"
    assert error.source_id == "events"


def test_query_engine_context_manager_closes_connection(
    valid_catalog_path: Path,
) -> None:
    with QueryEngine(SemanticLayer.load(valid_catalog_path)) as engine:
        connection = engine._registry._connection
    with pytest.raises(duckdb.Error):
        connection.execute("SELECT 1")


def test_query_engine_delegates_source_reload(
    valid_layer: SemanticLayer,
    arrow_providers: MappingArrowProviderResolver,
) -> None:
    """The engine delegates reload_source and reports the generation change."""
    with QueryEngine(valid_layer, arrow_providers=arrow_providers) as engine:
        before = engine.source_status("order_items")
        result = engine.reload_source("order_items")
        after = engine.source_status("order_items")

    assert result.old_generation == before.generation
    assert result.new_generation == after.generation
    assert after.generation == before.generation + 1


def test_reload_all_is_exposed_as_immutable_results(
    valid_layer: SemanticLayer,
    arrow_providers: MappingArrowProviderResolver,
) -> None:
    """reload_all returns immutable results for every source in sorted order."""
    with QueryEngine(valid_layer, arrow_providers=arrow_providers) as engine:
        results = engine.reload_all()

    assert tuple(item.source_id for item in results) == tuple(
        sorted(valid_layer.data_sources)
    )
    for item in results:
        assert item.new_generation == 2


def test_query_engine_never_uses_eager_polars_source_reads(
    valid_layer: SemanticLayer,
    arrow_providers: MappingArrowProviderResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful query proves neither eager Polars reader is invoked."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("query engine must not eagerly read sources via Polars")

    import polars as pl

    monkeypatch.setattr(pl, "read_parquet", fail)
    monkeypatch.setattr(pl, "read_csv", fail)

    with QueryEngine(valid_layer, arrow_providers=arrow_providers) as engine:
        result = engine.query(["gross_margin"], ["product_category"])

    assert "gross_margin" in result.columns


def test_public_exports_exclude_compiler_internals() -> None:
    expected = {
        "Aggregation",
        "Cardinality",
        "CatalogIssue",
        "CatalogValidationError",
        "DataSource",
        "Dimension",
        "Fact",
        "FieldSchema",
        "Measure",
        "Metric",
        "OkfBundle",
        "QueryEngine",
        "QueryExecutionError",
        "QueryPlan",
        "QueryPlanningError",
        "Relationship",
        "ReloadResult",
        "SemanticLayer",
        "SemanticStatus",
        "SourceConnectionError",
        "SourceDependencyError",
        "SourceError",
        "SourceProfileError",
        "SourceReloadError",
        "SourceSchemaError",
        "SourceStatus",
        "TableSchema",
    }
    assert set(selayer.__all__) == expected
    # The registry, adapter classes, raw handles, profiles, and schema-parser
    # internals are deliberately private to the sources package and never
    # exported from the package root.
    assert not hasattr(selayer, "compile_duckdb")
    assert not hasattr(selayer, "parse_expression")
    assert not hasattr(selayer, "SourceRegistry")
    assert not hasattr(selayer, "parse_schema_document")


def test_next_package_is_absent() -> None:
    assert not (Path(selayer.__file__).parent / "_next").exists()


def test_unparameterized_driver_error_hides_authenticated_location(
    valid_catalog_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s3://access:password@example.invalid/data"
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))

    class FailingConnection:
        def execute(self, _sql: str, _parameters: tuple[object, ...]) -> object:
            raise duckdb.Error(f"Catalog Error: failed to scan {secret}")

    monkeypatch.setattr(engine._registry, "_connection", FailingConnection())
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"])

    assert secret not in caught.value.message
    assert "Catalog Error" in caught.value.message
    assert "SQL:" not in caught.value.message


def test_query_execution_error_sanitizes_unparameterized_driver_details(
    valid_catalog_path: Path,
) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    engine.close()
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"])
    assert caught.value.message == "query execution failed: DuckDB Error"
    assert "Connection already closed" not in caught.value.message
    assert "SQL:" not in caught.value.message
    assert caught.value.query_id in str(caught.value)
