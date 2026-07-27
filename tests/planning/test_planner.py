from __future__ import annotations

import pytest

from selayer._next.model import Dimension, Relationship, SemanticLayer
from selayer.planning import (
    ListFilter,
    QueryPlanningError,
    QueryRequest,
    RangeFilter,
    ScalarFilter,
    plan_query,
)


def test_plans_item_margin_by_product_category(
    valid_catalog_path,  # type: ignore[no-untyped-def]
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    plan = plan_query(
        layer,
        QueryRequest(metrics=("gross_margin",), dimensions=("product_category",)),
    )
    assert plan.anchor_source == "order_items"
    assert [join.relationship_id for join in plan.joins] == ["product_order_items"]
    assert [measure.id for measure in plan.measures] == [
        "total_item_revenue",
        "total_item_cost",
    ]


def test_unknown_identifiers_have_stable_codes(valid_catalog_path) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    with pytest.raises(QueryPlanningError, match="unknown_metric") as error:
        plan_query(layer, QueryRequest(metrics=("missing",)))
    assert error.value.code == "unknown_metric"

    with pytest.raises(QueryPlanningError) as error:
        plan_query(
            layer, QueryRequest(metrics=("gross_margin",), dimensions=("missing",))
        )
    assert error.value.code == "unknown_dimension"

    with pytest.raises(QueryPlanningError) as error:
        plan_query(
            layer, QueryRequest(metrics=("gross_margin",), filters={"missing": 1})
        )
    assert error.value.code == "unknown_filter_dimension"


def test_different_grain_sources_are_mixed(valid_catalog_path) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    from dataclasses import replace

    facts = dict(layer.facts)
    measures = dict(layer.measures)
    metrics = dict(layer.metrics)
    facts["product_fact"] = replace(
        facts["item_revenue"], name="product_fact", source="products"
    )
    measures["total_product"] = replace(
        measures["total_item_revenue"], name="total_product", fact="product_fact"
    )
    metrics["different_grains"] = replace(
        metrics["gross_margin"],
        name="different_grains",
        measures=("total_item_revenue", "total_product"),
    )
    layer = layer.__class__(
        layer.version,
        layer.name,
        layer.label,
        layer.description,
        layer.data_sources,
        layer.dimensions,
        facts,
        measures,
        metrics,
        layer.relationships,
    )
    with pytest.raises(QueryPlanningError) as error:
        plan_query(layer, QueryRequest(("different_grains",)))
    assert error.value.code == "mixed_grain"


def test_invalid_identifier_order_has_stable_error_details(valid_catalog_path) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    errors = []
    for identifiers in (("missing_a", "missing_b"), ("missing_b", "missing_a")):
        with pytest.raises(QueryPlanningError) as error:
            plan_query(layer, QueryRequest(identifiers))
        errors.append((error.value.code, error.value.message))
    assert errors[0] == errors[1]


def test_same_grain_different_anchor_sources_are_mixed(
    valid_catalog_path,  # type: ignore[no-untyped-def]
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    # products and orders both have [id] grain, but are unrelated anchors.
    from dataclasses import replace

    measures = dict(layer.measures)
    facts = dict(layer.facts)
    metrics = dict(layer.metrics)
    facts["order_fact"] = replace(
        facts["item_revenue"], name="order_fact", source="orders"
    )
    measures["total_order"] = replace(
        measures["total_item_revenue"], name="total_order", fact="order_fact"
    )
    metrics["two_anchors"] = replace(
        metrics["gross_margin"],
        name="two_anchors",
        measures=("total_item_revenue", "total_order"),
    )
    sources = dict(layer.data_sources)
    sources["order_items"] = replace(sources["order_items"], grain=("id",))
    layer = replace(
        layer, data_sources=sources, facts=facts, measures=measures, metrics=metrics
    )
    with pytest.raises(QueryPlanningError) as error:
        plan_query(layer, QueryRequest(metrics=("two_anchors",)))
    assert error.value.code == "mixed_grain"


def test_query_request_freezes_caller_filters() -> None:
    filters: dict[str, list[str] | int] = {"status": ["open", "paid"]}
    request = QueryRequest(metrics=["m"], dimensions=["d"], filters=filters)
    assert isinstance(filters["status"], list)
    filters["status"].append("cancelled")
    filters["new"] = 1
    assert request.metrics == ("m",)
    assert request.dimensions == ("d",)
    assert isinstance(request.filters["status"], ListFilter)
    assert request.filters["status"].values == ("open", "paid")
    assert "new" not in request.filters


def test_direct_list_filter_is_defensively_immutable() -> None:
    values = [1, 2]
    variant = ListFilter(values)  # type: ignore[arg-type]
    request = QueryRequest(metrics=("m",), filters={"id": variant})

    values.append(3)
    assert variant.values == (1, 2)
    assert request.filters["id"] == ListFilter((1, 2))
    assert isinstance(request.filters["id"], ListFilter)
    assert isinstance(request.filters["id"].values, tuple)


def test_query_request_normalizes_all_filter_variants() -> None:
    request = QueryRequest(
        metrics=("m",),
        filters={"scalar": 1, "list": [1, 2], "range": (1, 3)},
    )
    assert request.filters == {
        "scalar": ScalarFilter(1),
        "list": ListFilter((1, 2)),
        "range": RangeFilter(1, 3),
    }


def _layer_with_dimension(layer: SemanticLayer, source: str, *, relationships=()):
    dimensions = dict(layer.dimensions)
    dimensions["extra"] = Dimension("extra", source, "id", "string")
    rels = dict(layer.relationships)
    rels.update({rel.name: rel for rel in relationships})
    return SemanticLayer(
        layer.version,
        layer.name,
        layer.label,
        layer.description,
        layer.data_sources,
        dimensions,
        layer.facts,
        layer.measures,
        layer.metrics,
        rels,
    )


@pytest.mark.parametrize(
    ("query", "code"),
    [
        (QueryRequest(metrics=()), "unknown_metric"),
        (QueryRequest(metrics=("missing",)), "unknown_metric"),
        (
            QueryRequest(metrics=("gross_margin",), dimensions=("missing",)),
            "unknown_dimension",
        ),
        (
            QueryRequest(metrics=("gross_margin",), filters={"missing": 1}),
            "unknown_filter_dimension",
        ),
    ],
)
def test_planner_identifier_and_empty_matrix(valid_catalog_path, query, code) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(QueryPlanningError) as error:
        plan_query(SemanticLayer.load(valid_catalog_path), query)
    assert error.value.code == code


def test_planner_no_path_ambiguous_and_row_expansion_matrix(valid_catalog_path) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    with pytest.raises(QueryPlanningError, match="no_relationship_path") as error:
        plan_query(
            _layer_with_dimension(layer, "orders"),
            QueryRequest(("gross_margin",), ("extra",)),
        )
    assert error.value.code == "no_relationship_path"

    one = Relationship(
        "a_path", "order_items", "orders", "one_to_one", "order_id", "id"
    )
    two = Relationship(
        "b_path", "order_items", "orders", "one_to_one", "product_id", "id"
    )
    with pytest.raises(QueryPlanningError) as error:
        plan_query(
            _layer_with_dimension(layer, "orders", relationships=(one, two)),
            QueryRequest(("gross_margin",), ("extra",)),
        )
    assert error.value.code == "ambiguous_relationship_path"

    fanout = Relationship(
        "fanout", "order_items", "orders", "one_to_many", "order_id", "id"
    )
    with pytest.raises(QueryPlanningError) as error:
        plan_query(
            _layer_with_dimension(layer, "orders", relationships=(fanout,)),
            QueryRequest(("gross_margin",), ("extra",)),
        )
    assert error.value.code == "row_expanding_path"

    many = Relationship(
        "many", "order_items", "orders", "many_to_many", "order_id", "id"
    )
    with pytest.raises(QueryPlanningError) as error:
        plan_query(
            _layer_with_dimension(layer, "orders", relationships=(many,)),
            QueryRequest(("gross_margin",), ("extra",)),
        )
    assert error.value.code == "row_expanding_path"


def test_reversed_relationship_insertion_order_has_equal_plan(
    valid_catalog_path,
) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    reversed_layer = layer.__class__(
        layer.version,
        layer.name,
        layer.label,
        layer.description,
        layer.data_sources,
        layer.dimensions,
        layer.facts,
        layer.measures,
        layer.metrics,
        dict(reversed(tuple(layer.relationships.items()))),
    )
    request = QueryRequest(("gross_margin",), ("product_category",))
    assert plan_query(layer, request) == plan_query(reversed_layer, request)


def test_shared_join_is_deduplicated_for_multiple_requirements(
    valid_catalog_path,
) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    plan = plan_query(
        layer,
        QueryRequest(
            ("gross_margin",),
            dimensions=("product_category",),
            filters={"product_category": "x"},
        ),
    )
    assert [join.relationship_id for join in plan.joins] == ["product_order_items"]


def test_multiple_metrics_and_dimensions_preserve_caller_order(
    valid_catalog_path,
) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    from dataclasses import replace

    metrics = dict(layer.metrics)
    metrics["revenue_only"] = replace(
        metrics["gross_margin"], name="revenue_only", measures=("total_item_revenue",)
    )
    layer = _layer_with_dimension(layer, "products")
    layer = layer.__class__(
        layer.version,
        layer.name,
        layer.label,
        layer.description,
        layer.data_sources,
        layer.dimensions,
        layer.facts,
        layer.measures,
        metrics,
        layer.relationships,
    )
    plan = plan_query(
        layer,
        QueryRequest(("revenue_only", "gross_margin"), ("extra", "product_category")),
    )
    assert [item.id for item in plan.metrics] == ["revenue_only", "gross_margin"]
    assert [item.id for item in plan.dimensions] == ["extra", "product_category"]


def test_equivalent_filter_mappings_have_deterministic_order(
    valid_catalog_path,
) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    layer = _layer_with_dimension(layer, "products")
    first = plan_query(
        layer,
        QueryRequest(
            ("gross_margin",), filters={"extra": (1, 2), "product_category": "x"}
        ),
    )
    second = plan_query(
        layer,
        QueryRequest(
            ("gross_margin",), filters={"product_category": "x", "extra": (1, 2)}
        ),
    )
    assert first == second
    assert [item.id for item in first.filters] == ["extra", "product_category"]


def test_filter_only_join_deduplication_order_and_stability(valid_catalog_path) -> None:  # type: ignore[no-untyped-def]
    layer = SemanticLayer.load(valid_catalog_path)
    first = plan_query(
        layer, QueryRequest(("gross_margin",), filters={"product_category": "x"})
    )
    order_path = Relationship(
        "order_path", "order_items", "orders", "one_to_one", "order_id", "id"
    )
    joined_layer = _layer_with_dimension(layer, "orders", relationships=(order_path,))
    second = plan_query(
        joined_layer,
        QueryRequest(
            ("gross_margin",), filters={"product_category": "x", "order_date": (1, 2)}
        ),
    )
    assert [join.relationship_id for join in first.joins] == ["product_order_items"]
    assert [item.id for item in second.filters] == ["order_date", "product_category"]
    assert [item.id for item in first.metrics] == ["gross_margin"]
    assert (
        first.joins
        == plan_query(
            layer, QueryRequest(("gross_margin",), filters={"product_category": "x"})
        ).joins
    )
