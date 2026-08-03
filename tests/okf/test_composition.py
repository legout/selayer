"""Tests for authored Reference and overlay composition loading."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from selayer.catalog import SemanticLayer
from selayer.okf import OkfBundle, composition
from selayer.okf.composition import load_overlays, load_references
from selayer.okf.document import OkfDocumentError
from selayer.okf.model import OkfConcept, OkfIssue, OkfSection, OkfValidationError

REFERENCE = (
    "---\ntype: Reference\ntitle: Guide\nstatus: stable\n---\n\n# Guidance\nText.\n"
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


def test_empty_inputs_load_cleanly(tmp_path: Path, valid_layer: SemanticLayer) -> None:
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
        "---\ntype: Reference\ntitle: Guide\nsecret: [hunter2\n---\n\n# Guidance\n",
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
    root_fd = os.open(str(references), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # The safe reader must refuse the symlink outright (replacement
        # detected) and never follow it to read the secret-bearing target.
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(
                link, references, root_fd, "link.md", composition._MAX_TOTAL_BYTES
            )
        assert "replaced" in str(exc.value)
        assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)
    finally:
        os.close(root_fd)


def test_reference_file_replaced_with_symlink_after_walk_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    references = _references(tmp_path)
    target = tmp_path / "secret.md"
    target.write_text("SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8")
    guide = references / "guide.md"
    _write(guide, REFERENCE)
    real_walk = composition._walk_inputs

    def swapping_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Swap the regular file for an escaping symlink AFTER the walk's lstat
        # validated it as a regular file (the TOCTOU window) but before read.
        guide.unlink()
        guide.symlink_to(target)
        return files, root_fd

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

    def swapping_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        overlay.unlink()
        overlay.symlink_to(target)
        return files, root_fd

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


# ---------------------------------------------------------------------------
# High-finding follow-up: close root/intermediate-directory symlink TOCTOU and
# enforce per-file and aggregate byte limits on the opened descriptor.
# ---------------------------------------------------------------------------


def test_safe_read_text_pins_root_against_symlink_swap(tmp_path: Path) -> None:
    # A pinned root fd must keep reads inside the original root even if the
    # root path is swapped for a symlink after the walk: the read must return
    # the original content and never follow the symlink to the secret target.
    root = tmp_path / "root"
    root.mkdir()
    (root / "guide.md").write_text("original-content", encoding="utf-8")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "guide.md").write_text("SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8")
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        shutil.move(str(root), str(tmp_path / "moved"))
        root.symlink_to(secret_dir)
        text, _ = composition._safe_read_text(
            root / "guide.md",
            root,
            root_fd,
            "guide.md",
            composition._MAX_TOTAL_BYTES,
        )
        assert text == "original-content"
        assert "SECRET-VALUE-DO-NOT-LEAK" not in text
    finally:
        os.close(root_fd)


def test_safe_read_text_rejects_intermediate_directory_symlink_swap(
    tmp_path: Path,
) -> None:
    # An intermediate directory swapped for a symlink must be refused at the
    # no-follow component traversal, never followed into the secret tree.
    root = tmp_path / "root"
    root.mkdir()
    (root / "metrics").mkdir()
    (root / "metrics" / "gross_margin.md").write_text("original", encoding="utf-8")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "gross_margin.md").write_text(
        "SECRET-VALUE-DO-NOT-LEAK", encoding="utf-8"
    )
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        shutil.rmtree(root / "metrics")
        (root / "metrics").symlink_to(secret_dir)
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(
                root / "metrics" / "gross_margin.md",
                root,
                root_fd,
                "metrics/gross_margin.md",
                composition._MAX_TOTAL_BYTES,
            )
        assert "replaced" in str(exc.value)
        assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)
    finally:
        os.close(root_fd)


def test_reference_root_swapped_to_symlink_after_walk_reads_pinned_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    references = _references(tmp_path)
    _write(references / "guide.md", REFERENCE)
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "guide.md").write_text(
        "---\ntype: Reference\ntitle: SECRET-VALUE-DO-NOT-LEAK\n---\n\n# x\n",
        encoding="utf-8",
    )
    real_walk = composition._walk_inputs

    def swapping_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Move the real root away and replace its path with a symlink to the
        # secret dir AFTER the root fd is pinned to the original root inode.
        shutil.move(str(root), str(tmp_path / "moved"))
        root.symlink_to(secret_dir)
        return files, root_fd

    monkeypatch.setattr(composition, "_walk_inputs", swapping_walk)
    loaded = load_references(references)
    guide = loaded["references/guide.md"]
    # The pinned fd reads the original guide.md, never the symlink target.
    assert guide.frontmatter["title"] == "Guide"
    assert "SECRET-VALUE-DO-NOT-LEAK" not in str(guide.frontmatter)


def test_overlay_intermediate_directory_swapped_to_symlink_after_walk_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlays = _overlays_root(tmp_path)
    overlay = overlays / "metrics" / "gross_margin.md"
    _write(overlay, OVERLAY)
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "gross_margin.md").write_text(
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Usage Guidance\nSECRET-VALUE-DO-NOT-LEAK\n",
        encoding="utf-8",
    )
    real_walk = composition._walk_inputs

    def swapping_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Replace the intermediate "metrics" directory with a symlink to a
        # secret tree AFTER the walk enumerated the real file.
        shutil.rmtree(overlays / "metrics")
        (overlays / "metrics").symlink_to(secret_dir)
        return files, root_fd

    monkeypatch.setattr(composition, "_walk_inputs", swapping_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_overlays(overlays, valid_layer)
    assert "replaced" in str(exc.value)
    assert "SECRET-VALUE-DO-NOT-LEAK" not in str(exc.value)


def test_file_grown_after_walk_is_rejected(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_FILE_BYTES", 80)
    overlays = _overlays_root(tmp_path)
    overlay = overlays / "metrics" / "gross_margin.md"
    content = "---\nselayer_id: metric.gross_margin\n---\n\n# Usage Guidance\nx\n"
    assert len(content.encode()) <= 80
    _write(overlay, content)
    real_walk = composition._walk_inputs

    def growing_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Grow the file past the per-file cap AFTER the walk's lstat accepted
        # its small size; the post-open fstat on the descriptor must reject it.
        _write(overlay, content + "y" * 200)
        return files, root_fd

    monkeypatch.setattr(composition, "_walk_inputs", growing_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_overlays(overlays, valid_layer)
    assert "exceeds" in str(exc.value)


def test_total_grown_after_walk_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition, "_MAX_TOTAL_BYTES", 120)
    references = _references(tmp_path)
    small = "---\ntype: Reference\ntitle: A\n---\n\n# Guidance\nx\n"
    a = references / "a.md"
    b = references / "b.md"
    _write(a, small)
    _write(b, small)
    assert 2 * len(small.encode()) <= 120
    real_walk = composition._walk_inputs

    def growing_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Grow one file AFTER the walk so the read-time aggregate total exceeds
        # the cap while each file stays under _MAX_FILE_BYTES (aggregate fires).
        _write(b, small + "z" * 200)
        return files, root_fd

    monkeypatch.setattr(composition, "_walk_inputs", growing_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_references(references)
    assert "total input exceeds" in str(exc.value)


# ---------------------------------------------------------------------------
# Medium-finding follow-up: walk every directory entry (not only *.md) so a
# symlinked/special intermediate directory is rejected rather than silently
# skipped or followed; non-md regular files are still silently ignored.
# ---------------------------------------------------------------------------


def test_overlay_behind_symlinked_directory_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # rglob('*.md') silently skips a valid file behind a symlinked directory;
    # a full entry walk must reject the symlinked directory explicitly.
    overlays = _overlays_root(tmp_path)
    real = tmp_path / "real" / "metrics"
    real.mkdir(parents=True)
    _write(real / "gross_margin.md", OVERLAY)
    (overlays / "metrics").symlink_to(real)
    with pytest.raises(FileExistsError):
        load_overlays(overlays, valid_layer)


def test_reference_behind_symlinked_directory_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    real = tmp_path / "real"
    real.mkdir(parents=True)
    _write(real / "guide.md", REFERENCE)
    (references / "linked").symlink_to(real)
    with pytest.raises(FileExistsError):
        load_references(references)


def test_special_file_in_subdirectory_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # A special file nested in a subdirectory must be rejected during the walk,
    # not silently skipped or only checked at the top level.
    overlays = _overlays_root(tmp_path)
    (overlays / "metrics").mkdir(parents=True)
    os.mkfifo(overlays / "metrics" / "pipe.md")
    with pytest.raises(OkfDocumentError):
        load_overlays(overlays, valid_layer)


def test_non_md_regular_file_is_silently_skipped(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # Non-md regular files are not candidates and must not be rejected (the
    # reserved/non-md semantics are preserved by the full entry walk).
    overlays = _overlays_root(tmp_path)
    _write(overlays / "metrics" / "gross_margin.md", OVERLAY)
    _write(overlays / "notes.txt", "ignore me")
    _write(overlays / "metrics" / "README", "ignore me too")
    loaded = load_overlays(overlays, valid_layer)
    assert loaded[0].selayer_id == "metric.gross_margin"


def test_symlinked_non_md_entry_is_rejected_not_skipped(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # A symlink whose name does not end in .md is still rejected: the lstat
    # check runs before the .md suffix filter, so no symlinked entry is ever
    # silently skipped.
    overlays = _overlays_root(tmp_path)
    _write(overlays / "metrics" / "gross_margin.md", OVERLAY)
    target = tmp_path / "outside"
    target.write_text("secret", encoding="utf-8")
    (overlays / "metrics" / "linked.txt").symlink_to(target)
    with pytest.raises(FileExistsError):
        load_overlays(overlays, valid_layer)


# ---------------------------------------------------------------------------
# Medium-finding follow-up: pass the remaining aggregate byte budget into the
# descriptor read so each read is prospectively capped and no bytes beyond
# _MAX_TOTAL_BYTES are consumed before a rejection.
# ---------------------------------------------------------------------------


def test_safe_read_text_prospectively_caps_to_remaining_budget(
    tmp_path: Path,
) -> None:
    references = _references(tmp_path)
    guide = references / "guide.md"
    _write(guide, "x" * 200)
    root_fd = os.open(str(references), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # A budget smaller than both the file size and _MAX_FILE_BYTES must be
        # rejected as an aggregate overflow before any byte beyond the budget
        # is consumed.
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(guide, references, root_fd, "guide.md", 10)
        assert "total input exceeds" in str(exc.value)
    finally:
        os.close(root_fd)


def test_safe_read_text_per_file_cap_precedes_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(composition, "_MAX_FILE_BYTES", 8)
    references = _references(tmp_path)
    guide = references / "guide.md"
    _write(guide, "x" * 200)
    root_fd = os.open(str(references), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # The per-file cap is the tighter defect and is reported first, even
        # though the remaining budget would also reject the file.
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(
                guide,
                references,
                root_fd,
                "guide.md",
                composition._MAX_TOTAL_BYTES,
            )
        assert "exceeds" in str(exc.value)
        assert "total input exceeds" not in str(exc.value)
    finally:
        os.close(root_fd)


def test_aggregate_budget_caps_read_prospectively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The remaining aggregate budget caps each read: two files whose combined
    # walk-time size fits, but the second is grown past the remaining budget
    # (while staying under the per-file cap) so the read-time prospective cap
    # rejects it before the aggregate overflows.
    monkeypatch.setattr(composition, "_MAX_TOTAL_BYTES", 80)
    references = _references(tmp_path)
    a = references / "a.md"
    b = references / "b.md"
    _write(a, "a" * 50)
    _write(b, "b" * 20)
    real_walk = composition._walk_inputs

    def growing_walk(root: Path) -> tuple[list[Path], int]:
        files, root_fd = real_walk(root)
        # Grow b past the remaining budget (80 - 50 = 30) but under the per-file
        # cap; the prospective read cap rejects it as a total overflow.
        _write(b, "b" * 200)
        return files, root_fd

    monkeypatch.setattr(composition, "_walk_inputs", growing_walk)
    with pytest.raises(OkfDocumentError) as exc:
        load_references(references)
    assert "total input exceeds" in str(exc.value)


# ---------------------------------------------------------------------------
# Hardening: bound YAML alias expansion / depth / node count, reject cyclic
# alias graphs, so a small frontmatter blob cannot force the downstream freeze
# (which copies every alias reference) into unbounded or exponential work.
# ---------------------------------------------------------------------------


def test_overlay_cyclic_sources_via_alias_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # A self-referential alias makes ``sources`` a cycle; the freeze that
    # OkfConcept.create runs would recurse without bound, so the composed node
    # graph must be rejected before construction with a fixed, safe message.
    overlays = _overlays_root(tmp_path)
    _write(
        overlays / "metrics" / "gross_margin.md",
        "---\nselayer_id: metric.gross_margin\nsources: &s [*s]\n"
        "---\n\n# Usage Guidance\nx\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "cyclic YAML structure" in messages


def test_reference_cyclic_frontmatter_via_alias_is_rejected(tmp_path: Path) -> None:
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntitle: Guide\nloop: &l [*l]\n---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    assert any("cyclic YAML structure" in issue.message for issue in exc.value.issues)


def test_overlay_alias_expansion_dag_is_rejected(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    # A compact fan-out alias DAG: each level aliases the previous twice, so a
    # few hundred bytes unfold into an exponentially large tree (2^k freeze
    # calls). The expansion budget must reject it before construction/freeze.
    overlays = _overlays_root(tmp_path)
    lines = ["---", "selayer_id: metric.gross_margin", "a: &a 'x'"]
    prev = "a"
    # 14 doubling levels -> 2^15 - 1 (~32k) > _MAX_YAML_EXPANSION (10_000).
    for index in range(14):
        name = f"n{index}"
        lines.append(f"{name}: &{name} [*{prev}, *{prev}]")
        prev = name
    lines += ["---", "", "# Usage Guidance", "x"]
    _write(overlays / "metrics" / "gross_margin.md", "\n".join(lines) + "\n")
    with pytest.raises(OkfValidationError) as exc:
        load_overlays(overlays, valid_layer)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "too complex" in messages


def test_deeply_nested_yaml_frontmatter_is_rejected(tmp_path: Path) -> None:
    # A document nested far past _MAX_YAML_DEPTH can overflow the stack during
    # composition; the rejection must be a fixed, secret-safe message rather
    # than an uncaught RecursionError.
    references = _references(tmp_path)
    nested = "nested: " + "[" * 5000 + "1" + "]" * 5000
    _write(
        references / "guide.md",
        f"---\ntype: Reference\ntitle: Guide\n{nested}\n---\n\n# Guidance\n",
    )
    with pytest.raises(OkfValidationError) as exc:
        load_references(references)
    messages = " ".join(issue.message for issue in exc.value.issues)
    assert "invalid YAML frontmatter" in messages


def test_reference_small_alias_dag_is_accepted(tmp_path: Path) -> None:
    # A small, non-cyclic alias DAG (a scalar referenced twice) is legitimate
    # and must be accepted, not rejected by the expansion/complexity bound.
    references = _references(tmp_path)
    _write(
        references / "guide.md",
        "---\ntype: Reference\ntitle: Guide\nshared: &s value\ncopy: *s\n"
        "---\n\n# Guidance\n",
    )
    guide = load_references(references)["references/guide.md"]
    assert guide.frontmatter["copy"] == "value"
    assert guide.frontmatter["shared"] == "value"


# ---------------------------------------------------------------------------
# Hardening: a file that grows *during* the bounded read must be rejected by
# the post-read fstat so a capped read never returns truncated content. Per-file
# precedence over the aggregate budget is preserved.
# ---------------------------------------------------------------------------


def test_safe_read_text_post_read_per_file_growth_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file fits both pre-read caps, then grows past _MAX_FILE_BYTES during
    # the read. The post-read fstat must reject it, and the per-file message
    # wins precedence over the (also-exhausted) aggregate budget.
    monkeypatch.setattr(composition, "_MAX_FILE_BYTES", 50)
    monkeypatch.setattr(composition, "_MAX_TOTAL_BYTES", 30)
    references = _references(tmp_path)
    guide = references / "guide.md"
    _write(guide, "b" * 20)  # 20 bytes: passes both pre-read caps
    root_fd = os.open(str(references), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    real_read = os.read
    grew = {"done": False}

    def growing_read(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if data and not grew["done"]:
            grew["done"] = True
            with open(guide, "a", encoding="utf-8") as handle:
                handle.write("z" * 200)
        return data

    monkeypatch.setattr(os, "read", growing_read)
    try:
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(guide, references, root_fd, "guide.md", 30)
        assert "exceeds" in str(exc.value)
        assert "total input exceeds" not in str(exc.value)
    finally:
        os.close(root_fd)


def test_safe_read_text_post_read_aggregate_growth_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Growth during the read past the remaining aggregate budget (but under the
    # per-file cap) must be rejected by the post-read fstat as a total overflow
    # rather than accepted as truncated content.
    monkeypatch.setattr(composition, "_MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(composition, "_MAX_TOTAL_BYTES", 80)
    references = _references(tmp_path)
    guide = references / "guide.md"
    _write(guide, "b" * 20)  # 20 bytes; budget 30 -> pre-read passes
    root_fd = os.open(str(references), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    real_read = os.read
    grew = {"done": False}

    def growing_read(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if data and not grew["done"]:
            grew["done"] = True
            with open(guide, "a", encoding="utf-8") as handle:
                handle.write("z" * 200)
        return data

    monkeypatch.setattr(os, "read", growing_read)
    try:
        with pytest.raises(OkfDocumentError) as exc:
            composition._safe_read_text(guide, references, root_fd, "guide.md", 30)
        assert "total input exceeds" in str(exc.value)
    finally:
        os.close(root_fd)


# ---------------------------------------------------------------------------
# Hardening: a cyclic or malformed overlay sources/stale_after value must be
# adapted into a controlled domain error, never a raw OkfMetadataError.
# ---------------------------------------------------------------------------


def test_validate_bound_overlay_adapts_cyclic_sources_error(
    valid_layer: SemanticLayer,
) -> None:
    # A cyclic sources value would surface a raw OkfMetadataError from the
    # freeze inside OkfConcept.create. _validate_bound_overlay must adapt it
    # into a controlled OkfIssue and never raise the raw exception.
    cyclic: list[object] = []
    cyclic.append(cyclic)
    frontmatter: dict[str, object] = {
        "selayer_id": "metric.gross_margin",
        "sources": cyclic,
    }
    issues: list[OkfIssue] = []
    result = composition._validate_bound_overlay(
        "metric.gross_margin",
        valid_layer,
        PurePosixPath("metrics/gross_margin.md"),
        "metrics/gross_margin.md",
        frontmatter,
        (),
        issues,
    )
    assert result is False
    assert issues, "expected a controlled issue for cyclic sources"
    messages = " ".join(issue.message for issue in issues)
    assert "cyclic or malformed" in messages


# ---------------------------------------------------------------------------
# Task 8: fresh atomic composition via ``OkfBundle.build``.
# ---------------------------------------------------------------------------


def _authored_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Write the valid Task 7 reference and overlay under ``tmp_path``."""
    references = tmp_path / "references"
    references.mkdir()
    (references / "guide.md").write_text(REFERENCE, encoding="utf-8")
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    (overlays / "gross_margin.md").write_text(OVERLAY, encoding="utf-8")
    return references, tmp_path / "overlays"


