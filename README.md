# selayer

`selayer` is a small semantic-layer library for grain-aware analytical queries.
The e-commerce example uses schema version `1` and the public `QueryEngine`
interface.

## Install

```bash
uv add selayer
```

For local development:

```bash
uv sync
```

## Example catalog

```python
from selayer import QueryEngine, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")
with QueryEngine(layer) as engine:
    result = engine.query(
        metrics=["gross_margin"],
        dimensions=["product_category"],
        filters={"product_category": "Books"},
    )
    print(result)
```

Run the complete example from the repository root:

```bash
uv run python examples/e_commerce/selayer1.py
```

## Grain-aware semantic model

Every source declares a non-empty grain. Order facts and measures are anchored
at the `orders` grain (`[id]`); item facts and measures are anchored at the
`order_items` grain (`[order_id, product_id]`). Order revenue and item revenue
are therefore distinct metrics and must not be combined in one query.

The example permits only grain-preserving many-to-one traversal from the
`order_items` anchor: `order_items -> products` and
`order_items -> orders -> customers` (the catalog stores these as one-to-many
relationships from the one side). A query that would expand anchor rows, has
mixed-grain measures, or has no safe relationship path is rejected with a
stable planning error.

Facts use the restricted, engine-neutral expression DSL, for example
`order_items.quantity * products.cost`. Metric expressions reference only
declared measures, such as `total_item_revenue / nullif(units_sold, 0)`.
Identifiers are typed by their catalog section (`source.products`,
`measure.total_item_cost`, and so on) and use stable lowercase names.

OKF, automatic or explicit re-graining/allocation, many-to-many planning, and
additional query engines are out of scope. DuckDB is the only execution
compiler.

## Public semantic model

The public interface exports exactly the symbols in `selayer.__all__`:

- `Aggregation`
- `Cardinality`
- `CatalogIssue`
- `CatalogValidationError`
- `DataSource`
- `Dimension`
- `Fact`
- `Measure`
- `Metric`
- `QueryEngine`
- `QueryExecutionError`
- `QueryPlan`
- `QueryPlanningError`
- `Relationship`
- `SemanticLayer`

Compiler and parser internals are intentionally not public exports.

Catalogs are loaded through the active schema-version-1 model:

```python
from selayer import SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")
print(layer.version, layer.data_sources.keys())
```

## Development

```bash
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests examples
uv build
```

## Legacy artifacts

The former NL-to-SQL chat backend and Streamlit, Gradio, Panel, and Marimo
prototypes are retained under [`legacy/`](legacy/README.md) as unsupported
historical artifacts. They are not installed with `selayer` and are excluded
from normal development checks.
