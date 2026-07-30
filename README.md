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

## Connectors, profiles, and reloads

The source connector matrix is closed and catalog-driven:

| Connector | Runtime ownership | Optional extra |
| --- | --- | --- |
| Parquet, CSV, PyArrow | PyArrow Dataset/provider | none |
| Delta | Python `deltalake` snapshots | `delta` |
| Iceberg | Python `pyiceberg` snapshots and query-scoped readers | `iceberg` |
| SQLite, DuckDB files | Native DuckDB read-only attachments | none |
| PostgreSQL | Native DuckDB PostgreSQL scanner | `postgres` |

S3 is a named runtime transport profile for file and lakehouse connectors, not
an additional source type (`s3` extra). Install every optional connector with
`uv sync --all-extras`, or install only the extras you need. Profiles contain
runtime-only values such as credentials and DSNs; they are never written to
catalogs, plans, OKF, reprs, logs, statuses, or error messages. Use `schema`
for an inline declaration or `schema_ref` for a contained YAML schema file;
every source must provide exactly one and a non-empty `grain`:

```yaml
sources:
  events:
    type: parquet
    location: data/events.parquet
    schema_ref: schemas/events.yaml
    grain: [id]
```

Reloading is explicit and preserves the published source on failure:

```python
with QueryEngine(layer, profiles=profiles) as engine:
    result = engine.query(["total_value"])
    change = engine.reload_source("events")
    all_changes = engine.reload_all()
    status = engine.source_status("events")
```

Arrow and Delta retain projection/filter pushdown. Iceberg owns snapshots in
PyIceberg and creates a fresh projected/filterable reader per query. SQLite,
DuckDB-file, and PostgreSQL sources are attached read-only under generated
internal aliases. Sources are not eagerly materialized into Polars; Polars is
only the result-frame boundary.

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

Automatic or explicit re-graining/allocation, many-to-many planning, and
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
- `OkfBundle`
- `QueryEngine`
- `QueryExecutionError`
- `QueryPlan`
- `QueryPlanningError`
- `Relationship`
- `ReloadResult`
- `SemanticLayer`
- `SourceConnectionError`
- `SourceDependencyError`
- `SourceError`
- `SourceProfileError`
- `SourceReloadError`
- `SourceSchemaError`
- `SourceStatus`
- `TableSchema`
- `FieldSchema`

Compiler and parser internals are intentionally not public exports.

Catalogs are loaded through the active schema-version-1 model. Programmatic
callers construct expression-bearing objects with the parser-backed factories:

```python
from selayer import Fact, Metric, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")
fact = Fact.from_expression(
    "item_revenue", "order_items", "order_items.total", "decimal"
)
metric = Metric.from_expression(
    "gross_revenue", "total_item_revenue", ["total_item_revenue"]
)
print(layer.version, fact.expression, metric.measures)
```

## Advisory OKF context

The YAML catalog controls execution; OKF is advisory context only. The catalog
is the executable authority for queryable dimensions, facts, measures, metrics,
relationships, planning, and compilation. OKF Markdown can explain those
objects, but it cannot add executable semantics or override the catalog.

Create a knowledge bundle and retrieve bounded, attributed context through the
public API:

```python
from selayer import OkfBundle, SemanticLayer

layer = SemanticLayer.load("ecommerce_semantic_layer.yaml")

OkfBundle.from_layer(layer).write("knowledge")

bundle = OkfBundle.load("knowledge", layer=layer)
context = bundle.context_for(
    ["metric.gross_margin", "dimension.product_category"],
    include_linked=True,
    max_chars=12_000,
)
```

Bundles use root `index.md`, per-kind `index.md`, and root append-only `log.md`.
`write()` creates new bundles and refuses to overwrite a populated destination.
`generate()` follows the same new-bundle-only safety contract and directs callers
to `sync()` instead of overwriting any existing file.
`sync()` preserves curated sections while updating generator-owned catalog
sections; conflicts remain explicit for human review. A decoded attribute such as
MLFB color requires a real catalog dimension before it is queryable.
Data values are never exported by generation, synchronization,
validation, or retrieval. Mutating bundle operations preflight the destination's
lexical ancestors and existing tree and refuse every symbolic link without
resolving through it. This is preflight protection only; descriptor-based
protection against filesystem races after the check is outside the portable API.

The deeper `selayer.okf` API exports `OkfBundle`, `OkfConcept`, `OkfIssue`,
`OkfValidationError`, `SyncReport`, `ContextItem`, `ContextResult`,
`ContextLookupError`, and `ContextBudgetError`. Only `OkfBundle` is also exposed
at the package root; parsers, renderers, merge helpers, and validation internals
are not public API.

This boundary intentionally provides no semantic search or multi-provider brokering.
It also provides no wiki publishing, RAG, embeddings, or orchestration. Those
concerns belong outside `selayer`.

### Dependency-free OKF CLI

The `selayer-okf` console script uses only the Python standard library for its
command-line and JSON presentation layer; it adds no CLI framework dependency.
It wraps the same advisory API:

```bash
uv run selayer-okf generate ecommerce_semantic_layer.yaml knowledge
uv run selayer-okf sync ecommerce_semantic_layer.yaml knowledge --dry-run
uv run selayer-okf validate knowledge --catalog ecommerce_semantic_layer.yaml
uv run selayer-okf retrieve knowledge metric.gross_margin dimension.product_category \
  --catalog ecommerce_semantic_layer.yaml --max-chars 12000 --max-depth 1
```

`generate CATALOG DESTINATION` creates a new bundle and fails without changing
files when the destination is populated. `sync CATALOG BUNDLE`
updates generator-owned definitions and accepts `--dry-run`. `validate BUNDLE`
and `retrieve BUNDLE SEMANTIC_ID...` accept an optional `--catalog`; retrieval
also accepts `--no-linked`, `--max-chars`, and `--max-depth`.

Successful commands exit 0 and write deterministic JSON to standard output;
domain, validation, I/O, or sync-conflict errors exit 1 and write an `error:`
message to standard error; a sync conflict also leaves its JSON report on
standard output. As defined by `argparse`, invalid command-line usage exits 2.
Diagnostics remain visible in the JSON rather than silently changing authority.
See [`examples/e_commerce/okf_workflow.py`](examples/e_commerce/okf_workflow.py)
for the API workflow.

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
