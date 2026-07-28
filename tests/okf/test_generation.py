from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.okf import OkfBundle


@pytest.fixture
def ecommerce_layer(valid_catalog_path: Path) -> SemanticLayer:
    return SemanticLayer.load(valid_catalog_path)


def test_generate_metric_concept(ecommerce_layer: SemanticLayer) -> None:
    bundle = OkfBundle.from_layer(
        ecommerce_layer,
        generated_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )

    concept = bundle.concepts["metrics/gross_margin"]
    assert concept.frontmatter == {
        "type": "Selayer Metric",
        "title": "Gross margin",
        "description": "Gross margin ratio",
        "selayer_id": "metric.gross_margin",
        "generated": {
            "by": "process:selayer-okf",
            "at": "2026-07-27T12:00:00Z",
        },
        "status": "stable",
    }
    assert concept.relative_path.as_posix() == "metrics/gross_margin.md"
    assert [section.title for section in concept.sections] == [
        "Catalog Definition",
        "Usage Guidance",
        "Examples",
        "Caveats",
        "Related Concepts",
    ]
    assert (
        "(total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)"
        in concept.sections[0].content
    )


def test_generation_without_timestamp_is_deterministic(
    ecommerce_layer: SemanticLayer,
) -> None:
    first = OkfBundle.from_layer(ecommerce_layer)
    second = OkfBundle.from_layer(ecommerce_layer)

    assert first.concepts == second.concepts
    assert first.concepts["metrics/gross_margin"].frontmatter["generated"] == {
        "by": "process:selayer-okf"
    }


def test_projection_contains_every_semantic_object(
    ecommerce_layer: SemanticLayer,
) -> None:
    bundle = OkfBundle.from_layer(ecommerce_layer)

    assert tuple(bundle.concepts) == (
        "dimensions/order_date",
        "dimensions/product_category",
        "facts/item_cost",
        "facts/item_revenue",
        "measures/total_item_cost",
        "measures/total_item_revenue",
        "metrics/gross_margin",
        "relationships/product_order_items",
        "sources/order_items",
        "sources/orders",
        "sources/products",
    )
    assert (
        "Physical type: `parquet`"
        in bundle.concepts["sources/order_items"].sections[0].content
    )
    assert (
        "Grain: `order_id`, `product_id`"
        in bundle.concepts["sources/order_items"].sections[0].content
    )
    assert (
        "Column: `category`"
        in bundle.concepts["dimensions/product_category"].sections[0].content
    )
    assert (
        "Expression: `order_items.quantity * products.cost`"
        in bundle.concepts["facts/item_cost"].sections[0].content
    )
    assert (
        "Aggregation: `sum`"
        in bundle.concepts["measures/total_item_cost"].sections[0].content
    )
    assert (
        "Source: `products.id`"
        in bundle.concepts["relationships/product_order_items"].sections[0].content
    )
    assert (
        "Target: `order_items.product_id`"
        in bundle.concepts["relationships/product_order_items"].sections[0].content
    )


def test_write_creates_progressive_indexes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"

    OkfBundle.from_layer(ecommerce_layer).write(destination)

    assert (destination / "metrics" / "gross_margin.md").is_file()
    root_index = (destination / "_index.md").read_text(encoding="utf-8")
    assert "# Metrics" in root_index
    assert "[Gross margin](metrics/gross_margin.md)" in root_index
    metric_index = (destination / "metrics" / "_index.md").read_text(encoding="utf-8")
    assert "[Gross margin](gross_margin.md)" in metric_index
    assert "\r\n" not in root_index
    assert (destination / "_change_log.md").read_text(encoding="utf-8") == (
        "# Change Log\n"
    )


