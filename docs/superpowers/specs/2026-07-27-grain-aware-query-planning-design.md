# Grain-Aware Query Planning Design

## Purpose

Make analytical correctness explicit. selayer must execute a query only when it can prove that joins and aggregations preserve the declared row grain. The first implementation supports metrics whose measures share one grain and dimensions reachable through grain-preserving joins.

This is a breaking redesign. No released catalog format or Python interface requires backward compatibility.

## Goals

1. Require every data source to declare the columns that identify one row.
2. Represent calculated facts and metrics with a small, validated expression language rather than raw SQL.
3. Compile measures only from facts anchored at a declared source grain.
4. Combine measures in one metric only when they share that anchor grain.
5. Permit one-to-one and many-to-one traversal from the anchor grain.
6. Reject joins that can duplicate anchor rows.
7. Correctly answer product-level revenue, cost, and gross-margin queries from the example catalog.
8. Preserve a small `QueryEngine.query()` caller interface while placing parsing, planning, and SQL compilation behind it.
9. Establish stable typed semantic identifiers suitable for future documentation and knowledge enrichment without adding OKF behavior.

## Non-goals

- Backward compatibility with the current YAML or Python model.
- OKF loading, generation, validation, or query behavior.
- Agent-generated semantics.
- Automatic or explicit allocation between grains.
- Combining order-grain and order-item-grain measures in one metric.
- Allocating order discounts, shipping, or taxes to individual items.
- Many-to-many analytical planning.
- A Polars, PostgreSQL, or other execution compiler.
- Changes to the archived chat and UI prototypes under `legacy/`.

## Domain terminology

### Data source

A named tabular relation available to the planner. It declares its physical loading configuration and its row grain.

### Grain

The ordered set of source columns whose values identify one logical row. For example:

- `orders` has grain `[id]`;
- `order_items` has grain `[order_id, product_id]`;
- `products` has grain `[id]`.

The first implementation requires a non-empty grain. It does not require selayer to enforce database uniqueness during every query, but catalog validation can optionally verify uniqueness against loaded data.

### Anchor source

The data source whose grain defines a fact expression. All field references used by that fact must be reachable from the anchor through grain-preserving joins.

### Grain-preserving join

A traversal that cannot increase the number of anchor rows:

- one-to-one traversal in either direction;
- many-to-one traversal from the many side to the one side;
- one-to-many traversal from the many side to the one side.

Traversal from a one side to a many side and every many-to-many traversal are row-expanding and invalid in this release.

### Fact

A row-level semantic value evaluated at its anchor source grain. A fact contains an expression over source fields and literals.

### Measure

An aggregation of one fact. Its grain is inherited from the fact's anchor source.

### Metric

An aggregate-level formula over measures. Every referenced measure must inherit the same anchor grain.

### Dimension

A named grouping or filtering field. A dimension can be used with a metric when its source is reachable from the metric's anchor through grain-preserving joins.

## Stable semantic identifiers

Catalog mapping keys are stable local identifiers. selayer derives a canonical typed identifier by prefixing the kind:

```text
source.order_items
dimension.product_category
fact.item_cost
measure.total_item_cost
metric.gross_margin
relationship.product_order_items
```

Identifiers use lowercase ASCII letters, digits, and underscores, start with a letter, and match:

```text
[a-z][a-z0-9_]*
```

Labels and descriptions are mutable presentation metadata and do not participate in references.

OKF may reference these identifiers in a future project, but OKF is not part of this design or implementation.

## Catalog schema

The catalog has a required schema version of `1`.

