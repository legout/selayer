from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

from selayer import SemanticLayer
from selayer.okf import OkfBundle, OkfConcept, OkfValidationError
from selayer.okf.validation import validate_concept


def _write_concept(root: Path, frontmatter: str, body: str = "") -> Path:
    path = root / "concept.md"
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _make_concept(frontmatter: dict[str, object]) -> OkfConcept:
    return OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter=frontmatter,
    )


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": "Metric", "status": "bogus"},
        {"type": "Metric", "stale_after": "not-a-date"},
        {"type": "Metric", "generated": []},
        {
            "type": "Metric",
            "generated": {"by": "process:build", "fingerprint": "short"},
        },
        {"type": "Metric", "verified": "nope"},
        {"type": "Metric", "sources": "nope"},
        {"type": "Metric", "sources": [{"id": "policy"}]},
        {"type": "Attested Computation", "runtime": "python", "parameters": "nope"},
        {"type": "Attested Computation", "runtime": "python", "computation": ""},
        {"type": "Attested Computation", "runtime": "python", "executor": "nope"},
        {"type": "Attested Computation", "runtime": "python", "attester": "nope"},
    ],
)
def test_validate_concept_lenient_downgrades_optional_families_to_warnings(
    frontmatter: dict[str, object],
) -> None:
    issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert issues
    assert all(issue.severity == "warning" for issue in issues)
    assert list(issues) == sorted(issues, key=lambda issue: (issue.path, issue.message))


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": ""},
        {"type": "Attested Computation"},
        {"type": "Selayer Metric", "selayer_id": "metric."},
        {"type": "Selayer Metric", "selayer_id": "bogus"},
    ],
)
def test_validate_concept_keeps_hard_families_fatal_in_lenient_mode(
    frontmatter: dict[str, object],
) -> None:
    issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert issues
    assert all(issue.severity == "error" for issue in issues)


def test_strict_default_keeps_optional_families_as_errors() -> None:
    issues = validate_concept(
        _make_concept({"type": "Metric", "status": "bogus"}), None
    )

    assert issues
    assert all(issue.severity == "error" for issue in issues)


def test_lenient_issues_share_paths_and_messages_with_strict_errors() -> None:
    frontmatter: dict[str, object] = {
        "type": "Metric",
        "status": "bogus",
        "sources": "nope",
    }

    strict_issues = validate_concept(_make_concept(frontmatter), None)
    lenient_issues = validate_concept(_make_concept(frontmatter), None, strict=False)

    assert [(i.path, i.message) for i in strict_issues] == [
        (i.path, i.message) for i in lenient_issues
    ]
    assert all(i.severity == "error" for i in strict_issues)
    assert all(i.severity == "warning" for i in lenient_issues)


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


def test_inline_computation_section_without_path_is_valid(tmp_path: Path) -> None:
    _write_concept(
        tmp_path,
        "type: Attested Computation\nruntime: python",
        "\n# Computation\n\n    def decode(mlfb): ...\n",
    )
    assert OkfBundle.load(tmp_path).concepts["concept"]


def test_computation_path_and_inline_section_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Attested Computation\n"
        "runtime: python\n"
        "computation: references/computations/decode.sql\n",
        "\n# Computation\n\n    def decode(mlfb): ...\n",
    )
    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)
    assert any(
        issue.path == "concept.md.frontmatter.computation"
        and "mutually exclusive" in issue.message.lower()
        for issue in caught.value.issues
    )


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


def test_lenient_load_downgrades_optional_errors_to_warnings(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Metric\nstatus: unknown")

    bundle = OkfBundle.load(tmp_path, strict=False)

    assert [(issue.path, issue.severity) for issue in bundle.diagnostics] == [
        ("concept.md.frontmatter.status", "warning")
    ]
    assert bundle.concepts["concept"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "type: ''",
        "type: Attested Computation",
        "type: Selayer Metric\nselayer_id: metric.",
    ],
)
def test_hard_failures_remain_fatal_in_lenient_mode(
    tmp_path: Path, frontmatter: str
) -> None:
    _write_concept(tmp_path, frontmatter)

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path, strict=False)

    assert caught.value.issues
    assert all(issue.severity == "error" for issue in caught.value.issues)


