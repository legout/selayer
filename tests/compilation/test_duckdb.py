from __future__ import annotations

from dataclasses import replace

import pytest

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
from selayer.model import SemanticLayer
from selayer.planning import (
    ListFilter,
    PlannedFilter,
    QueryRequest,
    RangeFilter,
    ScalarFilter,
    plan_query,
)
from tests.conftest import VALID_CATALOG_YAML


@pytest.fixture
def item_margin_plan(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "layer.yaml"
    path.write_text(VALID_CATALOG_YAML, encoding="utf-8")
    layer = SemanticLayer.load(path)
    return plan_query(
        layer,
        QueryRequest(metrics=("gross_margin",), dimensions=("product_category",)),
    )


@pytest.fixture
def two_join_plan(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "layer.yaml"
    path.write_text(
        VALID_CATALOG_YAML
        + """
  order_item_order:
    source: orders
    target: order_items
    type: one_to_one
    source_column: id
    target_column: order_id
""",
        encoding="utf-8",
    )
    layer = SemanticLayer.load(path)
    return plan_query(
        layer,
        QueryRequest(
            metrics=("gross_margin",),
            dimensions=("product_category", "order_date"),
        ),
    )


def test_compiles_metrics_outside_aggregate_cte(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    compiled = compile_duckdb(item_margin_plan)
    assert compiled.sql.startswith('WITH "aggregated" AS (')
    assert (
        'SUM("order_items"."total") AS "__selayer_measure_total_item_revenue"'
        in compiled.sql
    )
    assert 'AS "gross_margin"' in compiled.sql.split(") SELECT", maxsplit=1)[1]
    assert 'FROM "aggregated"' in compiled.sql


def test_quotes_identifier_and_escapes_quotes() -> None:
    assert quote_identifier('odd"name') == '"odd""name"'


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("+", '(+"src"."value")'),
        ("-", '(-"src"."value")'),
        ("not", '(NOT "src"."value")'),
    ],
)
def test_compiles_every_unary_operator(operator: str, expected: str) -> None:
    expression = UnaryOperation(operator, Reference(("src", "value")))
    assert compile_row_expression(expression) == expected


@pytest.mark.parametrize(
    "operator", ("+", "-", "*", "/", "=", "!=", "<", "<=", ">", ">=")
)
def test_compiles_every_binary_operator_exactly(operator: str) -> None:
    expression = BinaryOperation(operator, Reference(("src", "left")), Literal(1))
    assert compile_row_expression(expression) == f'("src"."left" {operator} 1)'


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("abs", (Reference(("src", "value")),), 'ABS("src"."value")'),
        (
            "coalesce",
            (Reference(("src", "value")), Literal(0)),
            'COALESCE("src"."value", 0)',
        ),
        (
            "if",
            (Literal(True), Reference(("src", "value")), Literal(0)),
            'IF(TRUE, "src"."value", 0)',
        ),
        ("lower", (Reference(("src", "value")),), 'LOWER("src"."value")'),
        (
            "nullif",
            (Reference(("src", "value")), Literal(0)),
            'NULLIF("src"."value", 0)',
        ),
        ("upper", (Reference(("src", "value")),), 'UPPER("src"."value")'),
    ],
)
def test_compiles_every_allowlisted_row_function_exactly(
    name: str, arguments: tuple[Expression, ...], expected: str
) -> None:
    assert compile_row_expression(FunctionCall(name, arguments)) == expected


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("abs", (Reference(("measure",)),), 'ABS("measure")'),
        (
            "coalesce",
            (Reference(("measure",)), Literal(0)),
            'COALESCE("measure", 0)',
        ),
        (
            "nullif",
            (Reference(("measure",)), Literal(0)),
            'NULLIF("measure", 0)',
        ),
    ],
)
def test_compiles_every_allowlisted_metric_function_exactly(
    name: str, arguments: tuple[Expression, ...], expected: str
) -> None:
    assert compile_metric_expression(FunctionCall(name, arguments)) == expected


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
        plan = replace(
            item_margin_plan,
            measures=(measure, *item_margin_plan.measures[1:]),
        )
        assert prefix in compile_duckdb(plan).sql


def _where_sql(compiled_sql: str) -> str:
    return compiled_sql.split(" WHERE ", maxsplit=1)[1].split(" GROUP BY ", maxsplit=1)[
        0
    ]


