from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from selayer.compilation.duckdb import (
    CompiledQuery,
    compile_duckdb,
    compile_metric_expression,
    compile_row_expression,
    quote_identifier,
)
from selayer.expressions import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    parse_expression,
)
from selayer.model import Aggregation
from selayer.planning import (
    JoinStep,
    ListFilter,
    PlannedDimension,
    PlannedFilter,
    PlannedMeasure,
    PlannedMetric,
    QueryPlan,
    RangeFilter,
    ScalarFilter,
)


@pytest.fixture
def item_margin_plan() -> QueryPlan:
    return QueryPlan(
        anchor_source="order_items",
        joins=(
            JoinStep(
                relationship_id="product_order_items",
                source="products",
                target="order_items",
                source_column="id",
                target_column="product_id",
            ),
        ),
        dimensions=(
            PlannedDimension(
                id="product_category",
                source="products",
                column="category",
                data_type="string",
            ),
        ),
        measures=(
            PlannedMeasure(
                id="total_item_revenue",
                expression=parse_expression("order_items.total"),
                aggregation="sum",
            ),
            PlannedMeasure(
                id="total_item_cost",
                expression=parse_expression("order_items.quantity * products.cost"),
                aggregation="sum",
            ),
        ),
        metrics=(
            PlannedMetric(
                id="gross_margin",
                expression=parse_expression(
                    "(total_item_revenue - total_item_cost) "
                    "/ nullif(total_item_revenue, 0)"
                ),
            ),
        ),
        filters=(),
    )


def _single_measure_plan(aggregation: Aggregation) -> QueryPlan:
    return QueryPlan(
        anchor_source="events",
        joins=(),
        dimensions=(),
        measures=(
            PlannedMeasure(
                id="amount_measure",
                expression=parse_expression("events.amount"),
                aggregation=aggregation,
            ),
        ),
        metrics=(PlannedMetric("result", parse_expression("amount_measure")),),
        filters=(),
    )


def test_compiles_metrics_outside_aggregate_cte(item_margin_plan: QueryPlan) -> None:
    compiled = compile_duckdb(item_margin_plan)

    assert compiled.sql.startswith("WITH aggregated AS (")
    assert (
        'SUM("order_items"."total") AS "__selayer_measure_total_item_revenue"'
        in compiled.sql
    )
    assert 'AS "gross_margin"' in compiled.sql.split(") SELECT", maxsplit=1)[1]
    assert compiled.parameters == ()


