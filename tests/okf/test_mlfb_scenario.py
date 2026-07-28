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


def test_mlfb_retrieval_surfaces_the_attested_computation_contract(
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

    decoder = next(
        item for item in result.items if item.concept_id == "computations/mlfb_decoder"
    )
    contract = decoder.attested_computation
    assert contract is not None
    assert contract.runtime == "python"
    assert contract.parameters[0].name == "mlfb"
    assert contract.executor_resource == "../references/mlfb_coding_guide.md"
    assert contract.executor_receipt == ("decoded_value",)
    assert contract.attester_resource is not None
    with pytest.raises(ContextLookupError):
        bundle.context_for(["dimension.product_color"])
