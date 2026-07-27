# selayer

`selayer` is a small Python semantic-layer library for defining analytical data sources, facts, measures, dimensions, metrics, hierarchies, and relationships. It can serialize catalogs to YAML or JSON and execute metric queries with DuckDB.

## Install

```bash
uv add selayer
```

For local development:

```bash
uv sync
```

## Example

```python
from selayer import QueryEngine, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")
engine = QueryEngine(layer)

result = engine.query(
    metrics=["average_order_value"],
    dimensions=["customer_country"],
)
print(result)
```

## Semantic model

The public interface exports:

- `DataSource`
- `Fact`
- `Measure`
- `Dimension`
- `Hierarchy`
- `Metric`
- `Relationship`
- `SemanticLayer`
- `QueryEngine`

Existing catalogs can be loaded and saved as YAML or JSON:

```python
from selayer import SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")
layer.save("catalog.json", format="json")
print(layer.to_mermaid())
```

## Development

```bash
uv run pytest
uv run ruff check src tests examples
uv run pyright src tests
uv build
```

## Legacy artifacts

The former NL-to-SQL chat backend and Streamlit, Gradio, Panel, and Marimo prototypes are retained under [`legacy/`](legacy/README.md) as unsupported historical artifacts. They are not installed with `selayer` and are excluded from normal development checks.
