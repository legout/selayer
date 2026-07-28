import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.model import Dimension
from selayer.okf import OkfBundle


@pytest.fixture
def ecommerce_layer(valid_catalog_path: Path) -> SemanticLayer:
    return SemanticLayer.load(valid_catalog_path)


def test_generate_metric_concept(ecommerce_layer: SemanticLayer) -> None:
    bundle = OkfBundle.from_layer(
        ecommerce_layer,
        generated_at=datetime(
            2026,
            7,
            27,
            12,
            0,
            tzinfo=timezone.utc,  # noqa: UP017
        ),
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
            "fingerprint": "14c8c17f850c1e0b580437e2a03de7c325cee3ebb36502570ae53e1297195d6f",
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
    generated = first.concepts["metrics/gross_margin"].frontmatter["generated"]
    assert generated["by"] == "process:selayer-okf"
    assert re.fullmatch(r"[0-9a-f]{64}", generated["fingerprint"])


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


def test_semantic_object_named_index_is_rejected_before_reserved_file_collision(
    ecommerce_layer: SemanticLayer,
) -> None:
    layer = replace(
        ecommerce_layer,
        dimensions={
            **ecommerce_layer.dimensions,
            "index": Dimension(
                name="index", source="orders", column="id", data_type="string"
            ),
        },
    )

    with pytest.raises(ValueError, match="dimension.index.*reserved.*index.md"):
        OkfBundle.from_layer(layer)


def test_write_creates_progressive_indexes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"

    OkfBundle.from_layer(ecommerce_layer).write(destination)

    assert (destination / "metrics" / "gross_margin.md").is_file()
    root_index = (destination / "index.md").read_text(encoding="utf-8")
    assert "# Metrics" in root_index
    assert "[Gross margin](metrics/gross_margin.md)" in root_index
    metric_index = (destination / "metrics" / "index.md").read_text(encoding="utf-8")
    assert "[Gross margin](gross_margin.md)" in metric_index
    assert "\r\n" not in root_index
    assert (destination / "log.md").read_text(encoding="utf-8") == "# Change Log\n"


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


@pytest.mark.parametrize("operation", ["generate", "write", "sync"])
def test_bundle_mutation_refuses_nonexistent_destination_beneath_symlinked_ancestor(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    operation: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    symlinked_ancestor = tmp_path / "linked"
    symlinked_ancestor.symlink_to(external, target_is_directory=True)
    destination = symlinked_ancestor / "knowledge"
    bundle = OkfBundle.from_layer(ecommerce_layer)

    with pytest.raises(FileExistsError, match="symbolic link"):
        if operation == "generate":
            OkfBundle.generate(ecommerce_layer, destination)
        elif operation == "write":
            bundle.write(destination)
        else:
            bundle.sync(destination)

    assert list(external.iterdir()) == []
    assert symlinked_ancestor.is_symlink()


@pytest.mark.parametrize("operation", ["generate", "write"])
def test_new_bundle_refuses_symlinked_destination_root_without_external_changes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    operation: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    destination = tmp_path / "knowledge"
    destination.symlink_to(external, target_is_directory=True)

    with pytest.raises(FileExistsError, match="symbolic link"):
        bundle = OkfBundle.from_layer(ecommerce_layer)
        if operation == "generate":
            OkfBundle.generate(ecommerce_layer, destination)
        else:
            bundle.write(destination)

    assert list(external.iterdir()) == []
    assert destination.is_symlink()


@pytest.mark.parametrize("operation", ["generate", "write"])
def test_new_bundle_refuses_symlinked_kind_directory_without_external_changes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    operation: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    curated = external / "gross_margin.md"
    original = b"finance-curated metric\n"
    curated.write_bytes(original)
    destination = tmp_path / "knowledge"
    destination.mkdir()
    (destination / "metrics").symlink_to(external, target_is_directory=True)

    with pytest.raises(FileExistsError, match="symbolic link"):
        bundle = OkfBundle.from_layer(ecommerce_layer)
        if operation == "generate":
            OkfBundle.generate(ecommerce_layer, destination)
        else:
            bundle.write(destination)

    assert curated.read_bytes() == original
    assert sorted(path.name for path in external.iterdir()) == ["gross_margin.md"]
    assert (destination / "metrics").is_symlink()


@pytest.mark.parametrize("operation", ["generate", "write"])
def test_new_bundle_refuses_broken_symlink_without_writing(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    operation: str,
) -> None:
    destination = tmp_path / "knowledge"
    destination.mkdir()
    broken = destination / "broken"
    broken.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(FileExistsError, match="symbolic link"):
        bundle = OkfBundle.from_layer(ecommerce_layer)
        if operation == "generate":
            OkfBundle.generate(ecommerce_layer, destination)
        else:
            bundle.write(destination)

    assert broken.is_symlink()
    assert list(destination.iterdir()) == [broken]


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


def test_generate_refuses_populated_destination_without_changing_any_bytes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.generate(ecommerce_layer, destination)
    concept = destination / "metrics" / "gross_margin.md"
    curated = "\n# Usage Guidance\n\nKeep this finance-approved wording.\n"
    concept.write_text(
        concept.read_text(encoding="utf-8") + curated,
        encoding="utf-8",
    )
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    with pytest.raises(FileExistsError, match="contains files; use sync"):
        OkfBundle.generate(ecommerce_layer, destination)

    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert curated.encode() in concept.read_bytes()


def test_generate_returns_loaded_bundle_bound_to_layer(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    destination.mkdir()

    bundle = OkfBundle.generate(ecommerce_layer, destination)

    assert bundle.root == destination
    assert bundle.layer is ecommerce_layer
    assert bundle.context_for(["metric.gross_margin"], include_linked=False).items[
        0
    ].semantic_refs == ("metric.gross_margin",)


def test_atomic_generation_removes_temporary_file_when_replace_fails(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "knowledge"
    concept = destination / "metrics" / "gross_margin.md"
    original_replace = Path.replace

    def fail_gross_margin(temporary: Path, target: Path) -> Path:
        if temporary.name == "gross_margin.md.tmp":
            raise OSError("injected replacement failure")
        return original_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", fail_gross_margin)

    with pytest.raises(OSError, match="injected replacement failure"):
        OkfBundle.generate(ecommerce_layer, destination)

    assert not concept.exists()
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