def test_scalar_filter_shape_and_binding(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    compiled = compile_duckdb(
        replace(
            item_margin_plan,
            filters=(PlannedFilter("product_category", dimension, ScalarFilter("x")),),
        )
    )
    assert _where_sql(compiled.sql) == '"products"."category" = ?'
    assert compiled.parameters == ("x",)


def test_range_filter_shape_and_binding(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    compiled = compile_duckdb(
        replace(
            item_margin_plan,
            filters=(PlannedFilter("product_category", dimension, RangeFilter(1, 2)),),
        )
    )
    assert _where_sql(compiled.sql) == '"products"."category" BETWEEN ? AND ?'
    assert compiled.parameters == (1, 2)


def test_non_empty_list_filter_shape_and_binding(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    compiled = compile_duckdb(
        replace(
            item_margin_plan,
            filters=(
                PlannedFilter("product_category", dimension, ListFilter(("a", "b"))),
            ),
        )
    )
    assert _where_sql(compiled.sql) == '"products"."category" IN (?, ?)'
    assert compiled.parameters == ("a", "b")


def test_empty_list_filter_is_false_without_parameters(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    compiled = compile_duckdb(
        replace(
            item_margin_plan,
            filters=(PlannedFilter("product_category", dimension, ListFilter(())),),
        )
    )
    assert _where_sql(compiled.sql) == "FALSE"
    assert compiled.parameters == ()


def test_multiple_filters_preserve_sql_occurrence_order_and_bind_malicious_values(
    item_margin_plan,
) -> None:  # type: ignore[no-untyped-def]
    dimension = item_margin_plan.dimensions[0].dimension
    malicious = "x'; DROP TABLE t;--"
    filters = (
        PlannedFilter("first", dimension, ScalarFilter(malicious)),
        PlannedFilter("second", dimension, ListFilter(("a", "b"))),
        PlannedFilter("third", dimension, RangeFilter(1, 2)),
        PlannedFilter("fourth", dimension, ListFilter(())),
    )
    compiled = compile_duckdb(replace(item_margin_plan, filters=filters))
    assert _where_sql(compiled.sql) == (
        '"products"."category" = ? AND "products"."category" IN (?, ?) '
        'AND "products"."category" BETWEEN ? AND ? AND FALSE'
    )
    assert compiled.parameters == (malicious, "a", "b", 1, 2)
    assert malicious not in compiled.sql


def test_aggregate_cte_preserves_dimension_and_measure_order(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    first_dimension = item_margin_plan.dimensions[0]
    second_dimension = replace(first_dimension, id="product_category_again")
    plan = replace(
        item_margin_plan,
        dimensions=(first_dimension, second_dimension),
    )
    sql = compile_duckdb(plan).sql
    cte = sql.split(") SELECT", maxsplit=1)[0]
    assert cte.index('AS "__selayer_dimension_product_category"') < cte.index(
        'AS "__selayer_dimension_product_category_again"'
    )
    assert cte.index('AS "__selayer_dimension_product_category_again"') < cte.index(
        'AS "__selayer_measure_total_item_revenue"'
    )
    assert cte.index('AS "__selayer_measure_total_item_revenue"') < cte.index(
        'AS "__selayer_measure_total_item_cost"'
    )


def test_cross_kind_names_use_distinct_internal_aggregate_aliases(
    item_margin_plan,
) -> None:  # type: ignore[no-untyped-def]
    colliding_dimension = replace(
        item_margin_plan.dimensions[0], id="total_item_revenue"
    )
    plan = replace(item_margin_plan, dimensions=(colliding_dimension,))

    sql = compile_duckdb(plan).sql
    aggregate_projection, outer_projection = sql.split(") SELECT", maxsplit=1)

    assert 'AS "__selayer_dimension_total_item_revenue"' in aggregate_projection
    assert 'AS "__selayer_measure_total_item_revenue"' in aggregate_projection
    assert (
        '"__selayer_dimension_total_item_revenue" AS "total_item_revenue"'
        in outer_projection
    )
    assert '"__selayer_measure_total_item_revenue"' in outer_projection


def test_stable_dimension_and_metric_output_order(item_margin_plan) -> None:  # type: ignore[no-untyped-def]
    first = item_margin_plan.metrics[0]
    second = replace(first, id="revenue_ratio")
    plan = replace(item_margin_plan, metrics=(second, first))
    sql = compile_duckdb(plan).sql
    projection = sql.split(") SELECT ", maxsplit=1)[1].split(" FROM ", maxsplit=1)[0]
    assert projection == (
        '"__selayer_dimension_product_category" AS "product_category", '
        '(("__selayer_measure_total_item_revenue" - '
        '"__selayer_measure_total_item_cost") / '
        'NULLIF("__selayer_measure_total_item_revenue", 0)) AS "revenue_ratio", '
        '(("__selayer_measure_total_item_revenue" - '
        '"__selayer_measure_total_item_cost") / '
        'NULLIF("__selayer_measure_total_item_revenue", 0)) AS "gross_margin"'
    )


def test_stable_two_join_order(two_join_plan) -> None:  # type: ignore[no-untyped-def]
    sql = compile_duckdb(two_join_plan).sql
    assert (
        'FROM "order_items" JOIN "products" ON '
        '"products"."id" = "order_items"."product_id" JOIN "orders" ON '
        '"orders"."id" = "order_items"."order_id"'
    ) in sql


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