def test_write_refuses_to_replace_existing_bundle(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    destination.mkdir()
    (destination / "notes.md").write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="use sync"):
        OkfBundle.from_layer(ecommerce_layer).write(destination)

    assert (destination / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_generate_maps_descriptions_only_when_requested(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    concise = OkfBundle.generate(ecommerce_layer, tmp_path / "concise")
    descriptive = OkfBundle.generate(
        ecommerce_layer,
        tmp_path / "descriptive",
        include_descriptive=True,
    )

    assert "description" not in concise.concepts["metrics/gross_margin"].frontmatter
    assert descriptive.concepts["metrics/gross_margin"].frontmatter["description"] == (
        "Gross margin ratio"
    )
    assert "description" not in descriptive.concepts["sources/orders"].frontmatter


def test_generate_preserves_append_only_change_log_on_regeneration(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    change_log = destination / "_change_log.md"
    original = "# Change Log\n\n## 2026-07-27\n\n- Reviewed catalog.\n"
    change_log.write_bytes(original.encode())
    concept = destination / "metrics" / "gross_margin.md"
    concept.write_text("stale generated output", encoding="utf-8")

    OkfBundle.generate(ecommerce_layer, destination)

    assert change_log.read_bytes() == original.encode()
    assert concept.read_text(encoding="utf-8").startswith("---\ntype: Selayer Metric\n")
    assert not tuple(destination.rglob("*.tmp"))
    loaded = OkfBundle.load(destination, layer=ecommerce_layer)
    assert loaded.concepts["metrics/gross_margin"]


def test_regeneration_removes_stale_generated_concepts(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    without_metrics = replace(ecommerce_layer, metrics={})
    curated = destination / "metrics" / "business_notes.md"
    curated.write_text("human-authored guidance", encoding="utf-8")

    OkfBundle.generate(without_metrics, destination)

    assert not (destination / "metrics" / "gross_margin.md").exists()
    assert not (destination / "metrics" / "_index.md").exists()
    assert curated.read_text(encoding="utf-8") == "human-authored guidance"


@pytest.mark.parametrize(
    "content",
    [
        "Ownership marker example:\n\ngenerated:\n  by: process:selayer-okf\n",
        "```yaml\ngenerated:\n  by: process:selayer-okf\n```\n",
    ],
    ids=["prose", "fenced-code-block"],
)
def test_regeneration_preserves_curated_marker_examples(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    content: str,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    curated = destination / "metrics" / "ownership_notes.md"
    curated.write_text(content, encoding="utf-8")

    OkfBundle.generate(ecommerce_layer, destination)

    assert curated.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "frontmatter",
    [
        "generated:\n  by: process:selayer-okf\n  broken: [",
        '"generated:\n  by: process:selayer-okf"',
    ],
    ids=["malformed", "non-mapping"],
)
def test_regeneration_preserves_invalid_frontmatter_with_marker(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    frontmatter: str,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    curated = destination / "metrics" / "ownership_notes.md"
    content = f"---\n{frontmatter}\n---\n# Curated notes\n"
    curated.write_text(content, encoding="utf-8")

    OkfBundle.generate(ecommerce_layer, destination)

    assert curated.read_text(encoding="utf-8") == content


def test_regeneration_deletes_stale_frontmatter_owned_markdown(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    stale = destination / "metrics" / "retired.md"
    stale.write_text(
        "---\ngenerated:\n  by: process:selayer-okf\n---\n# Retired metric\n",
        encoding="utf-8",
    )

    OkfBundle.generate(ecommerce_layer, destination)

    assert not stale.exists()


def test_atomic_replacement_keeps_previous_file_when_replace_fails(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    concept = destination / "metrics" / "gross_margin.md"
    concept.write_text("previous bytes", encoding="utf-8")
    original_replace = Path.replace

    def fail_gross_margin(temporary: Path, target: Path) -> Path:
        if temporary.name == "gross_margin.md.tmp":
            raise OSError("injected replacement failure")
        return original_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", fail_gross_margin)

    with pytest.raises(OSError, match="injected replacement failure"):
        OkfBundle.generate(ecommerce_layer, destination)

    assert concept.read_text(encoding="utf-8") == "previous bytes"
    assert not (concept.parent / "gross_margin.md.tmp").exists()


def test_generation_never_accesses_source_data(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import duckdb
    import polars as pl

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("OKF generation attempted data access")

    monkeypatch.setattr(duckdb, "connect", fail)
    monkeypatch.setattr(pl, "read_parquet", fail)

    OkfBundle.generate(ecommerce_layer, tmp_path / "knowledge")
