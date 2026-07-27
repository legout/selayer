"""Run the grain-aware e-commerce catalog example.

Run from the repository root with ``uv run python
examples/e_commerce/selayer1.py``.
"""

from pathlib import Path

from selayer import QueryEngine, QueryPlanningError, SemanticLayer

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "ecommerce_semantic_layer.yaml"


def main() -> None:
    """Load the catalog, inspect plans, and execute safe example queries."""
    layer = SemanticLayer.load(CATALOG)
    print(f"Loaded schema version {layer.version}: {layer.label}")

    # Data paths in the example catalog are repository-relative.
    with QueryEngine(layer) as engine:
        order_plan = engine.plan(
            ["average_order_value"], ["customer_segment"]
        )
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
            engine.plan(
                ["average_order_value", "gross_margin"], ["product_category"]
            )
        except QueryPlanningError as error:
            print(f"Expected mixed-grain rejection: {error.code}")


if __name__ == "__main__":
    main()