def test_strict_load_default_remains_fatal_for_optional_errors(tmp_path: Path) -> None:
    _write_concept(tmp_path, "type: Metric\nstatus: unknown")

    with pytest.raises(OkfValidationError) as caught:
        OkfBundle.load(tmp_path)

    assert caught.value.issues[0].severity == "error"


def test_malformed_yaml_remains_fatal_in_lenient_mode(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        "---\ntype: [unterminated\n---\n", encoding="utf-8"
    )

    with pytest.raises(OkfValidationError):
        OkfBundle.load(tmp_path, strict=False)


def test_duplicate_bindings_remain_fatal_in_lenient_mode(
    tmp_path: Path, valid_catalog_path: Path
) -> None:
    layer = SemanticLayer.load(valid_catalog_path)
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(
            "---\ntype: Selayer Metric\nselayer_id: metric.gross_margin\n---\n",
            encoding="utf-8",
        )

    with pytest.raises(OkfValidationError):
        OkfBundle.load(tmp_path, layer=layer, strict=False)


def test_okf_issue_defaults_to_invalid_code() -> None:
    from selayer.okf.model import OkfIssue

    assert OkfIssue("a.md", "broken").code == "okf.invalid"
    assert OkfIssue("a.md", "broken", severity="warning").code == "okf.invalid"
    assert OkfIssue("a.md", "broken", "warning", "okf.link.missing_fragment").code == (
        "okf.link.missing_fragment"
    )


def test_catalog_aware_load_accepts_a_freshly_generated_bundle(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)

    bundle = OkfBundle.load(root, layer=valid_layer)

    assert bundle.diagnostics == ()


def test_catalog_aware_load_rejects_stale_valid_looking_fingerprint(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "metrics" / "gross_margin.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "Expression: `(total_item_revenue - total_item_cost)"
            " / nullif(total_item_revenue, 0)`",
            "Expression: `wrong`",
        ),
        encoding="utf-8",
    )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.fingerprint_mismatch" in {
        issue.code for issue in raised.value.issues
    }


