from pathlib import Path

from examples.e_commerce.okf_workflow import build_context

EXPECTED_USAGE = """\
from selayer import OkfBundle, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")

OkfBundle.from_layer(layer).write("knowledge")

bundle = OkfBundle.load("knowledge", layer=layer)
context = bundle.context_for(
    ["metric.gross_margin", "dimension.product_category"],
    include_linked=True,
    max_chars=12_000,
)
"""


def test_readme_documents_api_authority_and_explicit_exclusions(root: Path) -> None:
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert EXPECTED_USAGE in readme
    for statement in (
        "The YAML catalog controls execution; OKF is advisory context only.",
        "`write()` creates new bundles",
        "`generate()` follows the same new-bundle-only safety contract",
        "root `index.md`, per-kind `index.md`, and root append-only `log.md`",
        "`sync()` preserves curated sections",
        "MLFB color requires a real catalog dimension before it is queryable",
        "Data values are never exported",
        "semantic search",
        "multi-provider brokering",
        "wiki publishing",
        "RAG",
        "embeddings",
        "orchestration",
    ):
        assert statement in readme


def test_readme_documents_all_dependency_free_cli_commands(root: Path) -> None:
    readme = (root / "README.md").read_text(encoding="utf-8")

    for command in (
        "selayer-okf generate",
        "selayer-okf sync",
        "selayer-okf validate",
        "selayer-okf retrieve",
    ):
        assert command in readme
    assert "Successful commands exit 0" in readme
    assert "domain, validation, I/O, or sync-conflict errors exit 1" in readme
    assert "invalid command-line usage exits 2" in readme
    assert "JSON to standard output" in readme


def test_ecommerce_workflow_builds_advisory_context(
    tmp_path: Path,
    valid_catalog_path: Path,
) -> None:
    result = build_context(valid_catalog_path, tmp_path / "knowledge")

    assert {ref for item in result.items for ref in item.semantic_refs} == {
        "metric.gross_margin",
        "dimension.product_category",
    }