def test_quotes_every_identifier_and_escapes_embedded_quotes() -> None:
    assert quote_identifier('order"items') == '"order""items"'
    assert compile_row_expression(Reference(('odd"source', 'odd"column'))) == (
        '"odd""source"."odd""column"'
    )
    assert compile_metric_expression(Reference(('odd"measure',))) == '"odd""measure"'


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("orders.a + orders.b", '("orders"."a" + "orders"."b")'),
        ("orders.a - orders.b", '("orders"."a" - "orders"."b")'),
        ("orders.a * orders.b", '("orders"."a" * "orders"."b")'),
        ("orders.a / orders.b", '("orders"."a" / "orders"."b")'),
        ("orders.a = orders.b", '("orders"."a" = "orders"."b")'),
        ("orders.a != orders.b", '("orders"."a" != "orders"."b")'),
        ("orders.a < orders.b", '("orders"."a" < "orders"."b")'),
        ("orders.a <= orders.b", '("orders"."a" <= "orders"."b")'),
        ("orders.a > orders.b", '("orders"."a" > "orders"."b")'),
        ("orders.a >= orders.b", '("orders"."a" >= "orders"."b")'),
        ("+orders.a", '(+"orders"."a")'),
        ("-orders.a", '(-"orders"."a")'),
        ("not orders.a", '(NOT "orders"."a")'),
    ],
)
def test_compiles_every_row_operator(source: str, expected: str) -> None:
    assert compile_row_expression(parse_expression(source)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a + b", '("a" + "b")'),
        ("a - b", '("a" - "b")'),
        ("a * b", '("a" * "b")'),
        ("a / b", '("a" / "b")'),
        ("a = b", '("a" = "b")'),
        ("a != b", '("a" != "b")'),
        ("a < b", '("a" < "b")'),
        ("a <= b", '("a" <= "b")'),
        ("a > b", '("a" > "b")'),
        ("a >= b", '("a" >= "b")'),
        ("+a", '(+"a")'),
        ("-a", '(-"a")'),
        ("not a", '(NOT "a")'),
    ],
)
def test_compiles_every_metric_operator(source: str, expected: str) -> None:
    assert compile_metric_expression(parse_expression(source)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("abs(orders.amount)", 'ABS("orders"."amount")'),
        ("coalesce(orders.amount, 0)", 'COALESCE("orders"."amount", 0)'),
        (
            "if(orders.active, orders.amount, 0)",
            'IF("orders"."active", "orders"."amount", 0)',
        ),
        ("lower(orders.name)", 'LOWER("orders"."name")'),
        ("nullif(orders.amount, 0)", 'NULLIF("orders"."amount", 0)'),
        ("upper(orders.name)", 'UPPER("orders"."name")'),
    ],
)
def test_compiles_every_allowlisted_row_function(source: str, expected: str) -> None:
    assert compile_row_expression(parse_expression(source)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("abs(total)", 'ABS("total")'),
        ("coalesce(total, 0)", 'COALESCE("total", 0)'),
        ("nullif(total, 0)", 'NULLIF("total", 0)'),
    ],
)
def test_compiles_every_allowlisted_metric_function(source: str, expected: str) -> None:
    assert compile_metric_expression(parse_expression(source)) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (Literal(None), "NULL"),
        (Literal(False), "FALSE"),
        (Literal(True), "TRUE"),
        (Literal(12), "12"),
        (Literal(1.25), "1.25"),
        (Literal("O'Reilly"), "'O''Reilly'"),
    ],
)
def test_compiles_every_literal(expression: Literal, expected: str) -> None:
    assert compile_row_expression(expression) == expected
    assert compile_metric_expression(expression) == expected


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [
        ("sum", 'SUM("events"."amount")'),
        ("avg", 'AVG("events"."amount")'),
        ("min", 'MIN("events"."amount")'),
        ("max", 'MAX("events"."amount")'),
        ("count", 'COUNT("events"."amount")'),
        ("count_distinct", 'COUNT(DISTINCT "events"."amount")'),
    ],
)
def test_compiles_every_aggregation(aggregation: Aggregation, expected: str) -> None:
    assert expected in compile_duckdb(_single_measure_plan(aggregation)).sql


def test_compiles_dimensions_then_measures_and_groups_positionally() -> None:
    plan = QueryPlan(
        anchor_source="events",
        joins=(),
        dimensions=(
            PlannedDimension("second", "events", "second_col", "string"),
            PlannedDimension("first", "events", "first_col", "string"),
        ),
        measures=(
            PlannedMeasure("units", parse_expression("events.units"), "sum"),
            PlannedMeasure("total", parse_expression("events.total"), "sum"),
        ),
        metrics=(
            PlannedMetric("combined", parse_expression("units + total")),
            PlannedMetric("revenue", parse_expression("total")),
        ),
        filters=(),
    )

    sql = compile_duckdb(plan).sql
    aggregate_projection, outer_projection = sql.split(") SELECT", maxsplit=1)

    assert (
        aggregate_projection.index('AS "__selayer_dimension_second"')
        < aggregate_projection.index('AS "__selayer_dimension_first"')
        < aggregate_projection.index('AS "__selayer_measure_units"')
        < aggregate_projection.index('AS "__selayer_measure_total"')
    )
    assert " GROUP BY 1, 2" in aggregate_projection
    assert (
        outer_projection.index('"second"')
        < outer_projection.index('"first"')
        < outer_projection.index('AS "combined"')
        < outer_projection.index('AS "revenue"')
    )


def test_cross_kind_names_use_distinct_internal_aggregate_aliases() -> None:
    plan = QueryPlan(
        anchor_source="events",
        joins=(),
        dimensions=(PlannedDimension("total", "events", "total", "integer"),),
        measures=(PlannedMeasure("total", parse_expression("events.value"), "sum"),),
        metrics=(PlannedMetric("m", parse_expression("total")),),
        filters=(),
    )

    sql = compile_duckdb(plan).sql
    aggregate_projection, outer_projection = sql.split(") SELECT", maxsplit=1)

    assert 'AS "__selayer_dimension_total"' in aggregate_projection
    assert 'AS "__selayer_measure_total"' in aggregate_projection
    assert '"__selayer_dimension_total" AS "total"' in outer_projection
    assert '"__selayer_measure_total" AS "m"' in outer_projection


