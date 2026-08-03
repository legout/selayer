"""Tests for the catalog static-validation adapter and coded ``CatalogIssue``.

``validate_catalog`` adapts the existing catalog loader into the immutable
verification report model: a successful load yields a passed static outcome
alongside the loaded layer, while a ``CatalogValidationError`` is mapped to a
failed outcome whose diagnostics carry the catalog's stable issue codes. The
``CatalogIssue`` type gains a ``code`` field while keeping its old positional
``(path, message)`` construction for backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

from selayer.catalog import CatalogIssue
from selayer.verification import validate_catalog


def test_catalog_issue_keeps_old_positional_construction() -> None:
    issue = CatalogIssue("metrics.margin", "unknown measure")
    assert issue.path == "metrics.margin"
    assert issue.message == "unknown measure"
    assert issue.code == "catalog.invalid"


def test_validate_catalog_returns_layer_and_passed_report(
    valid_catalog_path: Path,
) -> None:
    result = validate_catalog(valid_catalog_path)
    assert result.layer is not None
    assert result.report.passed
    assert result.report.outcomes[0].check_id == "catalog.static"


def test_validate_catalog_returns_coded_failure(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n", encoding="utf-8")
    result = validate_catalog(path)
    assert result.layer is None
    assert not result.report.passed
    assert result.report.diagnostics[0].code == "catalog.version.unsupported"