```yaml
version: 1
name: ecommerce
label: E-commerce Analytics
description: Semantic model for the example store

data_sources:
  orders:
    type: parquet
    path: data/orders.parquet
    grain: [id]

  order_items:
    type: parquet
    path: data/order_items.parquet
    grain: [order_id, product_id]

  products:
    type: parquet
    path: data/products.parquet
    grain: [id]

dimensions:
  product_category:
    source: products
    column: category
    data_type: string
    description: Product category

  order_date:
    source: orders
    column: created_at
    data_type: timestamp
    description: Order creation time

facts:
  item_revenue:
    source: order_items
    data_type: decimal
    expression: order_items.total
    description: Revenue recorded on one order item

  item_cost:
    source: order_items
    data_type: decimal
    expression: order_items.quantity * products.cost
    description: Extended product cost for one order item

measures:
  total_item_revenue:
    fact: item_revenue
    aggregation: sum
    description: Item revenue

  total_item_cost:
    fact: item_cost
    aggregation: sum
    description: Extended item cost

metrics:
  gross_margin:
    expression: >-
      (total_item_revenue - total_item_cost) / total_item_revenue
    measures: [total_item_revenue, total_item_cost]
    description: Gross margin ratio

relationships:
  product_order_items:
    source: products
    target: order_items
    type: one_to_many
    source_column: id
    target_column: product_id
```

### Required fields

- Catalog: `version`, `name`, `data_sources`.
- Data source: `type`, `path`, `grain`.
- Dimension: `source`, `column`, `data_type`.
- Fact: `source`, `expression`, `data_type`.
- Measure: `fact`, `aggregation`.
- Metric: `expression`, `measures`.
- Relationship: `source`, `target`, `type`, `source_column`, `target_column`.

Descriptions and labels are optional.

### Supported aggregation values

The first implementation supports:

- `sum`
- `avg`
- `min`
- `max`
- `count`
- `count_distinct`

A filtered aggregation is expressed with a row-level conditional expression rather than a raw SQL filter string.

## Expression language

One parser produces an engine-neutral expression tree. Validation applies a different symbol environment to row expressions and metric expressions.

### Grammar

```text
expression     := comparison
comparison     := additive (comparison_op additive)?
additive       := multiplicative (("+" | "-") multiplicative)*
multiplicative := unary (("*" | "/") unary)*
unary          := ("+" | "-" | "not") unary | primary
primary        := identifier | number | string | boolean | null
               | function_call | "(" expression ")"
function_call  := identifier "(" arguments? ")"
arguments      := expression ("," expression)*
comparison_op  := "=" | "!=" | "<" | "<=" | ">" | ">="
```

Identifiers may be local semantic names such as `total_item_revenue` or qualified source fields such as `order_items.quantity`.

The parser rejects trailing input, unknown tokens, comments, semicolons, SQL keywords, subqueries, attribute chains longer than two segments, and function names outside the allowlist.

### Expression tree

The parser returns immutable nodes equivalent to:

```python
Literal(value)
Reference(parts=(...))
UnaryOperation(operator, operand)
BinaryOperation(operator, left, right)
FunctionCall(name, arguments)
```

Expression nodes contain no SQL text.

### Row-expression environment

Fact expressions may reference:

- fields on the anchor source;
- fields reachable through grain-preserving joins;
- literals;
- allowlisted row functions.

They may not reference measures, metrics, dimensions by alias, aggregate functions, or fields requiring a row-expanding join.

Initial row functions:

- `coalesce(value, fallback)`
- `nullif(left, right)`
- `abs(value)`
- `lower(value)`
- `upper(value)`

Conditional aggregation uses:

```text
if(condition, when_true, when_false)
```

### Metric-expression environment

Metric expressions may reference only the measures listed in that metric plus literals and allowlisted scalar functions. They may not reference physical source fields, facts, dimensions, metrics, or aggregate functions.

Initial metric functions:

- `coalesce(value, fallback)`
- `nullif(left, right)`
- `abs(value)`

Division compiles with ordinary DuckDB semantics. Catalog authors use `nullif(denominator, 0)` when a zero denominator should produce null.

## Python model

The active model becomes immutable after construction so validated plans cannot be invalidated by later dictionary mutation.

Representative interfaces:

```python
@dataclass(frozen=True)
class DataSource:
    name: str
    type: str
    path: str
    grain: tuple[str, ...]

@dataclass(frozen=True)
class Fact:
    name: str
    source: str
    expression: Expression
    data_type: str
    description: str = ""

@dataclass(frozen=True)
class Measure:
    name: str
    fact: str
    aggregation: Aggregation
    description: str = ""

@dataclass(frozen=True)
class Metric:
    name: str
    expression: Expression
    measures: tuple[str, ...]
    description: str = ""

@dataclass(frozen=True)
class Dimension:
    name: str
    source: str
    column: str
    data_type: str
    description: str = ""
```

`SemanticLayer.load()` parses YAML into these validated objects. It does not expose partially valid model objects.

Programmatic construction accepts expression strings through explicit factory methods that run the same parser used by YAML loading. Callers do not construct parser nodes manually for normal use.

## Module structure

```text
src/selayer/
├── __init__.py
├── model.py
├── catalog.py
├── expressions/
│   ├── __init__.py
│   ├── ast.py
│   ├── parser.py
│   └── validation.py
├── planning/
│   ├── __init__.py
│   ├── types.py
│   └── planner.py
├── compilation/
│   ├── __init__.py
│   └── duckdb.py
├── query.py
└── py.typed
```

### Module interfaces

- `expressions` parses and validates formulas without knowing DuckDB.
- `planning` resolves semantic identifiers, grains, dimensions, and join paths into a typed query plan.
- `compilation.duckdb` converts a validated query plan into SQL plus bound parameters.
- `query.QueryEngine` loads sources, asks the planner for a plan, compiles it, executes it, and returns a Polars DataFrame.

The planner does not hold a database connection. The compiler does not read the semantic catalog. The expression parser does not emit SQL.

## Query planning

`QueryEngine.query(metrics, dimensions, filters)` remains the primary caller interface.

For each query, the planner performs these steps in order:

1. Require at least one metric.
2. Resolve all requested metrics, dimensions, and filter dimensions.
3. Resolve each metric expression to exactly its declared measures.
4. Resolve each measure to one fact and one anchor source.
5. Require all measures across the requested metrics to share one anchor source and grain.
6. Resolve every field referenced by each fact expression.
7. Find a deterministic relationship path from the anchor source to each required source.
8. Verify every path is grain-preserving from the anchor.
9. Deduplicate shared joins.
10. Produce a typed query plan containing the anchor, joins, row expressions, aggregations, metric expressions, dimensions, filters, and output order.

Relationship traversal order is deterministic: relationships are sorted by stable identifier, and ambiguous equal-length paths are rejected rather than selected arbitrarily.

## Query-plan model

The internal plan contains no catalog dictionaries and no unresolved string references. Representative types:

```python
@dataclass(frozen=True)
class JoinStep:
    relationship_id: str
    source: str
    target: str
    source_column: str
    target_column: str

@dataclass(frozen=True)
class PlannedMeasure:
    id: str
    expression: Expression
    aggregation: Aggregation

@dataclass(frozen=True)
class QueryPlan:
    anchor_source: str
    joins: tuple[JoinStep, ...]
    dimensions: tuple[PlannedDimension, ...]
    measures: tuple[PlannedMeasure, ...]
    metrics: tuple[PlannedMetric, ...]
    filters: tuple[PlannedFilter, ...]
```

## DuckDB compilation

The compiler uses nested SQL so metric formulas operate on already aggregated measures:

```sql
WITH aggregated AS (
    SELECT
        "products"."category" AS "product_category",
        SUM("order_items"."total") AS "total_item_revenue",
        SUM(
            "order_items"."quantity" * "products"."cost"
        ) AS "total_item_cost"
    FROM "order_items"
    JOIN "products"
      ON "products"."id" = "order_items"."product_id"
    GROUP BY 1
)
SELECT
    "product_category",
    (
        "total_item_revenue" - "total_item_cost"
    ) / "total_item_revenue" AS "gross_margin"
FROM aggregated
```

All identifiers come from validated catalog objects and are quoted. All query filter values are bound parameters. The compiler never interpolates filter values.

