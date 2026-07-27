from pathlib import Path

import selayer
from selayer import Measure, SemanticLayer


def test_public_interface_exports_expected_symbols() -> None:
    expected = {
        "DataSource",
        "Dimension",
        "Fact",
        "Hierarchy",
        "Measure",
        "Metric",
        "QueryEngine",
        "Relationship",
        "SemanticLayer",
    }

    assert set(selayer.__all__) == expected
    assert all(hasattr(selayer, name) for name in expected)


def test_semantic_layer_round_trips_yaml_and_json(
    semantic_layer: SemanticLayer,
) -> None:
    expected = semantic_layer.to_dict()

    assert SemanticLayer.from_yaml(semantic_layer.to_yaml()).to_dict() == expected
    assert SemanticLayer.from_json(semantic_layer.to_json()).to_dict() == expected


def test_semantic_layer_loads_yaml_file(
    semantic_layer: SemanticLayer, tmp_path: Path
) -> None:
    path = tmp_path / "layer.yaml"
    semantic_layer.save(str(path))

    assert SemanticLayer.load(str(path)).to_dict() == semantic_layer.to_dict()


def test_mermaid_contains_sources_and_relationships(
    semantic_layer: SemanticLayer,
) -> None:
    diagram = semantic_layer.to_mermaid()

    assert diagram.startswith("erDiagram")
    assert "customers" in diagram
    assert "orders_customers" in diagram


def test_count_distinct_measure_compiles_filtered_expression() -> None:
    measure = Measure(
        name="completed_orders",
        description="Completed order count",
        fact="order_id",
        aggregation="count_distinct",
        filter_expression="orders.status = 'completed'",
    )

    assert measure.to_sql() == (
        "COUNT(DISTINCT CASE WHEN orders.status = 'completed' "
        "THEN {fact_source}.{fact_column} ELSE NULL END)"
    )
