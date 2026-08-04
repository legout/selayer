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
    SemanticStatus,
)
from selayer.expressions import parse_expression
from selayer.sources.config import ParquetConfig
from selayer.sources.schema import FieldSchema, ScalarType, TableSchema


def _source(name: str = "orders") -> DataSource:
    """Build a minimal valid ``DataSource`` for direct-construction tests."""

    return DataSource(
        name=name,
        connector=ParquetConfig("x"),
        schema=TableSchema((FieldSchema("id", ScalarType("utf8"), False),)),
        grain=("id",),
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
    sources = {"orders": _source()}
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
        layer.data_sources["extra"] = _source("extra")  # type: ignore[index]


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
        layer.data_sources["orders"].connector = ParquetConfig("tampered")  # type: ignore[misc]


def test_fact_and_metric_expression_factories_parse_and_preserve_ast() -> None:
    fact = Fact.from_expression("amount", "orders", "orders.amount + 1", "decimal")
    metric = Metric.from_expression("ratio", "abs(total)", ("total",))
    from selayer.expressions import parse_expression

    assert fact.expression == parse_expression("orders.amount + 1")
    assert metric.expression == parse_expression("abs(total)")


def test_fact_and_metric_expression_factories_reject_invalid_syntax() -> None:
    from selayer.expressions import ExpressionSyntaxError

    with pytest.raises(ExpressionSyntaxError):
        Fact.from_expression("amount", "orders", "orders.amount +", "decimal")
    with pytest.raises(ExpressionSyntaxError):
        Metric.from_expression("ratio", "abs(", ("total",))


def test_metric_factory_rejects_bare_string_measures() -> None:
    with pytest.raises(TypeError, match="measures must be a list or tuple"):
        Metric.from_expression("ratio", "total", "total")  # type: ignore[arg-type]


def test_metric_factory_rejects_non_builtin_string_measure() -> None:
    with pytest.raises(TypeError, match="measures entries must be built-in str"):
        Metric.from_expression("ratio", "total", ["total", ["nested"]])  # type: ignore[list-item]


def test_metric_factory_copies_mutable_measure_input() -> None:
    measures = ["total"]
    metric = Metric.from_expression("ratio", "total", measures)  # type: ignore[arg-type]

    measures.append("other")

    assert metric.measures == ("total",)


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
        objects["dimension.product_color"] = layer.dimensions[  # type: ignore[index]
            "product_category"
        ]


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
            CatalogIssue(
                "version",
                "expected schema version 1",
                "catalog.version.unsupported",
            ),
        )


def test_catalog_requires_schema_version_one(tmp_path: Path) -> None:
    path = tmp_path / "layer.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert caught.value.issues == (
        CatalogIssue(
            path="version",
            message="expected schema version 1",
            code="catalog.version.unsupported",
        ),
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


def test_catalog_malformed_yaml_does_not_leak_source_secrets(
    tmp_path: Path,
) -> None:
    """A YAML parse error never echoes offending source lines (secret safety).

    PyYAML reproduces the offending line verbatim in its diagnostic; the loader
    must surface a fixed domain message so credentials in the file cannot leak
    into ``CatalogIssue`` messages, the error ``str``, or any ``repr``.
    """
    secret = "XYZ-SECRET-123"
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n  orders:\n"
        f"    pwd: {secret} {{ a: b\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    error = caught.value
    assert len(error.issues) == 1
    issue = error.issues[0]
    assert issue.code == "catalog.invalid"
    assert issue.message == "catalog file is not valid YAML"
    for surface in (issue.message, str(error), repr(error), repr(issue)):
        assert secret not in surface


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
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id, 2]\n"
        "    schema:\n      fields: not-a-list\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert "data_sources.orders.grain" in paths
    assert "data_sources.orders.schema.fields" in paths


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