def _section(concept: OkfConcept, title: str) -> OkfSection:
    return next(item for item in concept.sections if item.title == title)


def _no_staging_remnants(parent: Path, dest_name: str = "knowledge") -> bool:
    return not any(
        entry.name.startswith(f".{dest_name}.okf-build") for entry in parent.iterdir()
    )


def test_build_composes_reference_and_overlay(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"

    bundle = OkfBundle.build(
        valid_layer,
        output,
        references_dir=references,
        overlays_dir=overlays,
    )

    metric = bundle.concepts["metrics/gross_margin"]
    assert _section(metric, "Usage Guidance").content == "Use at item grain."
    assert _section(metric, "Caveats").content == "Do not mix grains."
    # Generated ownership is preserved: the overlay merge leaves the generated
    # Catalog Definition byte-for-byte equal to the fresh projection.
    generated_metric = OkfBundle.from_layer(
        valid_layer, include_descriptive=False
    ).concepts["metrics/gross_margin"]
    assert (
        _section(metric, "Catalog Definition").content
        == _section(generated_metric, "Catalog Definition").content
    )
    assert (
        metric.frontmatter["generated"]["fingerprint"]
        == generated_metric.frontmatter["generated"]["fingerprint"]
    )
    assert "references/guide" in bundle.concepts
    assert bundle.root == output
    assert bundle.layer is valid_layer
    # The published bundle reloads cleanly with no diagnostics at all.
    assert OkfBundle.load(output, layer=valid_layer).diagnostics == ()
    assert _no_staging_remnants(tmp_path)


def test_build_without_authored_inputs_writes_a_clean_generated_bundle(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    output = tmp_path / "knowledge"

    bundle = OkfBundle.build(valid_layer, output)

    assert "metrics/gross_margin" in bundle.concepts
    assert OkfBundle.load(output, layer=valid_layer).diagnostics == ()
    assert _no_staging_remnants(tmp_path)


def test_build_rejects_populated_destination_before_staging(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    output = tmp_path / "knowledge"
    output.mkdir()
    (output / "stale.md").write_text("pre-existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        OkfBundle.build(valid_layer, output)

    # The pre-existing file is untouched and no staging directory is created.
    assert (output / "stale.md").read_text() == "pre-existing"
    assert _no_staging_remnants(tmp_path)


def test_build_accepts_existing_empty_destination(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"
    output.mkdir()  # an existing but empty destination is accepted

    bundle = OkfBundle.build(
        valid_layer, output, references_dir=references, overlays_dir=overlays
    )

    assert bundle.root == output
    assert (output / "metrics" / "gross_margin.md").is_file()
    assert (output / "references" / "guide.md").is_file()
    assert _no_staging_remnants(tmp_path)


def test_build_overlay_failure_leaves_destination_untouched(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references = tmp_path / "references"
    references.mkdir()
    (references / "guide.md").write_text(REFERENCE, encoding="utf-8")
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    # An overlay binding an unknown selayer_id fails in the loader before any
    # staging directory is created.
    (overlays / "ghost.md").write_text(
        "---\nselayer_id: metric.ghost\n---\n\n# Usage Guidance\nx\n",
        encoding="utf-8",
    )
    output = tmp_path / "knowledge"

    with pytest.raises(OkfValidationError):
        OkfBundle.build(
            valid_layer,
            output,
            references_dir=references,
            overlays_dir=tmp_path / "overlays",
        )

    assert not output.exists()
    assert _no_staging_remnants(tmp_path)


def test_build_strict_load_failure_removes_staging(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"
    original_load = OkfBundle.load

    def failing_load(
        path: str | Path, *, layer: SemanticLayer | None = None, strict: bool = True
    ) -> OkfBundle:
        # Fail only the staging strict load (a sibling staging directory).
        if Path(path).name.startswith("."):
            raise OkfValidationError(
                (OkfIssue("metrics/gross_margin.md", "injected integrity failure"),)
            )
        return original_load(path, layer=layer, strict=strict)

    monkeypatch.setattr(OkfBundle, "load", failing_load)

    with pytest.raises(OkfValidationError):
        OkfBundle.build(
            valid_layer, output, references_dir=references, overlays_dir=overlays
        )

    assert not output.exists()
    assert _no_staging_remnants(tmp_path)


def test_build_publish_failure_leaves_absent_destination_unchanged(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"
    original_replace = Path.replace

    def fail_publish(self: Path, target: Path) -> Path:
        if Path(target) == output:
            raise OSError("injected publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        OkfBundle.build(
            valid_layer, output, references_dir=references, overlays_dir=overlays
        )

    # The destination was absent before the build and stays absent.
    assert not output.exists()
    assert _no_staging_remnants(tmp_path)


def test_build_publish_failure_leaves_empty_destination_unchanged(
    tmp_path: Path,
    valid_layer: SemanticLayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"
    output.mkdir()  # an existing empty destination
    original_replace = Path.replace

    def fail_publish(self: Path, target: Path) -> Path:
        if Path(target) == output:
            raise OSError("injected publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        OkfBundle.build(
            valid_layer, output, references_dir=references, overlays_dir=overlays
        )

    # The empty destination is preserved exactly.
    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert _no_staging_remnants(tmp_path)


def test_build_leaves_no_staging_directory_on_success(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    references, overlays = _authored_inputs(tmp_path)
    output = tmp_path / "knowledge"

    OkfBundle.build(
        valid_layer, output, references_dir=references, overlays_dir=overlays
    )

    assert output.is_dir()
    assert _no_staging_remnants(tmp_path)


def test_build_rejects_overlay_link_to_missing_concept(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    overlays = tmp_path / "overlays" / "metrics"
    overlays.mkdir(parents=True)
    # The loader accepts this path (it checks structure, not existence) and
    # defers cross-input link existence to composition, where the combined
    # link validation must reject the dangling reference.
    (overlays / "gross_margin.md").write_text(
        "---\nselayer_id: metric.gross_margin\n---\n\n"
        "# Related Concepts\n- [ghost](../facts/ghost.md)\n",
        encoding="utf-8",
    )
    output = tmp_path / "knowledge"

    with pytest.raises(OkfValidationError):
        OkfBundle.build(valid_layer, output, overlays_dir=tmp_path / "overlays")

    assert not output.exists()
    assert _no_staging_remnants(tmp_path)


# ---------------------------------------------------------------------------
# Task 8 review fix: a destination is valid only if it is an actually empty
# directory. Reject any non-directory filesystem object and any directory that
# contains any entry at all -- including an empty subdirectory -- before any
# staging directory is created.
# ---------------------------------------------------------------------------


def test_build_rejects_destination_with_empty_subdirectory(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    output = tmp_path / "knowledge"
    output.mkdir()
    (output / "metrics").mkdir()  # an empty subdirectory, not a file

    with pytest.raises(FileExistsError):
        OkfBundle.build(valid_layer, output)

    # The empty subdirectory is untouched and no staging directory is created.
    assert (output / "metrics").is_dir()
    assert list(output.iterdir()) == [output / "metrics"]
    assert _no_staging_remnants(tmp_path)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda path: path.write_text("not a directory"), id="file"),
        pytest.param(lambda path: os.mkfifo(path), id="special-file"),
    ],
)
def test_build_rejects_non_directory_destination(
    tmp_path: Path, valid_layer: SemanticLayer, make: Callable[[Path], object]
) -> None:
    output = tmp_path / "knowledge"
    make(output)

    with pytest.raises(FileExistsError):
        OkfBundle.build(valid_layer, output)

    # The non-directory object is untouched and no staging directory is created.
    assert output.exists()
    assert not output.is_dir()
    assert _no_staging_remnants(tmp_path)


# ---------------------------------------------------------------------------
# Final-fix regression: a canonical system ancestor symlink (e.g. macOS
# ``/var -> /private/var``, the lexical root of ``$TMPDIR`` that ``mktemp -d``
# returns) sits ABOVE the destination's immediate parent. The build mutation
# boundary (destination component + its parent) stays symlink-free, so the
# atomic fresh build must succeed and publish under the resolved real parent.
# A strict lexical-ancestor walk previously rejected this exact shape.
# ---------------------------------------------------------------------------


def test_build_allows_canonical_symlinked_ancestor(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    real_root = tmp_path / "private-root"
    real_root.mkdir()
    (real_root / "work").mkdir()
    canonical = tmp_path / "canonical"  # stands in for /var -> /private/var
    canonical.symlink_to(real_root, target_is_directory=True)
    output = canonical / "work" / "knowledge"  # parent is the real "work"

    bundle = OkfBundle.build(valid_layer, output)

    assert (output / "metrics" / "gross_margin.md").is_file()
    assert bundle.root == output
    # The published bytes land under the real ancestor the symlink resolves to.
    assert (real_root / "work" / "knowledge" / "metrics" / "gross_margin.md").is_file()
    assert _no_staging_remnants(canonical / "work")
