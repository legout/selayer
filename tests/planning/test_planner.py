from __future__ import annotations

import pytest

from selayer._next.model import SemanticLayer
from selayer.planning import QueryPlanningError, QueryRequest, plan_query


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
