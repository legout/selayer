"""Tests for authored Reference and overlay composition loading."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from selayer.catalog import SemanticLayer
from selayer.okf import composition
from selayer.okf.composition import load_overlays, load_references
from selayer.okf.document import OkfDocumentError
from selayer.okf.model import OkfValidationError

REFERENCE = (
    "---\ntype: Reference\ntitle: Guide\nstatus: stable\n---\n\n"
    "# Guidance\nText.\n"
)
OVERLAY = (
    "---\nselayer_id: metric.gross_margin\n---\n\n"
    "# Usage Guidance\nUse at item grain.\n\n"
    "# Caveats\nDo not mix grains.\n"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _references(tmp_path: Path) -> Path:
    root = tmp_path / "references"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _overlays_root(tmp_path: Path) -> Path:
    root = tmp_path / "overlays"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Step 1: valid inputs
# ---------------------------------------------------------------------------


def test_loads_valid_reference_and_overlay(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references = tmp_path / "references"
    references.mkdir()
    (references / "guide.md").write_text(REFERENCE, encoding="utf-8")
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    (overlays / "gross_margin.md").write_text(OVERLAY, encoding="utf-8")
    loaded_references = load_references(references)
    loaded_overlays = load_overlays(tmp_path / "overlays", valid_layer)
    assert tuple(loaded_references) == ("references/guide.md",)
    assert loaded_overlays[0].selayer_id == "metric.gross_margin"


def test_overlay_is_immutable_and_carries_sections(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    content = (
        "---\n"
        "selayer_id: metric.gross_margin\n"
        "sources:\n- resource: s3://example/policy\n"
        "stale_after: 2099-01-01\n"
        "---\n\n"
        "# Usage Guidance\nUse at item grain.\n\n"
        "# Examples\nExample.\n\n"
        "# Caveats\nDo not mix grains.\n\n"
        "# Related Concepts\n- [rev](../facts/item_revenue.md)\n"
    )
    overlays = _overlays_root(tmp_path)
    _write(overlays / "metrics" / "gross_margin.md", content)
    loaded = load_overlays(overlays, valid_layer)
    assert len(loaded) == 1
    overlay = loaded[0]
    assert overlay.selayer_id == "metric.gross_margin"
    assert tuple(section.title for section in overlay.sections) == (
        "Usage Guidance",
        "Examples",
        "Caveats",
        "Related Concepts",
    )
    sources = cast("list[dict[str, object]]", overlay.frontmatter["sources"])
    assert sources[0]["resource"] == "s3://example/policy"
    assert overlay.frontmatter["stale_after"] == date(2099, 1, 1)
    # frontmatter is deeply immutable
    with pytest.raises(TypeError):
        overlay.frontmatter["selayer_id"] = "x"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        overlay.selayer_id = "y"  # type: ignore[misc]


def test_overlays_sorted_by_relative_path(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "facts" / "item_revenue.md",
        "---\nselayer_id: fact.item_revenue\n---\n\n# Usage Guidance\nx\n",
    )
    _write(overlays / "metrics" / "gross_margin.md", OVERLAY)
    loaded = load_overlays(overlays, valid_layer)
    assert [o.relative_path.as_posix() for o in loaded] == [
        "facts/item_revenue.md",
        "metrics/gross_margin.md",
    ]


def test_empty_inputs_load_cleanly(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references = _references(tmp_path)
    overlays = _overlays_root(tmp_path)
    assert dict(load_references(references)) == {}
    assert load_overlays(overlays, valid_layer) == ()


# ---------------------------------------------------------------------------
# Step 2: rejection tests
# ---------------------------------------------------------------------------


def test_reference_with_selayer_id_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntitle: Guide\nselayer_id: metric.gross_margin\n"
        "---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError):
        load_references(references)


def test_reference_missing_title_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(references / "guide.md", "---\ntype: Reference\n---\n\n# Guidance\n")
    with pytest.raises(OkfValidationError):
        load_references(references)


def test_reference_missing_type_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(references / "guide.md", "---\ntitle: Guide\n---\n\n# Guidance\n")
    with pytest.raises(OkfValidationError):
        load_references(references)


def test_reference_invalid_status_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntitle: Guide\nstatus: bogus\n---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError):
        load_references(references)


@pytest.mark.parametrize("name", ["index.md", "log.md"])
def test_reserved_reference_path_is_rejected(tmp_path: Path, name: str) -> None:
    references = _references(tmp_path)
    _write(references / name, REFERENCE)
    with pytest.raises(OkfValidationError):
        load_references(references)


@pytest.mark.parametrize("name", ["index.md", "log.md"])
def test_reserved_overlay_path_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer, name: str
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(overlays / "metrics" / name, OVERLAY)
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_symbolic_link_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text("hi", encoding="utf-8")
    (references / "link.md").symlink_to(target)
    with pytest.raises(FileExistsError):
        load_references(references)


def test_path_escape_is_rejected(tmp_path: Path, valid_layer: SemanticLayer) -> None:
    overlays = _overlays_root(tmp_path)
    # A symlink that escapes the input root is the only realistic escape vector
    # for rglob results; lstat rejects it before reading.
    target = tmp_path / "outside.md"
    target.write_text(OVERLAY, encoding="utf-8")
    (overlays / "escape.md").symlink_to(target)
    with pytest.raises(FileExistsError):
        load_overlays(overlays, valid_layer)


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    (references / "bad.md").write_bytes(b"---\ntitle: \xff\xfe\n---\n")
    with pytest.raises(OkfDocumentError):
        load_references(references)


def test_special_file_is_rejected(tmp_path: Path, valid_layer: SemanticLayer) -> None:
    overlays = _overlays_root(tmp_path)
    os.mkfifo(overlays / "pipe.md")
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_overlay_missing_selayer_id_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nsources: []\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_unknown_selayer_id_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "ghost.md",
        "---\nselayer_id: metric.ghost\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_path_id_mismatch_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "wrong.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_duplicate_overlay_ids_are_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(overlays / "metrics" / "gross_margin.md", OVERLAY)
    _write(
        overlays / "metrics" / "again.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_duplicate_overlay_section_heading_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Usage Guidance\na\n\n# Usage Guidance\nb\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_duplicate_frontmatter_key_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nselayer_id: metric.gross_margin\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_unknown_frontmatter_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\ncustom: x\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "verified:\n- by: human:a\n  at: 2024-01-01T00:00:00Z\n",
        "title: X\n",
        "description: d\n",
        "generated:\n  by: x\n",
        "status: stable\n",
    ],
)
def test_overlay_rejects_generated_or_concept_only_frontmatter(
    tmp_path: Path, valid_layer: SemanticLayer, frontmatter: str
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        f"---\nselayer_id: metric.gross_margin\n{frontmatter}---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_rejects_catalog_definition_section(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Usage Guidance\nx\n\n# Catalog Definition\nforged\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_preamble_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "preamble text is forbidden\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_self_link_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Related Concepts\n- [self](gross_margin.md)\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_duplicate_related_link_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Related Concepts\n"
        "- [a](../facts/item_revenue.md)\n"
        "- [b](../facts/item_revenue.md)\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_broken_escaping_link_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Related Concepts\n- [x](../../etc/passwd.md)\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_external_link_is_allowed(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Related Concepts\n- [ext](https://example.com/x)\n",
    )
    loaded = load_overlays(overlays, valid_layer)
    assert loaded[0].selayer_id == "metric.gross_margin"


def test_too_many_files_is_rejected(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_FILES", 1)
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\nx\n",
    )
    _write(
        overlays / "metrics" / "total_item_revenue.md",
        "---\nselayer_id: measure.total_item_revenue\n---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_file_too_big_is_rejected(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_FILE_BYTES", 8)
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\n"
        + "x" * 200
        + "\n",
    )
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_total_input_too_big_is_rejected(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_TOTAL_BYTES", 8)
    overlays = _overlays_root(tmp_path)
    _write(overlays / "a.md", "aaaaaaaa")
    _write(overlays / "b.md", "aaaaaaaa")
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_too_many_links_in_one_file_is_rejected(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_LINKS_PER_FILE", 2)
    overlays = _overlays_root(tmp_path)
    body = "# Related Concepts\n" + "\n".join(
        f"- [x](../facts/f{i}.md)" for i in range(3)
    )
    _write(
        overlays / "metrics" / "gross_margin.md",
        f"---\nselayer_id: metric.gross_margin\n---\n\n{body}\n",
    )
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_overlay_invalid_sources_field_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nsources: not-a-list\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)


def test_overlay_invalid_stale_after_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nstale_after: not-a-date\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError):
        load_overlays(overlays, valid_layer)
