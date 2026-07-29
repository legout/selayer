from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from selayer.okf import OkfBundle, OkfConcept
from selayer.okf.compatibility import effective_generated_at, effective_sources
from selayer.okf.model import OkfSection


def _concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
    )


def test_effective_generated_at_prefers_generated_at() -> None:
    stamped = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)

    concept = _concept(
        {
            "type": "Metric",
            "generated": {"by": "process:selayer-okf", "at": stamped},
            "timestamp": "2026-01-01",
        }
    )

    assert effective_generated_at(concept) is stamped


def test_effective_generated_at_falls_back_to_legacy_timestamp() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    assert effective_generated_at(concept) == "2026-01-01"


def test_effective_generated_at_returns_none_without_metadata() -> None:
    assert effective_generated_at(_concept({"type": "Metric"})) is None


def test_malformed_generated_does_not_fall_back_to_timestamp() -> None:
    concept = _concept(
        {"type": "Metric", "generated": "not-a-mapping", "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_generated_mapping_without_at_returns_none_without_fallback() -> None:
    concept = _concept(
        {"type": "Metric", "generated": {"by": "process:x"}, "timestamp": "2026-01-01"}
    )

    assert effective_generated_at(concept) is None


def test_effective_generated_at_does_not_mutate_frontmatter() -> None:
    concept = _concept({"type": "Metric", "timestamp": "2026-01-01"})

    effective_generated_at(concept)

    assert dict(concept.frontmatter) == {"type": "Metric", "timestamp": "2026-01-01"}


def _section_concept(
    frontmatter: dict[str, object], sections: tuple[OkfSection, ...]
) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
        sections=sections,
    )


def test_effective_sources_return_frontmatter_entries_when_present() -> None:
    concept = _concept(
        {
            "type": "Metric",
            "sources": [
                {"id": "policy", "resource": "https://example.com/policy"},
                {"resource": "urn:warehouse:margin"},
            ],
        }
    )

    assert effective_sources(concept) == (
        concept.frontmatter["sources"][0],
        concept.frontmatter["sources"][1],
    )


def test_effective_sources_omit_malformed_frontmatter_entries() -> None:
    raw = [
        {"id": "policy", "resource": "https://example.com/policy"},
        {"id": "broken"},
        "not-a-mapping",
        {"resource": "   "},
        {"resource": "urn:warehouse:margin"},
    ]
    concept = _concept({"type": "Metric", "sources": raw})

    assert [source["resource"] for source in effective_sources(concept)] == [
        "https://example.com/policy",
        "urn:warehouse:margin",
    ]


def test_effective_sources_returns_empty_for_malformed_container() -> None:
    concept = _concept({"type": "Metric", "sources": "not-a-list"})

    assert effective_sources(concept) == ()


def test_effective_sources_fall_back_to_citations_when_sources_absent() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "- [Margin Policy](https://example.com/policy)\n"
                "* urn:warehouse:margin\n",
            ),
        ),
    )

    assert [dict(source) for source in effective_sources(concept)] == [
        {"title": "Margin Policy", "resource": "https://example.com/policy"},
        {"resource": "urn:warehouse:margin"},
    ]


def test_citations_ignore_nested_prose_and_non_list_lines() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "Intro prose is ignored.\n"
                "- [Policy](https://example.com/policy)\n"
                "  - nested item ignored\n"
                "plain text ignored\n"
                "- urn:warehouse:margin\n",
            ),
        ),
    )

    assert [source["resource"] for source in effective_sources(concept)] == [
        "https://example.com/policy",
        "urn:warehouse:margin",
    ]


def test_citations_empty_resource_links_are_ignored() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (OkfSection("Citations", "- [Broken]()\n- urn:ok\n"),),
    )

    assert [source["resource"] for source in effective_sources(concept)] == ["urn:ok"]


def test_citations_preserve_order_and_duplicate_resources() -> None:
    concept = _section_concept(
        {"type": "Metric"},
        (
            OkfSection(
                "Citations",
                "- urn:first\n- [Second](urn:second)\n- urn:first\n",
            ),
        ),
    )

    assert [source["resource"] for source in effective_sources(concept)] == [
        "urn:first",
        "urn:second",
        "urn:first",
    ]


def test_effective_sources_returns_empty_without_sources_or_citations() -> None:
    concept = _concept({"type": "Metric"})

    assert effective_sources(concept) == ()


def test_effective_sources_does_not_mutate_frontmatter_or_sections() -> None:
    concept = _section_concept(
        {"type": "Metric", "sources": [{"resource": "urn:ok"}]},
        (OkfSection("Citations", "- urn:cite\n"),),
    )

    effective_sources(concept)

    assert [dict(source) for source in concept.frontmatter["sources"]] == [
        {"resource": "urn:ok"}
    ]
    assert [section.content for section in concept.sections] == ["- urn:cite\n"]


def test_legacy_timestamp_and_citations_survive_load_write_load(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "concept.md").write_text(
        "---\n"
        "type: Metric\n"
        "timestamp: 2026-01-01\n"
        "---\n"
        "# Meaning\n\nAuthored body.\n\n"
        "# Citations\n\n"
        "- [Policy](https://example.com/policy)\n",
        encoding="utf-8",
    )

    loaded = OkfBundle.load(source_root)
    output_root = tmp_path / "output"
    loaded.write(output_root)
    reloaded = OkfBundle.load(output_root)

    rewritten = (output_root / "concept.md").read_text(encoding="utf-8")
    assert "timestamp: 2026-01-01" in rewritten
    assert "# Citations" in rewritten
    assert "[Policy](https://example.com/policy)" in rewritten
    # The document loader parses YAML ``2026-01-01`` into ``datetime.date``;
    # ``effective_generated_at`` returns the frozen frontmatter value
    # unchanged (Task 4 contract), so the round-tripped value is a date.
    assert effective_generated_at(reloaded.concepts["concept"]) == date(2026, 1, 1)
    assert [
        source["resource"]
        for source in effective_sources(reloaded.concepts["concept"])
    ] == ["https://example.com/policy"]
