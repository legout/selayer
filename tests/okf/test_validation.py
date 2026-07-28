from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.okf import OkfBundle, OkfValidationError


def _write_concept(root: Path, frontmatter: str, body: str = "") -> Path:
    path = root / "concept.md"
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("root_kind", "expected_error"),
    [("missing", FileNotFoundError), ("file", NotADirectoryError)],
)
def test_load_rejects_missing_or_non_directory_root(
    tmp_path: Path, root_kind: str, expected_error: type[OSError]
) -> None:
    root = tmp_path / "bundle"
    if root_kind == "file":
        root.write_text("not a bundle", encoding="utf-8")

    with pytest.raises(expected_error, match="bundle root"):
        OkfBundle.load(root)


def test_load_accepts_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()

    bundle = OkfBundle.load(root)

    assert bundle.concepts == {}
    assert bundle.diagnostics == ()


def test_load_collects_and_sorts_invalid_documents(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\ntitle: Missing type\n---\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(
        "---\ntype: Metric\nstatus: unknown\n---\n", encoding="utf-8"
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert list(caught.value.issues) == sorted(
        caught.value.issues,
        key=lambda issue: (issue.path, issue.message),
    )
    assert {issue.path for issue in caught.value.issues} == {
        "a.md.frontmatter.type",
        "b.md.frontmatter.status",
    }


def test_non_string_status_values_are_reported_and_sorted(tmp_path: Path) -> None:
    (tmp_path / "z-sequence.md").write_text(
        "---\ntype: Metric\nstatus: [draft, stable]\n---\n", encoding="utf-8"
    )
    (tmp_path / "a-mapping.md").write_text(
        "---\ntype: Metric\nstatus: {bad: value}\n---\n", encoding="utf-8"
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert [(issue.path, issue.message) for issue in caught.value.issues] == [
        (
            "a-mapping.md.frontmatter.status",
            "status must be one of: deprecated, draft, stable",
        ),
        (
            "z-sequence.md.frontmatter.status",
            "status must be one of: deprecated, draft, stable",
        ),
    ]


@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        ("type: Metric\ngenerated: []", "concept.md.frontmatter.generated"),
        ("type: Metric\nsources: nope", "concept.md.frontmatter.sources"),
        (
            "type: Metric\nstale_after: not-a-date",
            "concept.md.frontmatter.stale_after",
        ),
        (
            "type: Attested Computation",
            "concept.md.frontmatter.runtime",
        ),
    ],
)
def test_invalid_optional_families_are_rejected(
    tmp_path: Path,
    frontmatter: str,
    issue_path: str,
) -> None:
    _write_concept(tmp_path, frontmatter)
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert issue_path in {issue.path for issue in caught.value.issues}


def test_verified_rejects_empty_list_with_deterministic_issue(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Metric\nverified: []")

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert [(issue.path, issue.message) for issue in caught.value.issues] == [
        (
            "concept.md.frontmatter.verified",
            "verified must contain at least one event",
        )
    ]


@pytest.mark.parametrize(
    "verified",
    [
        "{by: human:owner, at: 2026-07-27T10:00:00Z}",
        "[{by: process:nightly, at: 2026-07-27T02:00:00Z}]",
    ],
)
def test_verified_accepts_mapping_and_list_forms(
    tmp_path: Path,
    verified: str,
) -> None:
    _write_concept(tmp_path, f"type: Metric\nverified: {verified}")
    assert OkfBundle.load(tmp_path).concepts["concept"]


@pytest.mark.parametrize(
    "fingerprint",
    ["short", "g" * 64, 123],
)
def test_generated_fingerprint_must_be_a_sha256_hex_digest_when_present(
    tmp_path: Path,
    fingerprint: object,
) -> None:
    _write_concept(
        tmp_path,
        f"type: Metric\ngenerated: {{by: process:build, fingerprint: {fingerprint}}}",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert "concept.md.frontmatter.generated.fingerprint" in {
        issue.path for issue in caught.value.issues
    }


def test_valid_v02_optional_families_are_accepted(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric\n"
        "status: stable\n"
        "stale_after: 2026-09-23\n"
        "generated: {by: process:selayer-okf, at: 2026-07-27T14:00:00Z, "
        f"fingerprint: {'a' * 64}}}\n"
        "verified: {by: human:owner, at: 2026-07-27T15:00:00Z}\n"
        "sources:\n"
        "  - id: policy\n"
        "    resource: https://example.com/policy\n",
    )

    assert OkfBundle.load(tmp_path).concepts["concept"]


@pytest.mark.parametrize(
    ("family", "issue_path"),
    [
        ("generated: {at: 2026-07-27T10:00:00Z}", "generated.by"),
        ("generated: {by: process:build, at: yesterday}", "generated.at"),
        ("verified: [{by: human:owner}]", "verified[0].at"),
        ("verified: nope", "verified"),
        ("sources: [nope]", "sources[0]"),
        ("sources: [{id: policy}]", "sources[0].resource"),
    ],
)
def test_optional_family_members_are_validated(
    tmp_path: Path,
    family: str,
    issue_path: str,
) -> None:
    _write_concept(tmp_path, f"type: Metric\n{family}")

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert f"concept.md.frontmatter.{issue_path}" in {
        issue.path for issue in caught.value.issues
    }


def test_unknown_type_and_extension_are_preserved(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Product Identifier Scheme\ncustom_owner: product",
    )
    concept = OkfBundle.load(tmp_path).concepts["concept"]
    assert concept.frontmatter["custom_owner"] == "product"


def test_malformed_yaml_is_reported_at_document_path(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        "---\ntype: [unterminated\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "bad.md"


def test_cyclic_yaml_alias_is_reported_at_document_path(tmp_path: Path) -> None:
    (tmp_path / "cyclic.md").write_text(
        "---\ntype: Metric\ncustom: &node {self: *node}\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert [(issue.path, issue.message) for issue in caught.value.issues] == [
        ("cyclic.md", "cyclic YAML frontmatter is not supported")
    ]


def test_broken_internal_links_are_sorted_warnings(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Related\n\n"
        "[missing](missing.md) [absolute](/other.md) "
        "[external](https://example.com) [anchor](#related)\n",
    )

    bundle = OkfBundle.load(tmp_path)

    assert len(bundle.diagnostics) == 2
    assert all(issue.severity == "warning" for issue in bundle.diagnostics)
    assert list(bundle.diagnostics) == sorted(
        bundle.diagnostics,
        key=lambda issue: (issue.path, issue.message),
    )


def test_existing_internal_link_is_valid(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Related\n\n[Other](other.md#meaning)\n",
    )
    (tmp_path / "other.md").write_text(
        "---\ntype: Reference\n---\n\n# Meaning\n",
        encoding="utf-8",
    )

    assert OkfBundle.load(tmp_path).diagnostics == ()


@pytest.mark.parametrize(
    "selayer_id",
    [
        "metric.",
        ".gross_margin",
        "metric.GrossMargin",
        "metric.two.parts",
    ],
)
def test_malformed_selayer_ids_are_rejected_without_a_layer(
    tmp_path: Path,
    selayer_id: str,
) -> None:
    _write_concept(
        tmp_path,
        f"type: Selayer Metric\nselayer_id: {selayer_id}",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path, layer=None)

    assert {issue.path for issue in caught.value.issues} == {
        "concept.md.frontmatter.selayer_id"
    }


def test_selayer_id_is_resolved_and_kind_checked(
    tmp_path: Path, valid_catalog_path: Path
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    _write_concept(
        knowledge,
        "type: Selayer Metric\nselayer_id: metric.gross_margin",
    )

    bundle = OkfBundle.load(knowledge, layer=layer)

    assert bundle.layer is layer


@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        (
            "type: Selayer Metric\nselayer_id: metric.unknown",
            "concept.md.frontmatter.selayer_id",
        ),
        (
            "type: Selayer Dimension\nselayer_id: metric.gross_margin",
            "concept.md.frontmatter.type",
        ),
        (
            "type: Selayer Metric\nselayer_id: [metric, gross_margin]",
            "concept.md.frontmatter.selayer_id",
        ),
    ],
)
def test_invalid_selayer_bindings_are_rejected(
    tmp_path: Path,
    valid_catalog_path: Path,
    frontmatter: str,
    issue_path: str,
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    _write_concept(knowledge, frontmatter)

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(knowledge, layer=layer)

    assert issue_path in {issue.path for issue in caught.value.issues}


def test_duplicate_selayer_bindings_are_rejected(
    tmp_path: Path, valid_catalog_path: Path
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(
            "---\ntype: Selayer Metric\nselayer_id: metric.gross_margin\n---\n",
            encoding="utf-8",
        )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path, layer=layer)

    assert {issue.path for issue in caught.value.issues} == {
        "a.md.frontmatter.selayer_id",
        "b.md.frontmatter.selayer_id",
    }


def test_nested_index_rejects_frontmatter(tmp_path: Path) -> None:
    nested = tmp_path / "metrics"
    nested.mkdir()
    (nested / "index.md").write_text(
        "---\nokf_version: '0.2'\n---\n\n# Metrics\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "metrics/index.md"


def test_root_index_allows_only_okf_version_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.2'\nextra: nope\n---\n\n# Concepts\n",
        encoding="utf-8",
    )

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert caught.value.issues[0].path == "index.md"


def test_root_index_accepts_optional_version_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.2'\n---\n\n# Concepts\n",
        encoding="utf-8",
    )

    assert OkfBundle.load(tmp_path).concepts == {}


def test_log_requires_iso_date_headings(tmp_path: Path) -> None:
    (tmp_path / "log.md").write_text(
        "# Directory Update Log\n\n## July 27\n* Update\n",
        encoding="utf-8",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert caught.value.issues[0].path == "log.md"


def test_root_log_and_per_kind_indexes_are_reserved_not_concepts(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "metrics"
    nested.mkdir()
    (nested / "index.md").write_text("# Metrics\n", encoding="utf-8")
    (tmp_path / "log.md").write_text(
        "# Directory Update Log\n\n## 2026-07-27\n* Update\n",
        encoding="utf-8",
    )

    assert OkfBundle.load(tmp_path).concepts == {}


def test_underscore_layout_files_are_loaded_as_ordinary_unknown_concepts(
    tmp_path: Path,
) -> None:
    (tmp_path / "_index.md").write_text(
        "---\ntype: Domain Concept\ntitle: Legacy Index\n---\n# Meaning\n\nCurated.\n",
        encoding="utf-8",
    )
    (tmp_path / "_change_log.md").write_text(
        "---\ntype: Reference\ntitle: Legacy Change Log\n---\n# Meaning\n\nCurated.\n",
        encoding="utf-8",
    )

    bundle = OkfBundle.load(tmp_path)

    assert tuple(bundle.concepts) == ("_change_log", "_index")


@pytest.mark.parametrize(
    ("frontmatter", "issue_path"),
    [
        ("type: Attested Computation\nruntime: python\nparameters: nope", "parameters"),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{type: year}]",
            "parameters[0].name",
        ),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{name: year}]",
            "parameters[0].type",
        ),
        (
            "type: Attested Computation\nruntime: python\nparameters: [{name: year, type: int, required: 1}]",
            "parameters[0].required",
        ),
        ("type: Attested Computation\nruntime: python\ncomputation: ''", "computation"),
        (
            "type: Attested Computation\nruntime: python\nexecutor: nope",
            "executor",
        ),
        (
            "type: Attested Computation\nruntime: python\nexecutor: {receipt: nope}",
            "executor.resource",
        ),
        (
            "type: Attested Computation\nruntime: python\nexecutor: {resource: run.md, receipt: []}",
            "executor.receipt",
        ),
        (
            "type: Attested Computation\nruntime: python\nattester: nope",
            "attester",
        ),
        (
            "type: Attested Computation\nruntime: python\nattester: {}",
            "attester.resource",
        ),
    ],
)
def test_invalid_attested_computation_fields_are_rejected(
    tmp_path: Path,
    frontmatter: str,
    issue_path: str,
) -> None:
    _write_concept(tmp_path, frontmatter)
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert f"concept.md.frontmatter.{issue_path}" in {
        issue.path for issue in caught.value.issues
    }


def test_minimal_attested_computation_remains_valid(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Attested Computation\nruntime: python")
    assert OkfBundle.load(tmp_path).concepts["concept"]


def test_full_attested_computation_contract_is_valid(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Attested Computation\n"
        "runtime: bigquery\n"
        "parameters:\n"
        "  - {name: year, type: integer, required: true}\n"
        "computation: references/computations/revenue.sql\n"
        "executor:\n"
        "  resource: references/skills/run-on-bq.md\n"
        "  receipt: [job_id, executed_sql, result]\n"
        "attester:\n"
        "  resource: references/attesters/revenue.py\n",
    )
    assert OkfBundle.load(tmp_path).concepts["concept"]
