"""Tests for the immutable schema-version-1 catalog loader.

Every catalog rule in the grain-aware design is pinned here: schema version,
required fields, identifier syntax, duplicate keys, reference resolution,
non-empty grain, supported aggregation/cardinality, expression syntax, allowed
expression symbols, metric declaration matching, and deterministic multi-issue
ordering. Loading either returns a fully valid immutable ``SemanticLayer`` or
raises one sorted ``CatalogValidationError``.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from selayer import (
    CatalogIssue,
    CatalogValidationError,
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
)


def _write(tmp_path: Path, text: str, name: str = "layer.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_catalog_loads_complete_valid_catalog(
    valid_catalog_path: Path,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)

    assert isinstance(layer, SemanticLayer)
    assert layer.version == 1
    assert layer.name == "ecommerce"
    assert layer.label == "E-commerce Analytics"
    assert layer.description == "Semantic model for the example store"

    assert set(layer.data_sources) == {"orders", "order_items", "products"}
    assert isinstance(layer.data_sources["orders"], DataSource)
    assert layer.data_sources["order_items"].grain == ("order_id", "product_id")

    assert isinstance(layer.facts["item_cost"], Fact)
    assert layer.facts["item_revenue"].source == "order_items"
    assert layer.facts["item_revenue"].data_type == "decimal"

    assert isinstance(layer.measures["total_item_revenue"], Measure)
    assert layer.measures["total_item_cost"].aggregation == "sum"

    assert isinstance(layer.metrics["gross_margin"], Metric)
    assert layer.metrics["gross_margin"].measures == (
        "total_item_revenue",
        "total_item_cost",
    )

    assert isinstance(layer.dimensions["product_category"], Dimension)
    assert isinstance(layer.relationships["product_order_items"], Relationship)
    assert layer.relationships["product_order_items"].type == "one_to_many"


def test_direct_layer_construction_copies_mappings() -> None:
    sources = {"orders": DataSource("orders", "parquet", "x", ("id",))}
    layer = SemanticLayer(1, "ecommerce", "", "", sources, {}, {}, {}, {}, {})
    sources["new"] = sources["orders"]
    assert "new" not in layer.data_sources
    with pytest.raises(TypeError):
        layer.data_sources["new"] = sources["orders"]  # type: ignore[index]


def test_catalog_collections_are_immutable(
    valid_catalog_path: Path,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    assert isinstance(layer.data_sources, MappingProxyType)
    assert isinstance(layer.facts, MappingProxyType)
    assert isinstance(layer.measures, MappingProxyType)
    assert isinstance(layer.metrics, MappingProxyType)
    assert isinstance(layer.dimensions, MappingProxyType)
    assert isinstance(layer.relationships, MappingProxyType)
    with pytest.raises(TypeError):
        layer.data_sources["extra"] = DataSource(  # type: ignore[index]
            name="extra", type="parquet", path="x", grain=("id",)
        )


def test_direct_layer_copies_all_collection_mappings() -> None:
    collections = [{"x": object()} for _ in range(6)]
    layer = SemanticLayer(1, "x", "", "", *collections)  # type: ignore[arg-type]
    for original, field in zip(
        collections,
        (
            layer.data_sources,
            layer.dimensions,
            layer.facts,
            layer.measures,
            layer.metrics,
            layer.relationships,
        ),
    ):
        original["y"] = object()
        assert "y" not in field


def test_catalog_model_objects_are_frozen(
    valid_catalog_path: Path,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    with pytest.raises((AttributeError, TypeError)):
        layer.data_sources["orders"].path = "tampered"  # type: ignore[misc]


def test_catalog_lookup_helpers_raise_keyerror(valid_catalog_path: Path) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    with pytest.raises(KeyError):
        layer.source("missing")
    with pytest.raises(KeyError):
        layer.metric("missing")
    assert layer.source("orders").name == "orders"
    assert layer.measure("total_item_cost").fact == "item_cost"


def test_semantic_objects_have_stable_typed_identifiers(
    valid_catalog_path: Path,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    objects = layer.semantic_objects()

    assert tuple(objects) == tuple(sorted(objects))
    assert set(objects) == {
        "source.order_items",
        "source.orders",
        "source.products",
        "dimension.order_date",
        "dimension.product_category",
        "fact.item_cost",
        "fact.item_revenue",
        "measure.total_item_cost",
        "measure.total_item_revenue",
        "metric.gross_margin",
        "relationship.product_order_items",
    }
    assert objects["source.order_items"] is layer.data_sources["order_items"]
    assert objects["dimension.product_category"] is layer.dimensions["product_category"]
    assert objects["metric.gross_margin"] is layer.metrics["gross_margin"]


def test_semantic_objects_mapping_is_immutable(valid_catalog_path: Path) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    objects = layer.semantic_objects()

    assert isinstance(objects, MappingProxyType)
    with pytest.raises(TypeError):
        objects["dimension.product_color"] = layer.dimensions["product_category"]  # type: ignore[index]


def test_resolve_rejects_unknown_semantic_identifier(
    valid_catalog_path: Path,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)

    with pytest.raises(KeyError, match="dimension.product_color"):
        layer.resolve("dimension.product_color")


# ---------------------------------------------------------------------------
# Top-level structure and version
# ---------------------------------------------------------------------------


def test_catalog_rejects_non_int_schema_versions(tmp_path: Path) -> None:
    for version in ("true", "1.0"):
        path = _write(
            tmp_path,
            f"version: {version}\nname: ecommerce\ndata_sources: {{}}\n",
            version + ".yaml",
        )
        with pytest.raises(CatalogValidationError) as caught:
            SemanticLayer.load(path)
        assert caught.value.issues == (
            CatalogIssue("version", "expected schema version 1"),
        )


def test_catalog_requires_schema_version_one(tmp_path: Path) -> None:
    path = tmp_path / "layer.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert caught.value.issues == (
        CatalogIssue(path="version", message="expected schema version 1"),
    )


def test_catalog_missing_version(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: ecommerce\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "version" in {issue.path for issue in caught.value.issues}


def test_catalog_missing_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "name" in {issue.path for issue in caught.value.issues}


def test_catalog_invalid_name_identifier(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nname: Bad Name\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "name" and "name" in issue.message
        for issue in caught.value.issues
    )


@pytest.mark.parametrize(
    "section", ["dimensions", "facts", "measures", "metrics", "relationships"]
)
def test_optional_sections_must_be_mappings(tmp_path: Path, section: str) -> None:
    path = _write(
        tmp_path, f"version: 1\nname: ecommerce\ndata_sources: {{}}\n{section}: []\n"
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == section for issue in caught.value.issues)


def test_catalog_missing_data_sources(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nname: ecommerce\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "data_sources" in {issue.path for issue in caught.value.issues}


def test_catalog_malformed_yaml_is_catalog_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: [1\nname: ecommerce\n")
    with pytest.raises(CatalogValidationError):
        SemanticLayer.load(path)


def test_catalog_root_must_be_mapping(tmp_path: Path) -> None:
    path = _write(tmp_path, "- version: 1\n- name: ecommerce\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert len(caught.value.issues) >= 1


def test_catalog_empty_data_sources_is_valid(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nname: ecommerce\ndata_sources: {}\n")
    layer = SemanticLayer.load(path)
    assert layer.data_sources == {}


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


def test_catalog_rejects_unhashable_field_types_without_typeerror(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n  orders:\n    type: [parquet]\n    path: [x]\n    grain: [id]\nmeasures:\n  total:\n    fact: [amount]\n    aggregation: [sum]\nrelationships:\n  rel:\n    source: orders\n    target: orders\n    type: [one_to_one]\n    source_column: id\n    target_column: id\n",
    )
    with pytest.raises(CatalogValidationError):
        SemanticLayer.load(path)


def test_catalog_rejects_wrong_field_types_without_typeerror(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n  orders:\n    type: [parquet]\n    path: 1\n    grain: [id, 2]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert {
        "data_sources.orders.type",
        "data_sources.orders.path",
        "data_sources.orders.grain",
    } <= paths


def test_catalog_data_source_missing_type(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    path: x\n    grain: [id]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "data_sources.orders.type" for issue in caught.value.issues
    )


def test_catalog_data_source_missing_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    grain: [id]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "data_sources.orders.path" for issue in caught.value.issues
    )


def test_catalog_data_source_missing_grain(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "data_sources.orders.grain" for issue in caught.value.issues
    )


def test_catalog_data_source_empty_grain(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: []\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "data_sources.orders.grain" and "grain" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_data_source_invalid_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  Bad-Key:\n    type: parquet\n    path: x\n    grain: [id]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "data_sources.Bad-Key" for issue in caught.value.issues)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def test_catalog_dimension_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "dimensions:\n  cat:\n    source: orders\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert "dimensions.cat.column" in paths
    assert "dimensions.cat.data_type" in paths


def test_catalog_dimension_unknown_source(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "dimensions:\n  cat:\n    source: missing\n    column: c\n    data_type: string\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "dimensions.cat.source" for issue in caught.value.issues)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def test_catalog_fact_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  amount:\n    source: orders\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert "facts.amount.expression" in paths
    assert "facts.amount.data_type" in paths


def test_catalog_fact_unknown_source(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  amount:\n    source: missing\n    expression: missing.x\n    data_type: decimal\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "facts.amount.source" for issue in caught.value.issues)


def test_catalog_fact_invalid_expression_syntax(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  amount:\n    source: orders\n    expression: 'orders.amount +'\n    data_type: decimal\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "facts.amount.expression" for issue in caught.value.issues)


def test_catalog_fact_row_expression_rejects_one_part_reference(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  amount:\n    source: orders\n    expression: amount\n    data_type: decimal\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "facts.amount.expression" for issue in caught.value.issues)


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def test_catalog_measure_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "measures:\n  total:\n    fact: amount\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "measures.total.aggregation" for issue in caught.value.issues
    )


def test_catalog_measure_unknown_fact(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "measures:\n  total:\n    fact: missing\n    aggregation: sum\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "measures.total.fact" for issue in caught.value.issues)


@pytest.mark.parametrize("aggregation", ["median", "stdev", "SUM", "average", ""])
def test_catalog_measure_unsupported_aggregation(
    tmp_path: Path, aggregation: str
) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        f"measures:\n  total:\n    fact: amount\n    aggregation: {aggregation}\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "measures.total.aggregation" for issue in caught.value.issues
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_catalog_metric_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "metrics:\n  ratio:\n    expression: a\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert "metrics.ratio.measures" in paths


def test_catalog_metric_unknown_measure_reference(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "metrics:\n  ratio:\n    expression: total + extra\n    measures: [total]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.ratio.expression" for issue in caught.value.issues
    )


def test_catalog_metric_declaration_mismatch(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "metrics:\n  ratio:\n    expression: total\n    measures: [total, unused]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.ratio.expression" for issue in caught.value.issues
    )


def test_catalog_metric_invalid_expression_syntax(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "metrics:\n  ratio:\n    expression: 'total +'\n    measures: [total]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.ratio.expression" for issue in caught.value.issues
    )


def test_catalog_metric_rejects_row_only_function(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "metrics:\n  ratio:\n    expression: lower(total)\n    measures: [total]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.ratio.expression" for issue in caught.value.issues
    )


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def test_catalog_relationship_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "relationships:\n  rel:\n    source: orders\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert "relationships.rel.target" in paths
    assert "relationships.rel.type" in paths
    assert "relationships.rel.source_column" in paths
    assert "relationships.rel.target_column" in paths


def test_catalog_relationship_unknown_endpoint(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "relationships:\n  rel:\n    source: orders\n    target: missing\n"
        "    type: one_to_many\n    source_column: id\n    target_column: oid\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "relationships.rel.target" for issue in caught.value.issues
    )


@pytest.mark.parametrize("cardinality", ["one_to_three", "many", "1_to_n", ""])
def test_catalog_relationship_unsupported_cardinality(
    tmp_path: Path, cardinality: str
) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "relationships:\n  rel:\n    source: orders\n    target: orders\n"
        f"    type: {cardinality}\n    source_column: id\n    target_column: id\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "relationships.rel.type" for issue in caught.value.issues)


def test_relationship_has_type_not_cardinality() -> None:
    relationship = Relationship("rel", "a", "b", "one_to_one", "id", "id")
    assert relationship.type == "one_to_one"
    assert not hasattr(relationship, "cardinality")


def test_catalog_accepts_many_to_many_type(tmp_path: Path) -> None:
    # many_to_many is a valid cardinality value; planning it is deferred to the
    # planner task, so the catalog must not reject it.
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "  carts:\n    type: parquet\n    path: y\n    grain: [id]\n"
        "relationships:\n  rel:\n    source: orders\n    target: carts\n"
        "    type: many_to_many\n    source_column: id\n    target_column: id\n",
    )
    layer = SemanticLayer.load(path)
    assert layer.relationships["rel"].type == "many_to_many"


# ---------------------------------------------------------------------------
# Duplicate keys and identifier rules
# ---------------------------------------------------------------------------


def test_catalog_duplicate_key_within_mapping_is_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: a\n    grain: [id]\n"
        "  orders:\n    type: parquet\n    path: b\n    grain: [id2]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        "orders" in issue.message and "data_sources.orders" in issue.path
        for issue in caught.value.issues
    )


def test_catalog_invalid_identifier_in_each_collection(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  'Bad-Fact':\n    source: orders\n    expression: orders.amount\n    data_type: decimal\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(issue.path == "facts.Bad-Fact" for issue in caught.value.issues)


# ---------------------------------------------------------------------------
# Deterministic multi-issue ordering
# ---------------------------------------------------------------------------


def test_catalog_collects_and_sorts_multiple_issues(tmp_path: Path) -> None:
    path = tmp_path / "layer.yaml"
    path.write_text(
        "version: 1\nname: Bad Name\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: []\n"
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    issues = list(caught.value.issues)
    assert issues == sorted(issues, key=lambda issue: (issue.path, issue.message))
    # Both independent issues are reported (invalid name + empty grain).
    paths = {issue.path for issue in issues}
    assert "name" in paths
    assert "data_sources.orders.grain" in paths


def test_catalog_multiple_independent_issues_all_collected(tmp_path: Path) -> None:
    # Bad version, invalid name, empty grain, unknown dimension source, bad
    # aggregation, unsupported cardinality: all independent, all collected.
    path = _write(
        tmp_path,
        "version: 9\nname: Bad Name\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: []\n"
        "dimensions:\n  cat:\n    source: nowhere\n    column: c\n    data_type: string\n"
        "measures:\n  total:\n    fact: none\n    aggregation: median\n"
        "relationships:\n  rel:\n    source: orders\n    target: orders\n"
        "    type: weird\n    source_column: id\n    target_column: id\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    issues = list(caught.value.issues)
    assert issues == sorted(issues, key=lambda issue: (issue.path, issue.message))
    assert len(issues) >= 6
    paths = {issue.path for issue in issues}
    assert {
        "version",
        "name",
        "data_sources.orders.grain",
        "dimensions.cat.source",
        "measures.total.aggregation",
        "relationships.rel.type",
    } <= paths


def test_catalog_loading_is_deterministic(valid_catalog_path: Path) -> None:
    # The same valid input must produce equal immutable layers every time.
    first = SemanticLayer.load(valid_catalog_path)
    second = SemanticLayer.load(valid_catalog_path)
    assert first == second
