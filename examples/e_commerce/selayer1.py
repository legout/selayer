"""Run the grain-aware e-commerce catalog example.

From the repository root:

``uv run python examples/e_commerce/gen_data.py``
``uv run python examples/e_commerce/selayer1.py``

Use ``--data-dir`` on the runner to query data generated elsewhere.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

import yaml

from selayer import QueryEngine, QueryPlanningError, SemanticLayer

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "ecommerce_semantic_layer.yaml"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
_SOURCE_FILES = {
    "orders": "orders.parquet",
    "order_items": "order_items.parquet",
    "customers": "customers.parquet",
    "products": "products.parquet",
}


@contextmanager
def _runtime_catalog(data_dir: Path) -> Iterator[Path]:
    catalog = cast(dict[str, Any], yaml.safe_load(CATALOG.read_text(encoding="utf-8")))
    sources = cast(dict[str, dict[str, Any]], catalog["data_sources"])
    for name, filename in _SOURCE_FILES.items():
        source = sources[name]
        schema_ref = cast(str, source.pop("schema_ref"))
        source["schema"] = yaml.safe_load(
            (ROOT / schema_ref).read_text(encoding="utf-8")
        )
        source["location"] = str(data_dir / filename)

    with NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".yaml", delete=False
    ) as handle:
        yaml.safe_dump(catalog, handle, sort_keys=False)
        catalog_path = Path(handle.name)
    try:
        yield catalog_path
    finally:
        catalog_path.unlink(missing_ok=True)


def load_layer(data_dir: Path | str = DEFAULT_DATA_DIR) -> SemanticLayer:
    """Load the catalog with source locations bound to ``data_dir``."""
    with _runtime_catalog(Path(data_dir).expanduser().resolve()) as catalog_path:
        return SemanticLayer.load(catalog_path)


def main(data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
    """Load the catalog, inspect plans, and execute safe example queries."""
    layer = load_layer(data_dir)
    print(f"Loaded schema version {layer.version}: {layer.label}")

    # The runtime catalog binds every source location to data_dir.
    with QueryEngine(layer) as engine:
        order_plan = engine.plan(["average_order_value"], ["customer_segment"])
        print("Order plan anchor:", order_plan.anchor_source)
        print("Order revenue by customer segment:")
        print(engine.query(["average_order_value"], ["customer_segment"]))

        print("Product-category item gross margin:")
        print(engine.query(["gross_margin"], ["product_category"]))

        print("Filtered Books item metrics:")
        print(
            engine.query(
                ["total_item_revenue", "average_item_price"],
                ["product_category"],
                {"product_category": "Books"},
            )
        )

        try:
            engine.plan(["average_order_value", "gross_margin"], ["product_category"])
        except QueryPlanningError as error:
            print(f"Expected mixed-grain rejection: {error.code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the e-commerce example")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing generated datasets (default: examples/e_commerce/data)",
    )
    main(parser.parse_args().data_dir)
