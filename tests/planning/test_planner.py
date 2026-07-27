from __future__ import annotations

from collections.abc import Mapping

import pytest

from selayer._next import (
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
)
from selayer.expressions import parse_expression
from selayer.planning import (
    ListFilter,
    QueryPlanningError,
    QueryRequest,
    RangeFilter,
    ScalarFilter,
    plan_query,
)


def _layer(
    *,
    data_sources: Mapping[str, DataSource] | None = None,
    dimensions: Mapping[str, Dimension] | None = None,
    facts: Mapping[str, Fact] | None = None,
    measures: Mapping[str, Measure] | None = None,
    metrics: Mapping[str, Metric] | None = None,
    relationships: Mapping[str, Relationship] | None = None,
) -> SemanticLayer:
    return SemanticLayer(
        version=1,
        name="test",
        label="",
        description="",
        data_sources=data_sources or {},
        dimensions=dimensions or {},
        facts=facts or {},
        measures=measures or {},
        metrics=metrics or {},
        relationships=relationships or {},
    )


@pytest.fixture
def ecommerce_layer() -> SemanticLayer:
    return _layer(
        data_sources={
            "orders": DataSource("orders", "parquet", "orders", ("id",)),
            "order_items": DataSource(
                "order_items", "parquet", "items", ("order_id", "product_id")
            ),
            "products": DataSource("products", "parquet", "products", ("id",)),
        },
        dimensions={
            "product_category": Dimension(
                "product_category", "products", "category", "string"
            )
        },
        facts={
            "item_revenue": Fact(
                "item_revenue",
                "order_items",
                parse_expression("order_items.total"),
                "decimal",
            ),
            "item_cost": Fact(
                "item_cost",
                "order_items",
                parse_expression("order_items.quantity * products.cost"),
                "decimal",
            ),
        },
        measures={
            "total_item_revenue": Measure("total_item_revenue", "item_revenue", "sum"),
            "total_item_cost": Measure("total_item_cost", "item_cost", "sum"),
        },
        metrics={
            "gross_margin": Metric(
                "gross_margin",
                parse_expression(
                    "(total_item_revenue - total_item_cost) "
                    "/ nullif(total_item_revenue, 0)"
                ),
                ("total_item_revenue", "total_item_cost"),
            )
        },
        relationships={
            "product_order_items": Relationship(
                "product_order_items",
                "products",
                "order_items",
                "one_to_many",
                "id",
                "product_id",
            )
        },
    )


def test_plans_item_margin_by_product_category(
    ecommerce_layer: SemanticLayer,
) -> None:
    plan = plan_query(
        ecommerce_layer,
        QueryRequest(metrics=("gross_margin",), dimensions=("product_category",)),
    )

    assert plan.anchor_source == "order_items"
    assert [join.relationship_id for join in plan.joins] == ["product_order_items"]
    assert [measure.id for measure in plan.measures] == [
        "total_item_revenue",
        "total_item_cost",
    ]
    assert [dimension.id for dimension in plan.dimensions] == ["product_category"]
    assert [metric.id for metric in plan.metrics] == ["gross_margin"]


def _basic_layer(
    *,
    dimensions: Mapping[str, Dimension] | None = None,
    relationships: Mapping[str, Relationship] | None = None,
) -> SemanticLayer:
    return _layer(
        data_sources={
            "events": DataSource("events", "parquet", "events", ("id",)),
            "lookup": DataSource("lookup", "parquet", "lookup", ("id",)),
        },
        dimensions=dimensions or {},
        facts={
            "amount": Fact(
                "amount", "events", parse_expression("events.amount"), "decimal"
            )
        },
        measures={"total": Measure("total", "amount", "sum")},
        metrics={"revenue": Metric("revenue", parse_expression("total"), ("total",))},
        relationships=relationships or {},
    )


def _assert_error(
    layer: SemanticLayer, request: QueryRequest, expected_code: str
) -> QueryPlanningError:
    with pytest.raises(QueryPlanningError) as caught:
        plan_query(layer, request)
    assert caught.value.code == expected_code
    assert caught.value.message == str(caught.value)
    return caught.value