## Filtering

Filters remain a mapping from dimension identifier to a scalar, list, or two-value range:

```python
filters={
    "product_category": ["Books", "Electronics"],
    "order_date": (start, end),
}
```

Filters are applied before aggregation. A filter dimension must be reachable through a grain-preserving join from the anchor. Empty lists compile to `FALSE`.

## Validation and errors

Catalog loading collects deterministic validation issues and raises one `CatalogValidationError` containing all issues sorted by semantic identifier and field path.

Validation includes:

- schema version and required fields;
- identifier syntax and uniqueness;
- non-empty source grain;
- referenced sources, facts, measures, and relationships;
- supported cardinalities and aggregations;
- expression syntax and allowed symbols;
- fact-expression field reachability;
- metric measure declaration matching actual expression references;
- same-grain measure requirements;
- relationship endpoint consistency.

Query planning raises `QueryPlanningError` for valid catalogs that cannot satisfy a requested query, including:

- unknown requested semantic identifiers;
- no relationship path;
- ambiguous relationship path;
- row-expanding path;
- mixed measure grains.

Expression parsing raises `ExpressionSyntaxError` with the expression offset and a concise expectation. Expression semantic validation contributes normal catalog validation issues.

DuckDB execution failures raise `QueryExecutionError` containing the generated query identifier and the underlying DuckDB message. Bound parameter values are not included in exception text.

## Example-catalog migration

The existing example catalog is rewritten directly to schema version 1.

Product analytics uses order-item-grain facts:

- `item_quantity = order_items.quantity`
- `item_revenue = order_items.total`
- `item_cost = order_items.quantity * products.cost`

Corresponding measures:

- `units_sold = sum(item_quantity)`
- `total_item_revenue = sum(item_revenue)`
- `total_item_cost = sum(item_cost)`

Corresponding metrics:

- `average_item_price = total_item_revenue / nullif(units_sold, 0)`
- `gross_margin = (total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)`

Order analytics remains anchored at `orders` and cannot be combined with item-grain measures in one metric. Product-category queries use item-grain metrics. Customer and order-date dimensions remain reachable from `order_items` through `order_items → orders → customers`, which is many-to-one at each step.

The example questions and tests distinguish order-grain revenue from item-grain revenue rather than treating them as interchangeable.

## Testing strategy

### Expression tests

- precedence and parentheses;
- unary and comparison operators;
- literals and qualified references;
- repeated references;
- every allowlisted function;
- rejected SQL syntax, comments, semicolons, unknown tokens, and trailing input;
- row and metric symbol-environment violations.

### Catalog tests

- complete valid catalog load;
- every missing or invalid required field;
- stable identifiers;
- broken references;
- invalid relationship endpoints and cardinalities;
- mixed-grain metric rejection;
- deterministic multi-issue ordering;
- YAML round trip of the new schema.

### Planner tests

- same-source dimensions;
- many-to-one dimension paths;
- deduplicated shared joins;
- no-path, ambiguous-path, fan-out, many-to-many, and mixed-grain errors;
- deterministic plans independent of mapping insertion order;
- filter-only joins.

### Compiler tests

- quoted identifiers;
- bound scalar, list, range, and empty-list filters;
- row expressions inside aggregate functions;
- metric expressions outside the aggregate CTE;
- stable output column order;
- no raw filter values in SQL.

### Integration tests

- overall order metrics;
- item revenue and cost by product category;
- gross margin by product category;
- item metrics by customer and order date through safe joins;
- all supported example metric/dimension combinations classified as success or a specific planning error;
- numerical assertions against independently calculated Polars results;
- wheel build and public import checks.

## Delivery sequence

1. Expression tree, parser, and validation.
2. Breaking immutable catalog model and schema loader.
3. Typed grain-aware planner.
4. DuckDB compiler and `QueryEngine` integration.
5. Example catalog migration and numerical integration tests.
6. Documentation, public exports, and package verification.

Each slice is implemented test-first and must leave the active test suite green.
