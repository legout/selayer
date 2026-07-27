from __future__ import annotations

from dataclasses import replace

import pytest

from selayer._next.model import SemanticLayer
from selayer.compilation import (
    compile_duckdb,
    compile_metric_expression,
    compile_row_expression,
    quote_identifier,
)
from selayer.expressions import parse_expression
from selayer.expressions.ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
)
from selayer.planning import (
    ListFilter,
    PlannedFilter,
    QueryRequest,
    RangeFilter,
    ScalarFilter,
    plan_query,
)
from tests.next.conftest import VALID_CATALOG_YAML


@pytest.fixture
def item_margin_plan(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "layer.yaml"
    path.write_text(VALID_CATALOG_YAML, encoding="utf-8")
    layer = SemanticLayer.load(path)
    return plan_query(
        layer,
        QueryRequest(metrics=("gross_margin",), dimensions=("product_category",)),
    )


def test_compiles_metrics_outside_aggregate_cte(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    compiled = compile_duckdb(item_margin_plan)
    assert compiled.sql.startswith("WITH aggregated AS (")
    assert 'SUM("order_items"."total") AS "total_item_revenue"' in compiled.sql
    assert 'AS "gross_margin"' in compiled.sql.split(") SELECT", maxsplit=1)[1]


def test_quotes_identifier_and_escapes_quotes() -> None:
    assert quote_identifier('odd"name') == '"odd""name"'


def test_compiles_every_operator() -> None:
    row = Reference(("src", "value"))
    expressions: dict[str, Expression] = {
        "+": UnaryOperation("+", row),
        "-": UnaryOperation("-", row),
        "not": UnaryOperation("not", row),
    }
    for operator in ("+", "-", "*", "/", "=", "!=", "<", "<=", ">", ">="):
        expressions[operator] = BinaryOperation(operator, row, Literal(1))
    for expression in expressions.values():
        assert '"src"."value"' in compile_row_expression(expression)


def test_compiles_all_allowlisted_row_and_metric_functions() -> None:
    row = Reference(("src", "value"))
    for name in ("abs", "coalesce", "if", "lower", "nullif", "upper"):
        args = (row, Literal(0)) if name != "if" else (Literal(True), row, Literal(0))
        assert compile_row_expression(FunctionCall(name, args)).startswith(name.upper())
    for name in ("abs", "coalesce", "nullif"):
        args = (Reference(("measure",)), Literal(0))
        assert compile_metric_expression(FunctionCall(name, args)).startswith(
            name.upper()
        )


def test_compiles_every_aggregation(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    expected = {
        "sum": "SUM(",
        "avg": "AVG(",
        "min": "MIN(",
        "max": "MAX(",
        "count": "COUNT(",
        "count_distinct": "COUNT(DISTINCT ",
    }
    for aggregation, prefix in expected.items():
        measure = replace(item_margin_plan.measures[0], aggregation=aggregation)
        plan = replace(item_margin_plan, measures=(measure,))
        assert prefix in compile_duckdb(plan).sql


def test_filter_shapes_and_parameter_order(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    filters = (
        PlannedFilter(
            "product_category", dimension, ScalarFilter("x'; DROP TABLE t;--")
        ),
        PlannedFilter("product_category", dimension, ListFilter(("a", "b"))),
        PlannedFilter("product_category", dimension, RangeFilter(1, 2)),
        PlannedFilter("product_category", dimension, ListFilter(())),
    )
    compiled = compile_duckdb(replace(item_margin_plan, filters=filters))
    assert compiled.parameters == ("x'; DROP TABLE t;--", "a", "b", 1, 2)
    assert "x'; DROP TABLE t;--" not in compiled.sql
    assert "FALSE" in compiled.sql


def test_stable_dimension_and_metric_output_order(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    first = item_margin_plan.metrics[0]
    second = replace(first, id="revenue_ratio")
    plan = replace(item_margin_plan, metrics=(second, first))
    sql = compile_duckdb(plan).sql
    projection = sql.split(") SELECT ", maxsplit=1)[1].split(" FROM ", maxsplit=1)[0]
    assert projection.index('"product_category"') < projection.index('"revenue_ratio"')
    assert projection.index('"revenue_ratio"') < projection.index('"gross_margin"')


def test_shared_join_is_rendered_once(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    sql = compile_duckdb(item_margin_plan).sql
    assert sql.count('JOIN "products"') == 1


def test_parser_ast_literals_compile_without_raw_sql() -> None:
    assert compile_row_expression(parse_expression("lower(order_items.status)")) == (
        'LOWER("order_items"."status")'
    )
    assert compile_metric_expression(parse_expression("coalesce(total, 0)")) == (
        'COALESCE("total", 0)'
    )
