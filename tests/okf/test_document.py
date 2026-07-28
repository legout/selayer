from pathlib import Path

from selayer.okf.document import parse_concept, render_concept


def test_parse_and_render_preserves_extensions_and_curated_sections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metrics" / "gross_margin.md"
    path.parent.mkdir()
    path.write_text(
        "---\n"
        "type: Selayer Metric\n"
        "selayer_id: metric.gross_margin\n"
        "custom_owner: finance\n"
        "---\n\n"
        "# Catalog Definition\n\nGenerated definition.\n\n"
        "# Usage Guidance\n\nUse item revenue.\n",
        encoding="utf-8",
    )

    concept = parse_concept(path, tmp_path)

    assert concept.concept_id == "metrics/gross_margin"
    assert concept.frontmatter["custom_owner"] == "finance"
    assert [section.title for section in concept.sections] == [
        "Catalog Definition",
        "Usage Guidance",
    ]
    assert "Use item revenue." in render_concept(concept)


def test_heading_inside_fence_is_not_a_section(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Reference\n---\n\n# Examples\n\n```text\n# Not a section\n```\n",
        encoding="utf-8",
    )

    concept = parse_concept(path, tmp_path)

    assert [section.title for section in concept.sections] == ["Examples"]
    assert "# Not a section" in concept.sections[0].content


def test_tilde_fence_and_indented_fence_protect_headings(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Reference\n---\n\n"
        "Before.\n\n# Examples\n\n   ~~~~text\n# Still code\n   ~~~~\n",
        encoding="utf-8",
    )

    concept = parse_concept(path, tmp_path)

    assert concept.preamble == "Before."
    assert [section.title for section in concept.sections] == ["Examples"]
    assert "# Still code" in concept.sections[0].content


def test_parse_render_parse_round_trip_preserves_document_model(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Domain Concept\ncustom:\n  enabled: true\n---\n\n"
        "Introductory context.\n\n# Meaning\n\nCurated text.\n",
        encoding="utf-8",
    )

    original = parse_concept(path, tmp_path)
    path.write_text(render_concept(original), encoding="utf-8")
    reparsed = parse_concept(path, tmp_path)

    assert reparsed == original