def test_catalog_old_type_path_shape_is_rejected(tmp_path: Path) -> None:
    # The pre-v1 ``type/path/grain`` shape is no longer loadable: ``path`` is an
    # unknown field and every connector now requires a location and a schema.
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    paths = {issue.path for issue in caught.value.issues}
    assert {
        "data_sources.orders.path",
        "data_sources.orders.location",
        "data_sources.orders.schema",
    } <= paths


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


@pytest.mark.parametrize(
    ("function", "arguments", "expected"),
    [
        ("abs", "", 1),
        ("abs", "orders.value, orders.value", 1),
        ("lower", "", 1),
        ("lower", "orders.value, orders.value", 1),
        ("upper", "", 1),
        ("upper", "orders.value, orders.value", 1),
        ("coalesce", "orders.value", 2),
        ("coalesce", "orders.value, orders.value, orders.value", 2),
        ("nullif", "orders.value", 2),
        ("nullif", "orders.value, orders.value, orders.value", 2),
        ("if", "orders.value, orders.value", 3),
        ("if", "orders.value, orders.value, orders.value, orders.value", 3),
    ],
)
def test_catalog_rejects_invalid_row_function_arity(
    tmp_path: Path, function: str, arguments: str, expected: int
) -> None:
    expression = f"{function}({arguments})"
    path = _write(
        tmp_path,
        "version: 1\nname: generic\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  value:\n    source: orders\n"
        f"    expression: {expression}\n    data_type: decimal\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "facts.value.expression"
        and f"function '{function}' expects {expected} argument(s)" in issue.message
        for issue in caught.value.issues
    )


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
# Grain and reachability validation
# ---------------------------------------------------------------------------


def _grain_catalog(measure_grain: str = "[id]") -> str:
    return (
        "version: 1\nname: generic\ndata_sources:\n"
        "  anchor:\n    type: parquet\n    path: anchor\n    grain: [id]\n"
        f"  other:\n    type: parquet\n    path: other\n    grain: {measure_grain}\n"
        "facts:\n"
        "  anchor_value:\n    source: anchor\n    expression: anchor.value\n    data_type: decimal\n"
        "  other_value:\n    source: other\n    expression: other.value\n    data_type: decimal\n"
        "measures:\n"
        "  anchor_total:\n    fact: anchor_value\n    aggregation: sum\n"
        "  other_total:\n    fact: other_value\n    aggregation: sum\n"
        "metrics:\n"
        "  combined:\n    expression: anchor_total + other_total\n    measures: [anchor_total, other_total]\n"
    )


@pytest.mark.parametrize("other_grain", ["[id]", "[other_id]"])
def test_catalog_rejects_metric_measures_with_different_anchor_or_grain(
    tmp_path: Path, other_grain: str
) -> None:
    path = _write(tmp_path, _grain_catalog(other_grain))
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.combined.measures"
        and "same anchor source and grain" in issue.message
        for issue in caught.value.issues
    )


def _fact_reachability_catalog(
    relationship: str, referenced_source: str = "leaf"
) -> str:
    return (
        "version: 1\nname: generic\ndata_sources:\n"
        "  anchor:\n    type: parquet\n    location: anchor\n    grain: [id]\n"
        "    schema:\n"
        "      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: value, type: float64, nullable: true}\n"
        "  middle:\n    type: parquet\n    location: middle\n    grain: [id]\n"
        "    schema:\n"
        "      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: value, type: float64, nullable: true}\n"
        "  leaf:\n    type: parquet\n    location: leaf\n    grain: [id]\n"
        "    schema:\n"
        "      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: value, type: float64, nullable: true}\n"
        "facts:\n"
        f"  value:\n    source: anchor\n    expression: {referenced_source}.value\n    data_type: decimal\n"
        f"relationships:\n{relationship}"
    )


@pytest.mark.parametrize(
    "relationships",
    [
        (
            "  anchor_middle:\n    source: anchor\n    target: middle\n    type: many_to_one\n    source_column: id\n    target_column: id\n"
            "  middle_leaf:\n    source: middle\n    target: leaf\n    type: many_to_one\n    source_column: id\n    target_column: id\n"
        ),
    ],
)
def test_catalog_accepts_safe_many_to_one_fact_chain(
    tmp_path: Path, relationships: str
) -> None:
    layer = SemanticLayer.load(
        _write(tmp_path, _fact_reachability_catalog(relationships))
    )
    assert layer.facts["value"].source == "anchor"


