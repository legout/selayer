from pathlib import Path

import pytest

from selayer import SemanticLayer
from selayer.okf import ContextLookupError, OkfBundle


def test_mlfb_context_links_interpretation_knowledge_without_adding_dimensions(
    tmp_path: Path,
    root: Path,
) -> None:
    catalog_path = tmp_path / "products.yaml"
    catalog_path.write_text(
        "version: 1\n"
        "name: products\n"
        "data_sources:\n"
        "  products:\n"
        "    type: parquet\n"
        "    path: data/products.parquet\n"
        "    grain: [id]\n"
        "dimensions:\n"
        "  mlfb:\n"
        "    source: products\n"
        "    column: mlfb\n"
        "    data_type: string\n"
        "    description: Product MLFB identifier\n"
        "facts: {}\n"
        "measures: {}\n"
        "metrics: {}\n"
        "relationships: {}\n",
        encoding="utf-8",
    )
    layer = SemanticLayer.load(catalog_path)
    bundle = OkfBundle.load(root / "tests/okf/fixtures/mlfb", layer=layer)

    result = bundle.context_for(["dimension.mlfb"], max_depth=1)

    assert {item.concept_id for item in result.items} == {
        "dimensions/mlfb",
        "concepts/mlfb_scheme",
        "computations/mlfb_decoder",
        "references/mlfb_coding_guide",
    }
    with pytest.raises(ContextLookupError):
        bundle.context_for(["dimension.product_color"])