def test_compiles_join_endpoint_not_already_available_and_shared_join_once() -> None:
    plan = QueryPlan(
        anchor_source="events",
        joins=(
            JoinStep("events_customers", "events", "customers", "customer_id", "id"),
            JoinStep("regions_customers", "regions", "customers", "id", "region_id"),
        ),
        dimensions=(PlannedDimension("region", "regions", "name", "string"),),
        measures=(PlannedMeasure("total", parse_expression("events.amount"), "sum"),),
        metrics=(PlannedMetric("revenue", parse_expression("total")),),
        filters=(
            PlannedFilter(
                PlannedDimension("customer", "customers", "name", "string"),
                ScalarFilter("alice"),
            ),
        ),
    )

    sql = compile_duckdb(plan).sql

    assert sql.count(' JOIN "customers"') == 1
    assert 'JOIN "customers" ON "events"."customer_id" = "customers"."id"' in sql
    assert 'JOIN "regions" ON "regions"."id" = "customers"."region_id"' in sql


def test_compiles_scalar_range_list_and_empty_filters_with_bound_values(
    item_margin_plan: QueryPlan,
) -> None:
    event_day = PlannedDimension("event_day", "order_items", "event_day", "date")
    channel = PlannedDimension("channel", "order_items", "channel", "string")
    discarded = PlannedDimension("discarded", "order_items", "discarded", "string")
    plan = replace(
        item_margin_plan,
        filters=(
            PlannedFilter(item_margin_plan.dimensions[0], ScalarFilter("Books")),
            PlannedFilter(event_day, RangeFilter("2026-01-01", "2026-01-31")),
            PlannedFilter(channel, ListFilter(("web", "store"))),
            PlannedFilter(discarded, ListFilter(())),
        ),
    )

    compiled = compile_duckdb(plan)

    assert (
        'WHERE "products"."category" = ? '
        'AND "order_items"."event_day" BETWEEN ? AND ? '
        'AND "order_items"."channel" IN (?, ?) AND FALSE'
    ) in compiled.sql
    assert compiled.parameters == (
        "Books",
        "2026-01-01",
        "2026-01-31",
        "web",
        "store",
    )


def test_filter_value_is_never_interpolated_into_sql(
    item_margin_plan: QueryPlan,
) -> None:
    malicious = "Books'); DROP TABLE order_items; --"
    plan = replace(
        item_margin_plan,
        filters=(
            PlannedFilter(
                item_margin_plan.dimensions[0],
                ScalarFilter(malicious),
            ),
        ),
    )

    compiled = compile_duckdb(plan)

    assert malicious not in compiled.sql
    assert compiled.parameters == (malicious,)
    assert '"products"."category" = ?' in compiled.sql


@pytest.mark.parametrize(
    "compiler,expression",
    [
        (compile_row_expression, Reference(("only_one",))),
        (compile_metric_expression, Reference(("too", "many"))),
        (compile_row_expression, BinaryOperation("||", Literal(1), Literal(2))),
        (compile_metric_expression, BinaryOperation("||", Literal(1), Literal(2))),
        (compile_row_expression, FunctionCall("danger", (Literal(1),))),
        (compile_metric_expression, FunctionCall("danger", (Literal(1),))),
        (compile_row_expression, cast(Expression, object())),
        (compile_metric_expression, cast(Expression, object())),
    ],
)
def test_rejects_nodes_impossible_in_validated_expressions(
    compiler: object, expression: Expression
) -> None:
    compile_expression = cast(object, compiler)
    with pytest.raises(AssertionError):
        cast(object, compile_expression)(expression)  # type: ignore[operator]


def test_compiled_query_is_immutable(item_margin_plan: QueryPlan) -> None:
    compiled = compile_duckdb(item_margin_plan)

    assert isinstance(compiled, CompiledQuery)
    with pytest.raises(AttributeError):
        compiled.sql = "changed"  # type: ignore[misc]