def test_catalog_accepts_forward_one_to_one_fact_reachability(tmp_path: Path) -> None:
    relationship = (
        "  anchor_leaf:\n    source: anchor\n    target: leaf\n"
        "    type: one_to_one\n    source_column: id\n    target_column: id\n"
    )
    layer = SemanticLayer.load(
        _write(tmp_path, _fact_reachability_catalog(relationship))
    )
    assert layer.facts["value"].source == "anchor"


def test_catalog_accepts_reverse_one_to_one_fact_reachability(tmp_path: Path) -> None:
    relationship = (
        "  leaf_anchor:\n    source: leaf\n    target: anchor\n"
        "    type: one_to_one\n    source_column: id\n    target_column: id\n"
    )
    layer = SemanticLayer.load(
        _write(tmp_path, _fact_reachability_catalog(relationship))
    )
    assert layer.facts["value"].source == "anchor"


def test_catalog_accepts_reverse_one_to_many_fact_reachability(tmp_path: Path) -> None:
    relationship = (
        "  leaf_anchor:\n    source: leaf\n    target: anchor\n"
        "    type: one_to_many\n    source_column: id\n    target_column: id\n"
    )
    layer = SemanticLayer.load(
        _write(tmp_path, _fact_reachability_catalog(relationship))
    )
    assert layer.facts["value"].source == "anchor"


def test_catalog_rejects_reverse_many_to_one_fact_reachability(tmp_path: Path) -> None:
    relationship = (
        "  leaf_anchor:\n    source: leaf\n    target: anchor\n"
        "    type: many_to_one\n    source_column: id\n    target_column: id\n"
    )
    path = _write(tmp_path, _fact_reachability_catalog(relationship))
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert caught.value.issues == (
        CatalogIssue(
            path="facts.value.expression",
            message=(
                "source 'leaf' is not reachable from anchor 'anchor' through "
                "grain-preserving relationships"
            ),
        ),
    )


@pytest.mark.parametrize(
    "relationship",
    [
        "",
        "  anchor_middle:\n    source: anchor\n    target: middle\n    type: one_to_many\n    source_column: id\n    target_column: id\n",
        "  anchor_middle:\n    source: anchor\n    target: middle\n    type: many_to_many\n    source_column: id\n    target_column: id\n",
    ],
)
def test_catalog_rejects_fact_with_no_safe_reachability_path(
    tmp_path: Path, relationship: str
) -> None:
    path = _write(tmp_path, _fact_reachability_catalog(relationship))
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "facts.value.expression"
        and "not reachable from anchor" in issue.message
        for issue in caught.value.issues
    )


@pytest.mark.parametrize(
    ("function", "arguments", "expected"),
    [
        ("abs", "", 1),
        ("abs", "total, total", 1),
        ("coalesce", "total", 2),
        ("coalesce", "total, total, total", 2),
        ("nullif", "total", 2),
        ("nullif", "total, total, total", 2),
        ("lower", "", 1),
        ("lower", "total, total", 1),
        ("upper", "", 1),
        ("upper", "total, total", 1),
        ("if", "total, total", 3),
        ("if", "total, total, total, total", 3),
    ],
)
def test_catalog_rejects_invalid_metric_function_arity(
    tmp_path: Path, function: str, arguments: str, expected: int
) -> None:
    expression = f"{function}({arguments})"
    path = _write(
        tmp_path,
        "version: 1\nname: generic\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: [id]\n"
        "facts:\n  value:\n    source: orders\n"
        "    expression: orders.value\n    data_type: decimal\n"
        "measures:\n  total:\n    fact: value\n    aggregation: sum\n"
        "metrics:\n  result:\n"
        f"    expression: {expression}\n    measures: [total]\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.result.expression"
        and f"function '{function}' expects {expected} argument(s)" in issue.message
        for issue in caught.value.issues
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
    # planner, so the catalog must not reject it.
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id]\n"
        "    schema:\n      fields:\n        - {name: id, type: utf8, nullable: false}\n"
        "  carts:\n    type: parquet\n    location: y\n    grain: [id]\n"
        "    schema:\n      fields:\n        - {name: id, type: utf8, nullable: false}\n"
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