def test_query_request_normalizes_and_copies_filter_values() -> None:
    raw_filters: dict[str, object] = {
        "scalar": "Books",
        "list": ["Books", "Games"],
        "range": (1, 10),
        "empty": [],
    }

    request = QueryRequest(metrics=("revenue",), filters=raw_filters)  # type: ignore[arg-type]
    raw_filters["scalar"] = "changed"

    assert request.filters == {
        "scalar": ScalarFilter("Books"),
        "list": ListFilter(("Books", "Games")),
        "range": RangeFilter(1, 10),
        "empty": ListFilter(()),
    }
    with pytest.raises(TypeError):
        request.filters["new"] = ScalarFilter("x")  # type: ignore[index]


def test_rejects_empty_metric_request() -> None:
    error = _assert_error(_basic_layer(), QueryRequest(metrics=()), "unknown_metric")
    assert "at least one metric" in error.message


def test_rejects_unknown_metric() -> None:
    _assert_error(_basic_layer(), QueryRequest(metrics=("missing",)), "unknown_metric")


def test_rejects_unknown_dimension() -> None:
    _assert_error(
        _basic_layer(),
        QueryRequest(metrics=("revenue",), dimensions=("missing",)),
        "unknown_dimension",
    )


def test_rejects_unknown_filter_dimension() -> None:
    _assert_error(
        _basic_layer(),
        QueryRequest(metrics=("revenue",), filters={"missing": 1}),
        "unknown_filter_dimension",
    )


def test_rejects_metrics_from_mixed_grains() -> None:
    layer = _layer(
        data_sources={
            "orders": DataSource("orders", "parquet", "orders", ("id",)),
            "items": DataSource("items", "parquet", "items", ("id",)),
        },
        facts={
            "order_amount": Fact(
                "order_amount", "orders", parse_expression("orders.amount"), "decimal"
            ),
            "item_amount": Fact(
                "item_amount", "items", parse_expression("items.amount"), "decimal"
            ),
        },
        measures={
            "orders_total": Measure("orders_total", "order_amount", "sum"),
            "items_total": Measure("items_total", "item_amount", "sum"),
        },
        metrics={
            "orders_revenue": Metric(
                "orders_revenue", parse_expression("orders_total"), ("orders_total",)
            ),
            "items_revenue": Metric(
                "items_revenue", parse_expression("items_total"), ("items_total",)
            ),
        },
    )

    _assert_error(
        layer,
        QueryRequest(metrics=("orders_revenue", "items_revenue")),
        "mixed_grain",
    )


def test_rejects_source_with_no_relationship_path() -> None:
    layer = _basic_layer(
        dimensions={"kind": Dimension("kind", "lookup", "kind", "string")}
    )
    _assert_error(
        layer,
        QueryRequest(metrics=("revenue",), dimensions=("kind",)),
        "no_relationship_path",
    )


def _ambiguous_layer(reverse_relationships: bool = False) -> SemanticLayer:
    relationships = {
        "events_left": Relationship(
            "events_left", "events", "left", "many_to_one", "left_id", "id"
        ),
        "left_lookup": Relationship(
            "left_lookup", "left", "lookup", "many_to_one", "lookup_id", "id"
        ),
        "events_right": Relationship(
            "events_right", "events", "right", "many_to_one", "right_id", "id"
        ),
        "right_lookup": Relationship(
            "right_lookup", "right", "lookup", "many_to_one", "lookup_id", "id"
        ),
    }
    if reverse_relationships:
        relationships = dict(reversed(tuple(relationships.items())))
    return _layer(
        data_sources={
            source: DataSource(source, "parquet", source, ("id",))
            for source in ("events", "left", "right", "lookup")
        },
        dimensions={"kind": Dimension("kind", "lookup", "kind", "string")},
        facts={
            "amount": Fact(
                "amount", "events", parse_expression("events.amount"), "decimal"
            )
        },
        measures={"total": Measure("total", "amount", "sum")},
        metrics={"revenue": Metric("revenue", parse_expression("total"), ("total",))},
        relationships=relationships,
    )


def test_rejects_ambiguous_equal_length_paths_deterministically() -> None:
    request = QueryRequest(metrics=("revenue",), dimensions=("kind",))
    first = _assert_error(_ambiguous_layer(), request, "ambiguous_relationship_path")
    second = _assert_error(
        _ambiguous_layer(reverse_relationships=True),
        request,
        "ambiguous_relationship_path",
    )
    assert first.message == second.message
    assert "events_left -> left_lookup" in first.message
    assert "events_right -> right_lookup" in first.message


