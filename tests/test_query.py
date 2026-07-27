from pathlib import Path

import polars as pl
import pytest

from selayer import DataSource, Dimension, Metric, QueryEngine, SemanticLayer


def test_metric_evaluate_binds_repeated_placeholders_in_expression_order(
    semantic_layer: SemanticLayer,
) -> None:
    metric = Metric(
        name="calculation",
        description="Context calculation",
        expression="SELECT {{right}} + {{left}} + {{right}}",
    )

    result = metric.evaluate({"left": 1, "right": 2}, QueryEngine(semantic_layer))

    assert result == [(5,)]


def test_metric_only_query_returns_aggregate(semantic_layer: SemanticLayer) -> None:
    result = QueryEngine(semantic_layer).query(["revenue"])

    assert result.to_dicts() == [{"revenue": 35.0}]


def test_dimension_alias_collision_groups_correctly(
    semantic_layer: SemanticLayer,
) -> None:
    result = QueryEngine(semantic_layer).query(["revenue"], ["customer_id"])

    assert result.sort("customer_id").to_dicts() == [
        {"customer_id": 1, "revenue": 30.0},
        {"customer_id": 2, "revenue": 5.0},
    ]


def test_all_example_metric_dimension_combinations_are_classified() -> None:
    root = Path(__file__).parents[1]
    layer = SemanticLayer.load(str(root / "ecommerce_semantic_layer.yaml"))
    engine = QueryEngine(layer)
    fan_out_dimensions = {
        "product_id",
        "product_category",
        "product_subcategory",
    }

    combinations_checked = 0
    for metric in layer.metrics:
        for dimension in layer.dimensions:
            combinations_checked += 1
            if metric == "gross_margin":
                with pytest.raises(ValueError, match="multiple fact sources"):
                    engine.query([metric], [dimension])
            elif dimension in fan_out_dimensions:
                with pytest.raises(ValueError, match="unsafe fan-out"):
                    engine.query([metric], [dimension])
            else:
                result = engine.query([metric], [dimension])
                assert result.columns == [dimension, metric]

    assert combinations_checked == 36


def test_metric_across_fact_sources_is_rejected() -> None:
    root = Path(__file__).parents[1]
    layer = SemanticLayer.load(str(root / "ecommerce_semantic_layer.yaml"))

    with pytest.raises(ValueError, match="multiple fact sources"):
        QueryEngine(layer).query(["gross_margin"])


def test_dimension_join_that_expands_fact_rows_is_rejected() -> None:
    root = Path(__file__).parents[1]
    layer = SemanticLayer.load(str(root / "ecommerce_semantic_layer.yaml"))

    with pytest.raises(ValueError, match="unsafe fan-out"):
        QueryEngine(layer).query(["average_order_value"], ["product_category"])


def test_query_requires_at_least_one_metric(
    semantic_layer: SemanticLayer,
) -> None:
    with pytest.raises(ValueError, match="at least one metric"):
        QueryEngine(semantic_layer).query([])


def test_query_rejects_unknown_metric(semantic_layer: SemanticLayer) -> None:
    with pytest.raises(ValueError, match="unknown metric"):
        QueryEngine(semantic_layer).query(["missing"])


def test_query_rejects_unknown_dimension(semantic_layer: SemanticLayer) -> None:
    with pytest.raises(ValueError, match="unknown dimension"):
        QueryEngine(semantic_layer).query(["revenue"], ["missing"])


def test_filter_values_are_bound_as_parameters(
    semantic_layer: SemanticLayer,
) -> None:
    result = QueryEngine(semantic_layer).query(
        ["revenue"], filters={"customer_name": "O'Reilly"}
    )

    assert result.to_dicts() == [{"revenue": 30.0}]


def test_range_filter_uses_bound_parameters(semantic_layer: SemanticLayer) -> None:
    result = QueryEngine(semantic_layer).query(
        ["revenue"], filters={"customer_id": (1, 1)}
    )

    assert result.to_dicts() == [{"revenue": 30.0}]


def test_list_filter_uses_bound_parameters(semantic_layer: SemanticLayer) -> None:
    result = QueryEngine(semantic_layer).query(
        ["revenue"], filters={"customer_id": [2]}
    )

    assert result.to_dicts() == [{"revenue": 5.0}]


def test_unknown_filter_is_rejected(semantic_layer: SemanticLayer) -> None:
    with pytest.raises(ValueError, match="unknown filter dimension"):
        QueryEngine(semantic_layer).query(["revenue"], filters={"undeclared": "value"})


def test_missing_join_path_is_rejected(
    semantic_layer: SemanticLayer, tmp_path: Path
) -> None:
    regions_path = tmp_path / "regions.parquet"
    pl.DataFrame({"name": ["north", "south"]}).write_parquet(regions_path)
    semantic_layer.add_data_source(
        DataSource(name="regions", type="parquet", path=str(regions_path))
    )
    semantic_layer.add_dimension(
        Dimension(
            name="region",
            description="Disconnected region",
            data_type="string",
            source="regions",
            column="name",
        )
    )

    with pytest.raises(ValueError, match="no relationship path"):
        QueryEngine(semantic_layer).query(["revenue"], ["region"])


def test_polars_query_engine_is_rejected(semantic_layer: SemanticLayer) -> None:
    with pytest.raises(ValueError, match="Unsupported engine type"):
        QueryEngine(semantic_layer, engine_type="polars")