# ---------------------------------------------------------------------------
# Model-invariant rules: loaded catalogs emit the same code as verify_static
# ---------------------------------------------------------------------------


def test_catalog_rejects_duplicate_grain_columns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id, id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "catalog.grain.duplicate_column" in {
        issue.code for issue in caught.value.issues
    }


def test_catalog_rejects_nullable_grain_column(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: true}\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "catalog.grain.nullable_column" in {
        issue.code for issue in caught.value.issues
    }


def test_catalog_rejects_relationship_join_type_mismatch(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: qty, type: int64, nullable: true}\n"
        "  products:\n    type: parquet\n    location: y\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "relationships:\n  rel:\n    source: products\n    target: orders\n"
        "    type: one_to_many\n    source_column: id\n    target_column: qty\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "catalog.relationship.join_type_mismatch" in {
        issue.code for issue in caught.value.issues
    }


def test_catalog_rejects_measure_sum_of_string_fact(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nname: ecommerce\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: x\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: status, type: utf8, nullable: true}\n"
        "facts:\n  status:\n    source: orders\n    expression: orders.status\n"
        "    data_type: string\n"
        "measures:\n  total:\n    fact: status\n    aggregation: sum\n",
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert "catalog.measure.invalid_aggregation_type" in {
        issue.code for issue in caught.value.issues
    }


# ---------------------------------------------------------------------------
# Semantic deprecation metadata (model fields)
# ---------------------------------------------------------------------------


def test_semantic_status_enum_is_string_with_two_members() -> None:
    # SemanticStatus is a closed StrEnum so it serializes as its value and is
    # constructable from the canonical status strings.
    assert SemanticStatus.ACTIVE == "active"
    assert SemanticStatus.DEPRECATED == "deprecated"
    assert SemanticStatus("active") is SemanticStatus.ACTIVE
    assert SemanticStatus("deprecated") is SemanticStatus.DEPRECATED
    assert {member.value for member in SemanticStatus} == {"active", "deprecated"}


def test_semantic_objects_default_to_active_with_no_replacement() -> None:
    expr = parse_expression("orders.amount")
    objects = [
        DataSource("s", _source().connector, _source().schema, ("id",)),
        Dimension("d", "orders", "id", "string"),
        Fact("f", "orders", expr, "decimal"),
        Measure("m", "f", "sum"),
        Metric("r", expr, ("m",)),
        Relationship("rel", "a", "b", "one_to_one", "id", "id"),
    ]
    for obj in objects:
        assert obj.status is SemanticStatus.ACTIVE
        assert obj.replaced_by is None


def test_semantic_objects_carry_explicit_deprecation_metadata() -> None:
    expr = parse_expression("orders.amount")
    source = DataSource(
        "s",
        _source().connector,
        _source().schema,
        ("id",),
        status=SemanticStatus.DEPRECATED,
        replaced_by="source.t",
    )
    dimension = Dimension(
        "d",
        "orders",
        "id",
        "string",
        status=SemanticStatus.DEPRECATED,
        replaced_by="dimension.t",
    )
    fact = Fact(
        "f",
        "orders",
        expr,
        "decimal",
        status=SemanticStatus.DEPRECATED,
        replaced_by="fact.t",
    )
    measure = Measure(
        "m", "f", "sum", status=SemanticStatus.DEPRECATED, replaced_by="measure.t"
    )
    metric = Metric(
        "r", expr, ("m",), status=SemanticStatus.DEPRECATED, replaced_by="metric.t"
    )
    relationship = Relationship(
        "rel",
        "a",
        "b",
        "one_to_one",
        "id",
        "id",
        status=SemanticStatus.DEPRECATED,
        replaced_by="relationship.t",
    )

    for obj in (source, dimension, fact, measure, metric, relationship):
        assert obj.status is SemanticStatus.DEPRECATED
        assert obj.replaced_by is not None


