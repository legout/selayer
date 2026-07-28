from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.okf import OkfBundle


@pytest.fixture
def ecommerce_layer(valid_catalog_path: Path) -> SemanticLayer:
    return SemanticLayer.load(valid_catalog_path)


@pytest.fixture
def changed_ecommerce_layer(valid_catalog_path: Path) -> SemanticLayer:
    changed_path = valid_catalog_path.with_name("changed-layer.yaml")
    changed = (
        valid_catalog_path.read_text(encoding="utf-8")
        .replace(
            "description: Product category",
            "description: Product family",
        )
        .replace(
            "(total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)",
            "(total_item_revenue - total_item_cost) / total_item_revenue",
        )
    )
    changed_path.write_text(changed, encoding="utf-8")
    return SemanticLayer.load(changed_path)


def test_sync_preserves_curated_sections_and_extensions(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "status: stable",
        "status: reviewed\ncustom_owner: finance",
    ).replace(
        "# Usage Guidance\n",
        "# Usage Guidance\n\nUse item revenue as the denominator.\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    report = OkfBundle.from_layer(
        ecommerce_layer,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
    ).sync(destination)
    updated = metric_path.read_text(encoding="utf-8")

    assert "custom_owner: finance" in updated
    assert "status: reviewed" in updated
    assert "Use item revenue as the denominator." in updated
    assert "at: '2026-07-28T00:00:00Z'" in updated
    assert report.conflicts == ()


def test_sync_refreshes_generated_description_only(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)

    report = OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    dimension = (destination / "dimensions" / "product_category.md").read_text(
        encoding="utf-8"
    )
    assert "description: Product family" in dimension
    assert "dimensions/product_category.md" in report.written


def test_sync_leaves_duplicate_generated_sections_unchanged(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    original = metric_path.read_text(encoding="utf-8")
    metric_path.write_text(
        original + "\n# Catalog Definition\n\nSecond generated section.\n",
        encoding="utf-8",
    )
    unsafe = metric_path.read_bytes()

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert report.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_bytes() == unsafe


def test_sync_leaves_missing_generated_section_unchanged(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    unsafe = metric_path.read_text(encoding="utf-8").replace(
        "# Catalog Definition", "# Human Definition"
    )
    metric_path.write_text(unsafe, encoding="utf-8")

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert report.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_text(encoding="utf-8") == unsafe


def test_changed_definition_removes_current_verification(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        metric_path.read_text(encoding="utf-8").replace(
            "status: stable",
            "status: stable\nverified:\n  by: human:finance\n  at: '2026-07-27T12:00:00Z'",
        ),
        encoding="utf-8",
    )

    OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert "verified:" not in metric_path.read_text(encoding="utf-8")


def test_provenance_refresh_preserves_current_verification(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        metric_path.read_text(encoding="utf-8").replace(
            "status: stable",
            "status: stable\nverified:\n  by: human:finance",
        ),
        encoding="utf-8",
    )

    OkfBundle.from_layer(
        ecommerce_layer,
        generated_at=datetime(2026, 7, 28, tzinfo=UTC),
    ).sync(destination)

    assert "verified:" in metric_path.read_text(encoding="utf-8")


def test_sync_reports_stale_concepts_as_unchanged_orphans(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    original = metric_path.read_bytes()
    without_metrics = SemanticLayer.load(
        _catalog_without_metrics(tmp_path, ecommerce_layer)
    )

    report = OkfBundle.from_layer(without_metrics).sync(destination)

    assert report.orphaned == ("metrics/gross_margin.md",)
    assert "metrics/gross_margin.md" in report.unchanged
    assert metric_path.read_bytes() == original


def test_conflict_does_not_block_other_safe_updates(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    metric_path.write_text(
        metric_path.read_text(encoding="utf-8")
        + "\n# Catalog Definition\n\nDuplicate.\n",
        encoding="utf-8",
    )

    report = OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert "metrics/gross_margin.md" in report.conflicts
    assert "dimensions/product_category.md" in report.written
    assert report.written == tuple(sorted(report.written))


def test_sync_writes_new_concepts_and_regenerates_indexes(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    without_metrics = SemanticLayer.load(
        _catalog_without_metrics(tmp_path, ecommerce_layer)
    )
    OkfBundle.from_layer(without_metrics).write(destination)

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert "metrics/gross_margin.md" in report.written
    assert (destination / "metrics" / "gross_margin.md").is_file()
    assert "[Gross margin](metrics/gross_margin.md)" in (
        destination / "_index.md"
    ).read_text(encoding="utf-8")
    assert (destination / "metrics" / "_index.md").is_file()


def test_dry_run_reports_changes_without_writing_any_file(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    report = OkfBundle.from_layer(changed_ecommerce_layer).sync(
        destination, dry_run=True
    )

    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert "metrics/gross_margin.md" in report.written
    assert after == before
    assert not tuple(destination.rglob("*.tmp"))


def test_sync_preserves_append_only_change_log(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    change_log = destination / "_change_log.md"
    original = b"# Change Log\n\n## 2026-07-28\n\n- Human review completed.\n"
    change_log.write_bytes(original)

    OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert change_log.read_bytes() == original


def test_no_op_sync_is_byte_stable_and_reports_concepts_unchanged(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*.md")
    }

    report = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*.md")
    }
    assert report.written == ()
    assert len(report.unchanged) == len(ecommerce_layer.semantic_objects())
    assert after == before


def test_successful_sync_leaves_no_temporary_files(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)

    OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert list(destination.rglob("*.tmp")) == []


def test_failed_atomic_replace_preserves_original(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
    changed_ecommerce_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    original = metric_path.read_bytes()
    real_replace = Path.replace

    def fail_metric_replace(source: Path, target: Path) -> Path:
        if target == metric_path:
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_metric_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        OkfBundle.from_layer(changed_ecommerce_layer).sync(destination)

    assert metric_path.read_bytes() == original
    assert list(destination.rglob("*.tmp")) == []


def test_invalid_expected_concept_is_a_deterministic_conflict(
    tmp_path: Path,
    ecommerce_layer: SemanticLayer,
) -> None:
    destination = tmp_path / "knowledge"
    OkfBundle.from_layer(ecommerce_layer).write(destination)
    metric_path = destination / "metrics" / "gross_margin.md"
    unsafe = b"---\ntitle: [\n---\n# Catalog Definition\n"
    metric_path.write_bytes(unsafe)

    first = OkfBundle.from_layer(ecommerce_layer).sync(destination, dry_run=True)
    second = OkfBundle.from_layer(ecommerce_layer).sync(destination)

    assert first.conflicts == second.conflicts == ("metrics/gross_margin.md",)
    assert metric_path.read_bytes() == unsafe


def _catalog_without_metrics(tmp_path: Path, layer: SemanticLayer) -> Path:
    del layer
    source = next(tmp_path.glob("layer.yaml"))
    content = source.read_text(encoding="utf-8")
    metrics_start = content.index("metrics:\n")
    relationships_start = content.index("relationships:\n")
    path = tmp_path / "without-metrics.yaml"
    path.write_text(
        content[:metrics_start] + "metrics: {}\n" + content[relationships_start:],
        encoding="utf-8",
    )
    return path
