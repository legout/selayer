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


# ---------------------------------------------------------------------------
# Review-fix regressions: input-root validation, safe YAML diagnostics,
# recursive duplicate-key composition, and true duplicate-ID tracking.
# ---------------------------------------------------------------------------


def test_reference_root_symlink_is_rejected(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "references"
    link.symlink_to(real_dir)
    with pytest.raises(FileExistsError):
        load_references(link)


def test_overlay_root_symlink_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "overlays"
    link.symlink_to(real_dir)
    with pytest.raises(FileExistsError):
        load_overlays(link, valid_layer)


def test_input_root_regular_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "references"
    root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        load_references(root)


def test_input_root_special_file_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "overlays"
    os.mkfifo(root)
    with pytest.raises(NotADirectoryError):
        load_overlays(root, valid_layer)


def test_overlay_yaml_parse_error_message_is_safe(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # An unterminated flow sequence is a compose (parse) error; the injected
    # secret token must never appear in the resulting diagnostic.
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nsecret: [hunter2\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert "invalid YAML frontmatter" in messages


def test_overlay_yaml_construction_error_message_is_safe(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # An unknown explicit tag composes but fails construction under SafeLoader;
    # the constructor diagnostic (which echoes the tag) must not leak.
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nsecret: !!leaked hunter2\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert "leaked" not in messages
    assert "invalid YAML frontmatter" in messages


def test_reference_yaml_error_message_is_safe(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntitle: Guide\nsecret: [hunter2\n"
        "---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert "invalid YAML frontmatter" in messages


def test_overlay_nested_duplicate_key_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # A duplicate `resource` buried inside a nested sources mapping; a
    # top-level-only check would miss it (SafeLoader would silently overwrite).
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\n"
        "selayer_id: metric.gross_margin\n"
        "sources:\n"
        "- resource: s3://example/a\n"
        "  resource: s3://example/b\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    assert any(
        "duplicate frontmatter key" in issue.message for issue in exc.value.issues
    )


def test_overlay_merge_key_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # A merge key (<<) can mask a duplicate after merge resolution; reject it.
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\n"
        "selayer_id: metric.gross_margin\n"
        "base: &b\n"
        "  resource: s3://example/a\n"
        "override:\n"
        "  <<: *b\n"
        "  resource: s3://example/b\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    assert any(
        "merge keys are not supported" in issue.message for issue in exc.value.issues
    )


def test_reference_duplicate_frontmatter_key_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntype: Guide\ntitle: Guide\n---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    assert any(
        "duplicate frontmatter key" in issue.message for issue in exc.value.issues
    )


def test_reference_nested_duplicate_key_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\n"
        "type: Reference\n"
        "title: Guide\n"
        "sources:\n"
        "- resource: a\n"
        "  resource: b\n"
        "---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    assert any(
        "duplicate frontmatter key" in issue.message for issue in exc.value.issues
    )


def test_duplicate_overlay_id_flagged_even_when_paths_differ(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # Two files bind the same id; neither sits at its canonical path, so path
    # validation alone would not surface the duplicate binding.
    _write(
        overlays / "metrics" / "a.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\na\n",
    )
    _write(
        overlays / "metrics" / "b.md",
        "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\nb\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    duplicate_issues = [
        issue
        for issue in exc.value.issues
        if "duplicate overlay selayer_id" in issue.message
    ]
    assert duplicate_issues, "expected a true duplicate-ID diagnostic"
    assert all("metric.gross_margin" in issue.message for issue in duplicate_issues)


# ---------------------------------------------------------------------------
# TOCTOU hardening: a regular file enumerated by the walk must not be followed
# as a symlink if it is replaced before the read.
# ---------------------------------------------------------------------------


def test_safe_read_text_rejects_symlink_replacement(tmp_path: Path) -> None:
    references = _references(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text("SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8")
    link = references / "link.md"
    link.symlink_to(target)
    # The safe reader must refuse the symlink outright (replacement detected)
    # and never follow it to read the secret-bearing target.
    with pytest.raises(OkfDocumentError) as exc:
        composition._safe_read_text(link, references, "link.md")
    assert "replaced" in str(exc.value)
    assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)


def test_reference_file_replaced_with_symlink_after_walk_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    references = _references(tmp_path)
    target = tmp_path / "secret.md"
    target.write_text("SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8")
    guide = references / "guide.md"
    _write(guide, REFERENCE)
    real_walk = composition._walk_inputs

    def swapping_walk(root: Path) -> list[Path]:
        files = real_walk(root)
        # Swap the regular file for an escaping symlink AFTER the walk's lstat
        # validated it as a regular file (the TOCTOU window) but before read.
        guide.unlink()
        guide.symlink_to(target)
        return files

    monkeypatch.setattr(composition, "_walk_inputs", swapping_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_references(references)
    assert "replaced" in str(exc.value)
    assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)


def test_overlay_file_replaced_with_symlink_after_walk_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlays = _overlays_root(tmp_path)
    target = tmp_path / "secret.md"
    target.write_text("SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8")
    overlay = overlays / "metrics" / "gross_margin.md"
    _write(overlay, OVERLAY)
    real_walk = composition._walk_inputs

    def swapping_walk(root: Path) -> list[Path]:
        files = real_walk(root)
        overlay.unlink()
        overlay.symlink_to(target)
        return files

    monkeypatch.setattr(composition, "_walk_inputs", swapping_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_overlays(overlays, valid_layer)
    assert "replaced" in str(exc.value)
    assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)


# ---------------------------------------------------------------------------
# Secret-safe duplicate-key diagnostics: a key name is attacker-controlled
# and may carry a secret token, so it must never be interpolated.
# ---------------------------------------------------------------------------


def test_overlay_duplicate_key_name_is_not_leaked(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\n"
        "selayer_id: metric.gross_margin\n"
        "secret_token_hunter2: a\n"
        "secret_token_hunter2: b\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert "duplicate frontmatter key" in messages


def test_reference_duplicate_key_name_is_not_leaked(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\n"
        "type: Reference\n"
        "title: Guide\n"
        "secret_token_hunter2: a\n"
        "secret_token_hunter2: b\n"
        "---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert "duplicate frontmatter key" in messages


def test_overlay_nested_duplicate_key_name_is_not_leaked(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = _overlays_root(tmp_path)
    # A duplicate key buried inside a nested mapping; recursive detection must
    # still fire, and the secret-bearing key name must not appear in the fix.
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\n"
        "selayer_id: metric.gross_margin\n"
        "sources:\n"
        "- secret_field_hunter2: a\n"
        "  secret_field_hunter2: b\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "hunter2" not in messages
    assert any(
        "duplicate frontmatter key" in issue.message for issue in exc.value.issues
    )