def test_deprecation_metadata_fields_are_frozen() -> None:
    source = DataSource("s", _source().connector, _source().schema, ("id",))
    with pytest.raises((AttributeError, TypeError)):
        source.status = SemanticStatus.DEPRECATED  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        source.replaced_by = "source.t"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Semantic deprecation metadata (catalog parsing)
# ---------------------------------------------------------------------------

# A catalog exercising every semantic kind with a deprecated/active pair so the
# loader is proven to parse metadata for sources, dimensions, facts, measures,
# metrics, and relationships.
_DEPRECATED_CATALOG_YAML = """\
version: 1
name: deprecated
data_sources:
  old_source:
    type: parquet
    location: old.parquet
    grain: [id]
    schema:
      fields:
        - {name: id, type: utf8, nullable: false}
        - {name: amount, type: float64, nullable: true}
    status: deprecated
    replaced_by: source.new_source
  new_source:
    type: parquet
    location: new.parquet
    grain: [id]
    schema:
      fields:
        - {name: id, type: utf8, nullable: false}
        - {name: amount, type: float64, nullable: true}
dimensions:
  old_dimension:
    source: old_source
    column: amount
    data_type: float
    status: deprecated
    replaced_by: dimension.new_dimension
  new_dimension:
    source: new_source
    column: amount
    data_type: float
facts:
  old_fact:
    source: old_source
    expression: old_source.amount
    data_type: decimal
    status: deprecated
    replaced_by: fact.new_fact
  new_fact:
    source: new_source
    expression: new_source.amount
    data_type: decimal
measures:
  old_measure:
    fact: old_fact
    aggregation: sum
    status: deprecated
    replaced_by: measure.new_measure
  new_measure:
    fact: new_fact
    aggregation: sum
metrics:
  old_rate:
    expression: old_measure
    measures: [old_measure]
    status: deprecated
    replaced_by: metric.new_rate
  new_rate:
    expression: new_measure
    measures: [new_measure]
relationships:
  old_relationship:
    source: old_source
    target: new_source
    type: one_to_one
    source_column: id
    target_column: id
    status: deprecated
    replaced_by: relationship.new_relationship
  new_relationship:
    source: old_source
    target: new_source
    type: one_to_one
    source_column: id
    target_column: id
"""


def _catalog_with_deprecations(tmp_path: Path) -> Path:
    return _write(tmp_path, _DEPRECATED_CATALOG_YAML)


def test_catalog_parses_deprecation_metadata(tmp_path: Path) -> None:
    layer = SemanticLayer.load(_catalog_with_deprecations(tmp_path))

    # source
    assert layer.source("old_source").status is SemanticStatus.DEPRECATED
    assert layer.source("old_source").replaced_by == "source.new_source"
    assert layer.source("new_source").status is SemanticStatus.ACTIVE
    assert layer.source("new_source").replaced_by is None
    # dimension
    assert layer.dimension("old_dimension").status is SemanticStatus.DEPRECATED
    assert layer.dimension("old_dimension").replaced_by == "dimension.new_dimension"
    assert layer.dimension("new_dimension").status is SemanticStatus.ACTIVE
    assert layer.dimension("new_dimension").replaced_by is None
    # fact
    assert layer.fact("old_fact").status is SemanticStatus.DEPRECATED
    assert layer.fact("old_fact").replaced_by == "fact.new_fact"
    assert layer.fact("new_fact").status is SemanticStatus.ACTIVE
    assert layer.fact("new_fact").replaced_by is None
    # measure
    assert layer.measure("old_measure").status is SemanticStatus.DEPRECATED
    assert layer.measure("old_measure").replaced_by == "measure.new_measure"
    assert layer.measure("new_measure").status is SemanticStatus.ACTIVE
    assert layer.measure("new_measure").replaced_by is None
    # metric (the brief's example)
    assert layer.metric("old_rate").status is SemanticStatus.DEPRECATED
    assert layer.metric("old_rate").replaced_by == "metric.new_rate"
    assert layer.metric("new_rate").status is SemanticStatus.ACTIVE
    assert layer.metric("new_rate").replaced_by is None
    # relationship
    assert layer.relationship("old_relationship").status is SemanticStatus.DEPRECATED
    assert (
        layer.relationship("old_relationship").replaced_by
        == "relationship.new_relationship"
    )
    assert layer.relationship("new_relationship").status is SemanticStatus.ACTIVE
    assert layer.relationship("new_relationship").replaced_by is None


