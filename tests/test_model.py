from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from selayer import Fact, Metric, SemanticLayer
from selayer.expressions import ExpressionSyntaxError, parse_expression


def test_fact_from_expression_parses_valid_expression_and_is_frozen() -> None:
    fact = Fact.from_expression(
        name="item_cost",
        source="order_items",
        expression="order_items.quantity * products.cost",
        data_type="decimal",
        description="Extended product cost",
    )

    assert fact.expression == parse_expression("order_items.quantity * products.cost")
    with pytest.raises(FrozenInstanceError):
        fact.expression = parse_expression("order_items.quantity")  # type: ignore[misc]


def test_metric_from_expression_parses_and_normalizes_measures_to_tuple() -> None:
    measures = ["total_item_revenue", "total_item_cost"]

    metric = Metric.from_expression(
        name="gross_margin",
        expression="total_item_revenue - total_item_cost",
        measures=measures,
        description="Gross margin",
    )

    assert metric.expression == parse_expression("total_item_revenue - total_item_cost")
    assert metric.measures == ("total_item_revenue", "total_item_cost")
    assert isinstance(metric.measures, tuple)
    measures.append("units_sold")
    assert metric.measures == ("total_item_revenue", "total_item_cost")
    with pytest.raises(FrozenInstanceError):
        metric.measures = ()  # type: ignore[misc]


def test_expression_factories_match_yaml_parsing(valid_catalog_path: Path) -> None:
    layer = SemanticLayer.load(valid_catalog_path)

    fact = Fact.from_expression(
        name="item_cost",
        source="order_items",
        expression="order_items.quantity * products.cost",
        data_type="decimal",
        description="Extended product cost for one order item",
    )
    metric = Metric.from_expression(
        name="gross_margin",
        expression=(
            "(total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)"
        ),
        measures=["total_item_revenue", "total_item_cost"],
        description="Gross margin ratio",
    )

    assert fact == layer.facts["item_cost"]
    assert metric == layer.metrics["gross_margin"]


@pytest.mark.parametrize("model", [Fact, Metric])
def test_expression_factories_propagate_deterministic_parser_errors(
    model: type[Fact] | type[Metric],
) -> None:
    expression = "amount + @"
    with pytest.raises(ExpressionSyntaxError) as direct_error:
        parse_expression(expression)

    if model is Fact:
        invoke = lambda: Fact.from_expression("amount", "orders", expression, "decimal")
    else:
        invoke = lambda: Metric.from_expression("revenue", expression, ["amount"])

    with pytest.raises(ExpressionSyntaxError) as factory_error:
        invoke()

    assert (
        factory_error.value.expression,
        factory_error.value.offset,
        factory_error.value.message,
        str(factory_error.value),
    ) == (
        direct_error.value.expression,
        direct_error.value.offset,
        direct_error.value.message,
        str(direct_error.value),
    )
