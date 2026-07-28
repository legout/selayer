from __future__ import annotations

from pathlib import Path

import pytest

from selayer import QueryEngine, QueryExecutionError, SemanticLayer


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
    result = engine.query(
        ["gross_margin"], ["product_category"], {"product_category": "Books"}
    )
    assert result.columns == ["product_category", "gross_margin"]


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


def test_query_execution_error_does_not_leak_bound_values(
    valid_catalog_path: Path,
) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    engine.close()
    secret = "secret-filter-value"
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"], filters={"product_category": secret})
    assert caught.value.query_id
    assert secret not in str(caught.value)
    assert secret not in caught.value.message


def test_query_execution_error_contains_sql_without_parameters(
    valid_catalog_path: Path,
) -> None:
    engine = QueryEngine(SemanticLayer.load(valid_catalog_path))
    engine.close()
    with pytest.raises(QueryExecutionError) as caught:
        engine.query(["gross_margin"])
    assert "WITH" in caught.value.message
    assert caught.value.query_id in str(caught.value)