def _minimal_metric_catalog(metric_extra: str) -> str:
    """A minimal valid catalog with one metric; inject extra metric fields.

    ``metric_extra`` is interpolated verbatim into the metric body so each
    metadata-failure test can target a single malformed field on a metric.
    """
    return (
        "version: 1\nname: minimal\ndata_sources:\n"
        "  orders:\n    type: parquet\n    location: orders.parquet\n    grain: [id]\n"
        "    schema:\n      fields:\n"
        "        - {name: id, type: utf8, nullable: false}\n"
        "        - {name: amount, type: float64, nullable: true}\n"
        "facts:\n  amount:\n    source: orders\n    expression: orders.amount\n"
        "    data_type: decimal\n"
        "measures:\n  total:\n    fact: amount\n    aggregation: sum\n"
        "metrics:\n  rate:\n    expression: total\n    measures: [total]\n"
        f"    {metric_extra}\n"
    )


def test_catalog_rejects_unknown_status(tmp_path: Path) -> None:
    path = _write(tmp_path, _minimal_metric_catalog("status: archived"))
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.rate.status" and "unsupported status" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_rejects_non_string_replaced_by(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _minimal_metric_catalog("status: deprecated\n    replaced_by: 5"),
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.rate.replaced_by"
        and "replaced_by must be a string" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_rejects_replaced_by_on_active_object(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _minimal_metric_catalog("status: active\n    replaced_by: metric.other"),
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.rate.replaced_by" and "deprecated" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_rejects_null_status(tmp_path: Path) -> None:
    # An explicitly-supplied YAML null is a malformed status value, not an
    # absent field: only a missing ``status`` key receives the active default.
    path = _write(tmp_path, _minimal_metric_catalog("status: null"))
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.rate.status"
        and "status must be a string" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_rejects_null_replaced_by(tmp_path: Path) -> None:
    # An explicitly-supplied YAML null is a malformed replacement value, not
    # an absent field: only a missing ``replaced_by`` key receives the None
    # default.
    path = _write(
        tmp_path,
        _minimal_metric_catalog("status: deprecated\n    replaced_by: null"),
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert any(
        issue.path == "metrics.rate.replaced_by"
        and "replaced_by must be a string" in issue.message
        for issue in caught.value.issues
    )


def test_catalog_defaults_missing_status_to_active(valid_catalog_path: Path) -> None:
    # A catalog that declares no deprecation metadata must resolve every
    # semantic object to ACTIVE with no replacement target.
    layer = SemanticLayer.load(valid_catalog_path)
    for obj in layer.semantic_objects().values():
        assert obj.status is SemanticStatus.ACTIVE
        assert obj.replaced_by is None


def test_deprecated_catalog_is_still_a_valid_model(tmp_path: Path) -> None:
    # A catalog that does carry deprecation metadata must not produce any
    # model-rule issues: the deprecation fields are orthogonal to declaration
    # validation.
    from selayer.catalog import collect_model_issues

    layer = SemanticLayer.load(_catalog_with_deprecations(tmp_path))
    assert collect_model_issues(layer) == ()