@pytest.mark.parametrize("cardinality", ["one_to_many", "many_to_many"])
def test_rejects_row_expanding_paths(cardinality: str) -> None:
    layer = _basic_layer(
        dimensions={"kind": Dimension("kind", "lookup", "kind", "string")},
        relationships={
            "events_lookup": Relationship(
                "events_lookup",
                "events",
                "lookup",
                cardinality,  # type: ignore[arg-type]
                "id",
                "event_id",
            )
        },
    )
    _assert_error(
        layer,
        QueryRequest(metrics=("revenue",), dimensions=("kind",)),
        "row_expanding_path",
    )


def test_filter_only_source_adds_join_and_resolved_filter() -> None:
    layer = _basic_layer(
        dimensions={"kind": Dimension("kind", "lookup", "kind", "string")},
        relationships={
            "events_lookup": Relationship(
                "events_lookup",
                "events",
                "lookup",
                "many_to_one",
                "lookup_id",
                "id",
            )
        },
    )

    plan = plan_query(
        layer, QueryRequest(metrics=("revenue",), filters={"kind": ["a", "b"]})
    )

    assert plan.dimensions == ()
    assert [join.relationship_id for join in plan.joins] == ["events_lookup"]
    assert plan.filters[0].dimension.id == "kind"
    assert plan.filters[0].dimension.source == "lookup"
    assert plan.filters[0].value == ListFilter(("a", "b"))


def test_deduplicates_join_shared_by_fact_dimension_and_filter(
    ecommerce_layer: SemanticLayer,
) -> None:
    plan = plan_query(
        ecommerce_layer,
        QueryRequest(
            metrics=("gross_margin",),
            dimensions=("product_category",),
            filters={"product_category": "Books"},
        ),
    )

    assert [join.relationship_id for join in plan.joins] == ["product_order_items"]


def test_plan_is_independent_of_mapping_insertion_order() -> None:
    dimensions = {
        "kind": Dimension("kind", "lookup", "kind", "string"),
        "region": Dimension("region", "lookup", "region", "string"),
    }
    relationship = Relationship(
        "events_lookup", "events", "lookup", "many_to_one", "lookup_id", "id"
    )
    first = _basic_layer(
        dimensions=dimensions,
        relationships={"events_lookup": relationship},
    )
    second = _basic_layer(
        dimensions=dict(reversed(tuple(dimensions.items()))),
        relationships={"events_lookup": relationship},
    )

    first_plan = plan_query(
        first,
        QueryRequest(metrics=("revenue",), filters={"region": "eu", "kind": "sale"}),
    )
    second_plan = plan_query(
        second,
        QueryRequest(metrics=("revenue",), filters={"kind": "sale", "region": "eu"}),
    )

    assert first_plan == second_plan
    assert [item.dimension.id for item in first_plan.filters] == ["kind", "region"]


def test_preserves_requested_metric_dimension_and_measure_order() -> None:
    layer = _layer(
        data_sources={"events": DataSource("events", "parquet", "events", ("id",))},
        dimensions={
            "first": Dimension("first", "events", "first", "string"),
            "second": Dimension("second", "events", "second", "string"),
        },
        facts={
            "amount": Fact(
                "amount", "events", parse_expression("events.amount"), "decimal"
            ),
            "quantity": Fact(
                "quantity", "events", parse_expression("events.quantity"), "integer"
            ),
        },
        measures={
            "total": Measure("total", "amount", "sum"),
            "units": Measure("units", "quantity", "sum"),
        },
        metrics={
            "revenue": Metric("revenue", parse_expression("total"), ("total",)),
            "combined": Metric(
                "combined", parse_expression("units + total"), ("units", "total")
            ),
        },
    )

    plan = plan_query(
        layer,
        QueryRequest(metrics=("combined", "revenue"), dimensions=("second", "first")),
    )

    assert [metric.id for metric in plan.metrics] == ["combined", "revenue"]
    assert [dimension.id for dimension in plan.dimensions] == ["second", "first"]
    assert [measure.id for measure in plan.measures] == ["units", "total"]
