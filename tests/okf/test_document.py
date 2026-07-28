import operator
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
import yaml

from selayer.okf import OkfBundle
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


def test_frontmatter_is_deeply_immutable_and_renders_as_plain_yaml(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\n"
        "type: Metric\n"
        "custom:\n"
        "  nested:\n"
        "    entries:\n"
        "      - label: retained\n"
        "sources:\n"
        "  - resource: https://example.com/policy\n"
        "    details:\n"
        "      tags: [policy]\n"
        "generated:\n"
        "  by: process:selayer-okf\n"
        "  details:\n"
        "    steps: [parse]\n"
        "verified:\n"
        "  - by: human:owner\n"
        "    at: '2026-07-27T15:00:00Z'\n"
        "    evidence:\n"
        "      reviewers: [owner]\n"
        "---\n",
        encoding="utf-8",
    )

    concept = OkfBundle.load(tmp_path).concepts["concept"]

    assert isinstance(concept.frontmatter, MappingProxyType)
    assert isinstance(concept.frontmatter["custom"], MappingProxyType)
    assert isinstance(concept.frontmatter["custom"]["nested"], MappingProxyType)
    assert isinstance(concept.frontmatter["custom"]["nested"]["entries"], tuple)
    assert isinstance(
        concept.frontmatter["custom"]["nested"]["entries"][0], MappingProxyType
    )
    assert isinstance(concept.frontmatter["sources"], tuple)
    assert isinstance(concept.frontmatter["sources"][0], MappingProxyType)
    assert isinstance(concept.frontmatter["sources"][0]["details"], MappingProxyType)
    assert isinstance(concept.frontmatter["sources"][0]["details"]["tags"], tuple)
    assert isinstance(concept.frontmatter["generated"], MappingProxyType)
    assert isinstance(concept.frontmatter["generated"]["details"], MappingProxyType)
    assert isinstance(concept.frontmatter["generated"]["details"]["steps"], tuple)
    assert isinstance(concept.frontmatter["verified"], tuple)
    assert isinstance(concept.frontmatter["verified"][0], MappingProxyType)
    assert isinstance(concept.frontmatter["verified"][0]["evidence"], MappingProxyType)
    assert isinstance(
        concept.frontmatter["verified"][0]["evidence"]["reviewers"], tuple
    )
    with pytest.raises(TypeError):
        operator.setitem(cast(Any, concept.frontmatter["custom"]), "new", "value")
    with pytest.raises(TypeError):
        operator.setitem(cast(Any, concept.frontmatter["sources"]), 0, {})

    rendered = render_concept(concept)
    rendered_frontmatter = yaml.safe_load(rendered.split("---\n", 2)[1])
    assert type(rendered_frontmatter) is dict
    assert type(rendered_frontmatter["custom"]["nested"]["entries"]) is list
    assert type(rendered_frontmatter["sources"]) is list
    assert type(rendered_frontmatter["sources"][0]) is dict
    assert type(rendered_frontmatter["generated"]) is dict
    assert type(rendered_frontmatter["verified"]) is list
    assert rendered_frontmatter["custom"]["nested"]["entries"] == [
        {"label": "retained"}
    ]

    path.write_text(rendered, encoding="utf-8")
    assert parse_concept(path, tmp_path) == concept


def test_yaml_set_extension_is_immutable_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Metric\ncustom_tags: !!set\n  finance:\n  retained:\n---\n",
        encoding="utf-8",
    )

    concept = OkfBundle.load(tmp_path).concepts["concept"]

    assert concept.frontmatter["custom_tags"] == frozenset({"finance", "retained"})
    assert isinstance(concept.frontmatter["custom_tags"], frozenset)
    rendered = render_concept(concept)
    rendered_frontmatter = yaml.safe_load(rendered.split("---\n", 2)[1])
    assert isinstance(rendered_frontmatter, dict)
    assert type(rendered_frontmatter["custom_tags"]) is set
    assert rendered_frontmatter["custom_tags"] == {"finance", "retained"}

    path.write_text(rendered, encoding="utf-8")
    assert parse_concept(path, tmp_path) == concept


def test_yaml_set_rendering_is_deterministic_across_hash_seeds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concept.md"
    path.write_text(
        "---\ntype: Metric\ncustom_tags: !!set\n"
        "  finance:\n  retained:\n  operations:\n  alpha:\n---\n",
        encoding="utf-8",
    )
    script = (
        "import pathlib, sys; "
        "from selayer.okf.document import parse_concept, render_concept; "
        "path = pathlib.Path(sys.argv[1]); "
        "sys.stdout.write(render_concept(parse_concept(path, path.parent)))"
    )

    rendered = [
        subprocess.run(
            [sys.executable, "-c", script, str(path)],
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            stdout=subprocess.PIPE,
        ).stdout
        for seed in ("1", "2")
    ]

    assert rendered[0] == rendered[1]
    rendered_frontmatter = yaml.safe_load(rendered[0].decode().split("---\n", 2)[1])
    assert isinstance(rendered_frontmatter, dict)
    assert type(rendered_frontmatter["custom_tags"]) is set
    assert rendered_frontmatter["custom_tags"] == {
        "finance",
        "retained",
        "operations",
        "alpha",
    }


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
