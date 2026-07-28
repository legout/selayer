"""Build and retrieve advisory context for the e-commerce catalog.

The YAML catalog remains the executable authority. This example creates or safely
synchronizes Markdown knowledge without reading or exporting data values.
"""

from __future__ import annotations

from pathlib import Path

from selayer import OkfBundle, SemanticLayer
from selayer.okf import ContextResult


def build_context(
    catalog_path: str | Path,
    knowledge_path: str | Path,
) -> ContextResult:
    """Create or sync knowledge, then retrieve bounded attributed context."""
    layer = SemanticLayer.load(catalog_path)
    destination = Path(knowledge_path)
    generated = OkfBundle.from_layer(layer)
    if destination.exists():
        report = generated.sync(destination)
        if report.conflicts:
            conflicts = ", ".join(report.conflicts)
            raise RuntimeError(f"OKF sync conflicts require review: {conflicts}")
    else:
        generated.write(destination)

    bundle = OkfBundle.load(destination, layer=layer)
    return bundle.context_for(
        ["metric.gross_margin", "dimension.product_category"],
        include_linked=True,
        max_chars=12_000,
    )


def main() -> None:
    """Run the repository-local example and print advisory context."""
    result = build_context("ecommerce_semantic_layer.yaml", "knowledge")
    for item in result.items:
        print(f"[{item.concept_id}]\n{item.content}\n")


if __name__ == "__main__":
    main()