def test_catalog_aware_load_rejects_missing_generated_concept(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    (root / "metrics" / "gross_margin.md").unlink()

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.missing_concept" in codes
    assert any(
        issue.path == "metrics/gross_margin.md" for issue in raised.value.issues
    )


def test_catalog_aware_load_rejects_orphan_generated_selayer_id(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    (root / "metrics" / "unknown.md").write_text(
        "---\n"
        "type: Selayer Metric\n"
        "title: Unknown\n"
        "selayer_id: metric.unknown\n"
        "generated:\n"
        "  by: process:selayer-okf\n"
        f"  fingerprint: {'a' * 64}\n"
        "status: stable\n"
        "---\n\n"
        "# Catalog Definition\n\nSemantic ID: `metric.unknown`\n",
        encoding="utf-8",
    )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.orphan_selayer_id" in {
        issue.code for issue in raised.value.issues
    }


def test_catalog_aware_load_rejects_wrong_semantic_kind_directory(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    original = root / "metrics" / "gross_margin.md"
    misplaced = root / "relationships" / "gross_margin.md"
    misplaced.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    original.unlink()

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.path_mismatch" in codes
    assert "okf.generated.missing_concept" in codes


def test_catalog_aware_load_rejects_definition_drift_from_catalog(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    from selayer.okf.document import generated_fingerprint, parse_concept

    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "sources" / "products.md"
    text = path.read_text(encoding="utf-8")
    # Tamper the Catalog Definition body, then re-stamp the fingerprint so the
    # document is internally self-consistent while it no longer matches the
    # catalog projection.
    tampered = text.replace("Grain: id", "Grain: forged")
    path.write_text(tampered, encoding="utf-8")
    concept = parse_concept(path, root)
    definition = next(
        section.content
        for section in concept.sections
        if section.title == "Catalog Definition"
    )
    fresh = generated_fingerprint(concept.frontmatter, definition)
    self_consistent = tampered.replace(
        str(concept.frontmatter["generated"]["fingerprint"]), fresh
    )
    path.write_text(self_consistent, encoding="utf-8")

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.definition_mismatch" in codes
    assert "okf.generated.fingerprint_mismatch" not in codes


def test_catalog_aware_load_rejects_generated_index_missing_member(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    index_path = root / "metrics" / "index.md"
    text = index_path.read_text(encoding="utf-8")
    index_path.write_text(
        text.replace("- [Gross margin](gross_margin.md)\n", ""), encoding="utf-8"
    )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.index_mismatch" in {
        issue.code for issue in raised.value.issues
    }


def test_catalog_aware_load_rejects_generated_index_with_wrong_title(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    index_path = root / "metrics" / "index.md"
    text = index_path.read_text(encoding="utf-8")
    index_path.write_text(
        text.replace("[Gross margin]", "[Wrong title]"), encoding="utf-8"
    )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.index_mismatch" in {
        issue.code for issue in raised.value.issues
    }


def test_lenient_catalog_aware_load_exposes_integrity_as_diagnostics(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "metrics" / "gross_margin.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "Expression: `(total_item_revenue - total_item_cost)"
            " / nullif(total_item_revenue, 0)`",
            "Expression: `wrong`",
        ),
        encoding="utf-8",
    )

    bundle = OkfBundle.load(root, layer=valid_layer, strict=False)

    codes = {issue.code for issue in bundle.diagnostics}
    assert "okf.generated.fingerprint_mismatch" in codes
    assert all(issue.severity == "warning" for issue in bundle.diagnostics)


def test_internal_link_with_missing_fragment_heading_is_a_warning(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    metric_path = root / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "# Related Concepts",
        "# Related Concepts\n\n"
        "[Order items](../sources/order_items.md#nonexistent-heading)\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    bundle = OkfBundle.load(root, layer=valid_layer)

    fragment_issues = [
        issue
        for issue in bundle.diagnostics
        if issue.code == "okf.link.missing_fragment"
    ]
    assert len(fragment_issues) == 1
    assert fragment_issues[0].severity == "warning"
    assert fragment_issues[0].path == "metrics/gross_margin.md.links"


def test_internal_link_fragment_heading_is_valid_when_present(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    metric_path = root / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "# Related Concepts",
        "# Related Concepts\n\n"
        "[Order items](../sources/order_items.md#catalog-definition)\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    bundle = OkfBundle.load(root, layer=valid_layer)

    assert "okf.link.missing_fragment" not in {
        issue.code for issue in bundle.diagnostics
    }


def _deep_thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw(item) for item in value]
    return value


def _render_with_frontmatter(
    concept: OkfConcept, frontmatter: dict[str, object]
) -> OkfConcept:
    return OkfConcept.create(
        concept_id=concept.concept_id,
        relative_path=concept.relative_path,
        frontmatter=frontmatter,
        preamble=concept.preamble,
        sections=concept.sections,
    )


def _drop_frontmatter_key(root: Path, relative: str, key: str) -> None:
    from selayer.okf.document import parse_concept, render_concept

    path = root / relative
    concept = parse_concept(path, root)
    frontmatter = _deep_thaw(concept.frontmatter)
    assert isinstance(frontmatter, dict)
    frontmatter.pop(key, None)
    path.write_text(
        render_concept(_render_with_frontmatter(concept, frontmatter)),
        encoding="utf-8",
    )


def _drop_generated_fingerprint(root: Path, relative: str) -> None:
    from selayer.okf.document import parse_concept, render_concept

    path = root / relative
    concept = parse_concept(path, root)
    frontmatter = _deep_thaw(concept.frontmatter)
    assert isinstance(frontmatter, dict)
    generated = frontmatter.get("generated")
    assert isinstance(generated, dict)
    generated.pop("fingerprint", None)
    path.write_text(
        render_concept(_render_with_frontmatter(concept, frontmatter)),
        encoding="utf-8",
    )


# --- High 1: generated-concept completeness is not path-only ---


def test_catalog_aware_load_rejects_generated_document_without_generated_metadata(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    _drop_frontmatter_key(root, "metrics/gross_margin.md", "generated")

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.missing_metadata" in codes
    assert any(
        issue.path == "metrics/gross_margin.md" for issue in raised.value.issues
    )


def test_catalog_aware_load_rejects_generated_document_without_selayer_id(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    _drop_frontmatter_key(root, "metrics/gross_margin.md", "selayer_id")

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.missing_metadata" in {
        issue.code for issue in raised.value.issues
    }


# --- High 2: a missing generated.fingerprint must fail under catalog-aware integrity ---


def test_catalog_aware_load_rejects_generated_document_without_fingerprint(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    _drop_generated_fingerprint(root, "metrics/gross_margin.md")

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.fingerprint_missing" in codes
    # A missing digest is distinct from a digest that no longer matches.
    assert "okf.generated.fingerprint_mismatch" not in codes


def test_lenient_catalog_aware_load_exposes_missing_fingerprint_as_a_warning(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    _drop_generated_fingerprint(root, "metrics/gross_margin.md")

    bundle = OkfBundle.load(root, layer=valid_layer, strict=False)

    codes = {issue.code for issue in bundle.diagnostics}
    assert "okf.generated.fingerprint_missing" in codes
    assert all(issue.severity == "warning" for issue in bundle.diagnostics)


# --- Medium: same-document fragment links and GitHub duplicate-heading slugs ---


def test_section_slugs_disambiguate_duplicate_headings() -> None:
    from selayer.okf.model import OkfSection
    from selayer.okf.validation import _heading_slug, _section_slugs

    concept = OkfConcept.create(
        concept_id="concept",
        relative_path=PurePosixPath("concept.md"),
        frontmatter={"type": "Metric"},
        sections=(
            OkfSection("Catalog Definition", "body"),
            OkfSection("Examples", "first"),
            OkfSection("Usage Guidance", "curated"),
            OkfSection("Examples", "second"),
        ),
    )
    assert _section_slugs(concept) == frozenset(
        {"catalog-definition", "examples", "usage-guidance", "examples-1"}
    )
    assert _heading_slug("Catalog Definition") == "catalog-definition"


def test_same_document_fragment_link_with_missing_heading_is_a_warning(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Related\n\n[missing section](#nonexistent)\n",
    )

    bundle = OkfBundle.load(tmp_path)

    fragment_issues = [
        issue
        for issue in bundle.diagnostics
        if issue.code == "okf.link.missing_fragment"
    ]
    assert len(fragment_issues) == 1
    assert fragment_issues[0].severity == "warning"
    assert fragment_issues[0].path == "concept.md.links"
    assert "nonexistent" in fragment_issues[0].message


def test_same_document_fragment_link_with_present_heading_is_valid(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Related\n\n[related](#related)\n",
    )

    assert "okf.link.missing_fragment" not in {
        issue.code for issue in OkfBundle.load(tmp_path).diagnostics
    }


def test_same_document_fragment_link_decodes_percent_encoding(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Foo Bar Baz\n\n[link](#foo-bar%2Dbaz)\n",
    )

    assert "okf.link.missing_fragment" not in {
        issue.code for issue in OkfBundle.load(tmp_path).diagnostics
    }


def test_duplicate_heading_suffix_slugs_resolve_within_a_document(
    tmp_path: Path,
) -> None:
    _write_concept(
        tmp_path,
        "type: Metric",
        "\n# Catalog Definition\n\ntop\n\n# Examples\n\nfirst\n\n"
        "# Usage Guidance\n\ncurated\n\n# Examples\n\nsecond\n\n"
        "# Related\n\n[first](#examples) [second](#examples-1) "
        "[missing](#examples-2)\n",
    )

    bundle = OkfBundle.load(tmp_path)

    fragment_issues = [
        issue
        for issue in bundle.diagnostics
        if issue.code == "okf.link.missing_fragment"
    ]
    assert len(fragment_issues) == 1
    assert "examples-2" in fragment_issues[0].message


# --- Re-review High 1: catalog-aware integrity must not be skipped when every
# generated document loses its generated metadata. ---


def test_catalog_aware_load_rejects_bundle_with_every_generated_metadata_stripped(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    for concept_path in sorted(root.rglob("*.md")):
        if concept_path.name in {"index.md", "log.md"}:
            continue
        _drop_frontmatter_key(
            root,
            concept_path.relative_to(root).as_posix(),
            "generated",
        )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.missing_metadata" in codes
    assert any(
        issue.code == "okf.generated.missing_metadata"
        and issue.path == "metrics/gross_margin.md"
        for issue in raised.value.issues
    )


# --- Re-review High 2: forged controlled frontmatter must be caught even when
# the stored fingerprint is recomputed to remain internally self-consistent. ---


def test_catalog_aware_load_rejects_forged_controlled_frontmatter(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    from selayer.okf.document import (
        generated_fingerprint,
        parse_concept,
        render_concept,
    )

    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "metrics" / "gross_margin.md"
    concept = parse_concept(path, root)
    frontmatter = _deep_thaw(concept.frontmatter)
    assert isinstance(frontmatter, dict)
    assert isinstance(frontmatter.get("generated"), dict)
    # Forge a controlled field (title) and re-stamp the fingerprint so the
    # document is internally self-consistent while it diverges from the catalog.
    frontmatter["title"] = "Forged title"
    definition = next(
        section.content
        for section in concept.sections
        if section.title == "Catalog Definition"
    )
    frontmatter["generated"]["fingerprint"] = generated_fingerprint(
        frontmatter, definition
    )
    path.write_text(
        render_concept(_render_with_frontmatter(concept, frontmatter)),
        encoding="utf-8",
    )

    with pytest.raises(OkfValidationError) as raised:
        OkfBundle.load(root, layer=valid_layer)

    codes = {issue.code for issue in raised.value.issues}
    assert "okf.generated.frontmatter_mismatch" in codes
    assert "okf.generated.fingerprint_mismatch" not in codes
    assert "okf.generated.definition_mismatch" not in codes


def test_catalog_aware_load_preserves_curated_non_controlled_frontmatter(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    path = root / "metrics" / "gross_margin.md"
    text = path.read_text(encoding="utf-8")
    # Curate a non-controlled field; the controlled region is untouched, so the
    # controlled-frontmatter projection check must not fire.
    curated = text.replace(
        "status: stable",
        "status: stable\nverified: {by: human:owner, at: 2026-07-27T15:00:00Z}",
    )
    path.write_text(curated, encoding="utf-8")

    bundle = OkfBundle.load(root, layer=valid_layer)

    assert "okf.generated.frontmatter_mismatch" not in {
        issue.code for issue in bundle.diagnostics
    }


# --- Re-review Medium: fragment links to generated index.md targets must be
# validated too, while external URLs and layer-free behavior are retained. ---


def test_internal_link_to_generated_index_with_missing_fragment_is_a_warning(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    metric_path = root / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "# Related Concepts",
        "# Related Concepts\n\n[Metrics index](index.md#nonexistent)\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    bundle = OkfBundle.load(root, layer=valid_layer)

    fragment_issues = [
        issue
        for issue in bundle.diagnostics
        if issue.code == "okf.link.missing_fragment"
    ]
    assert len(fragment_issues) == 1
    assert fragment_issues[0].severity == "warning"
    assert fragment_issues[0].path == "metrics/gross_margin.md.links"
    assert "nonexistent" in fragment_issues[0].message


def test_internal_link_to_generated_index_with_present_heading_is_valid(
    tmp_path: Path, valid_layer: SemanticLayer
) -> None:
    root = tmp_path / "knowledge"
    OkfBundle.generate(valid_layer, root)
    metric_path = root / "metrics" / "gross_margin.md"
    text = metric_path.read_text(encoding="utf-8")
    text = text.replace(
        "# Related Concepts",
        "# Related Concepts\n\n[Metrics index](index.md#metrics)\n",
    )
    metric_path.write_text(text, encoding="utf-8")

    bundle = OkfBundle.load(root, layer=valid_layer)

    assert "okf.link.missing_fragment" not in {
        issue.code for issue in bundle.diagnostics
    }
