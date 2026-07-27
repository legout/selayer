# Grain-Aware Query Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace selayer's SQL-string prototype with a breaking, grain-aware semantic model, restricted expression DSL, typed planner, and DuckDB compiler that produces provably safe same-grain analytical queries.

**Architecture:** Catalog loading parses immutable model objects and engine-neutral expression trees. A connection-free planner resolves measures, grains, dimensions, filters, and safe joins into a typed `QueryPlan`; a DuckDB compiler converts only validated plans into quoted SQL and bound parameters; `QueryEngine` remains the small execution interface.

**Tech Stack:** Python 3.13+, dataclasses, PyYAML, DuckDB, Polars, pytest, Ruff, Pyright, uv.

## Global Constraints

- This is a breaking redesign; do not retain compatibility shims for the current YAML or Python model.
- Catalog schema version is exactly `1`.
- Every data source has a non-empty grain.
- Fact expressions use the restricted row DSL; metric expressions use the restricted aggregate DSL.
- Metrics may combine measures only when every measure has the same anchor source and grain.
- Only one-to-one and many-to-one traversal from the anchor is valid.
- Do not implement allocation, re-graining, many-to-many planning, OKF, agents, or additional execution engines.
- Keep `QueryEngine.query(metrics, dimensions, filters)` as the primary caller interface.
- Filter values are always bound parameters and never interpolated into SQL.
- Implement every behavior test-first and run the full active suite after each task.

---

## File map

### Create

- `src/selayer/expressions/__init__.py` — public expression types and parse entrypoint.
- `src/selayer/expressions/ast.py` — immutable expression nodes.
- `src/selayer/expressions/parser.py` — tokenizer and recursive-descent parser.
- `src/selayer/expressions/validation.py` — row and metric symbol validation.
- `src/selayer/planning/__init__.py` — planner exports.
- `src/selayer/planning/types.py` — immutable resolved plan types.
- `src/selayer/planning/planner.py` — grain and relationship planning.
- `src/selayer/compilation/__init__.py` — compiler exports.
- `src/selayer/compilation/duckdb.py` — expression and query-plan SQL compiler.
- `tests/expressions/test_parser.py`
- `tests/expressions/test_validation.py`
- `tests/planning/test_planner.py`
- `tests/compilation/test_duckdb.py`
- `tests/integration/test_ecommerce.py`

### Replace or modify

- `src/selayer/model.py` — immutable schema-version-1 model, promoted from temporary `_next` staging during the atomic runtime cutover.
- `src/selayer/catalog.py` — strict YAML parsing and aggregate validation errors, promoted during the same cutover.
- `src/selayer/query.py` — orchestration and execution only.
- `src/selayer/__init__.py` — curated new public interface.
- `tests/conftest.py` — schema-version-1 fixtures.
- `tests/test_catalog.py` — strict catalog tests.
- `tests/test_query.py` — execution-interface tests.
- `ecommerce_semantic_layer.yaml` — direct breaking migration.
- `examples/e_commerce/selayer1.py` — new model and metrics.
- `README.md` — schema and query examples.
- `.github/copilot-instructions.md` — new module layout and invariants.

### Temporary implementation staging

Tasks 2–4 build the breaking model under `src/selayer/_next/` while the current runtime remains operational. Task 5 atomically promotes `_next/model.py` and `_next/catalog.py` into their final paths and replaces `QueryEngine`. This is implementation scaffolding only: `_next/` does not exist in the completed tree and is never exported publicly.

---

### Task 1: Build the immutable expression tree and parser

**Files:**

- Create: `src/selayer/expressions/ast.py`
- Create: `src/selayer/expressions/parser.py`
- Create: `src/selayer/expressions/__init__.py`
- Create: `tests/expressions/test_parser.py`

**Interfaces:**

- Produces `Expression`, `Literal`, `Reference`, `UnaryOperation`, `BinaryOperation`, `FunctionCall`.
- Produces `ExpressionSyntaxError(expression: str, offset: int, message: str)`.
- Produces `parse_expression(source: str) -> Expression`.
- Consumed by catalog model, validator, planner, and compiler in later tasks.

- [ ] **Step 1: Write parser tests for precedence and immutable nodes**

```python
from selayer.expressions import (
    BinaryOperation,
    Literal,
    Reference,
    parse_expression,
)


def test_multiplication_binds_tighter_than_addition() -> None:
    assert parse_expression("a + b * 2") == BinaryOperation(
        operator="+",
        left=Reference(parts=("a",)),
        right=BinaryOperation(
            operator="*",
            left=Reference(parts=("b",)),
            right=Literal(value=2),
        ),
    )


def test_parentheses_override_precedence() -> None:
    expression = parse_expression("(a + b) * 2")
    assert isinstance(expression, BinaryOperation)
    assert expression.operator == "*"
    assert isinstance(expression.left, BinaryOperation)
```

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```bash
uv run pytest -q tests/expressions/test_parser.py
```

Expected: collection fails because `selayer.expressions` does not exist.

- [ ] **Step 3: Implement immutable expression nodes and syntax error**

```python
# src/selayer/expressions/ast.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

Scalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Literal:
    value: Scalar


@dataclass(frozen=True, slots=True)
class Reference:
    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnaryOperation:
    operator: str
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryOperation:
    operator: str
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str
    arguments: tuple[Expression, ...]


Expression: TypeAlias = (
    Literal | Reference | UnaryOperation | BinaryOperation | FunctionCall
)


class ExpressionSyntaxError(ValueError):
    def __init__(self, expression: str, offset: int, message: str) -> None:
        self.expression = expression
        self.offset = offset
        self.message = message
        super().__init__(f"expression error at offset {offset}: {message}")
```

- [ ] **Step 4: Implement a tokenizer and recursive-descent parser**

Implement `src/selayer/expressions/parser.py` with these exact parser methods: `parse() -> Expression`, `parse_comparison() -> Expression`, `parse_additive() -> Expression`, `parse_multiplicative() -> Expression`, `parse_unary() -> Expression`, `parse_primary() -> Expression`, and `parse_arguments() -> tuple[Expression, ...]`.

The public entrypoint is exactly:

```python
def parse_expression(source: str) -> Expression:
    return Parser(tokenize(source), source).parse()
```

Tokenize identifiers, qualified identifiers, decimal/integer literals, quoted strings with backslash escapes, booleans, null, arithmetic operators, comparisons, commas, and parentheses. Reject comments, semicolons, SQL keywords, attribute chains longer than two parts, unknown characters, and trailing tokens with `ExpressionSyntaxError` at the first invalid offset.

- [ ] **Step 5: Add complete syntax acceptance and rejection tests**

Add parameterized tests covering unary operators, every comparison, strings, booleans, null, function calls, qualified references, repeated references, comments, semicolons, `SELECT`, `FROM`, three-part references, unknown characters, missing parentheses, and trailing tokens.

```python
@pytest.mark.parametrize("source", ["a; b", "SELECT a", "a -- comment", "a.b.c"])
def test_rejects_non_dsl_syntax(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)
```

- [ ] **Step 6: Run parser tests, lint, and types**

```bash
uv run pytest -q tests/expressions/test_parser.py
uv run ruff check src/selayer/expressions tests/expressions
uv run pyright src/selayer/expressions tests/expressions
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/selayer/expressions tests/expressions
git commit -m "feat(expressions): add semantic expression parser"
```

---

### Task 2: Replace the catalog model and validate expression symbols

**Files:**

- Create: `src/selayer/_next/__init__.py`
- Create: `src/selayer/_next/model.py`
- Create: `src/selayer/_next/catalog.py`
- Create: `src/selayer/expressions/validation.py`
- Modify: `src/selayer/expressions/__init__.py`
- Create: `tests/next/conftest.py`
- Create: `tests/next/test_catalog.py`
- Create: `tests/expressions/test_validation.py`

**Interfaces:**

- Produces staged frozen `DataSource`, `Dimension`, `Relationship`, `Fact`, `Measure`, `Metric`, `SemanticLayer` under `selayer._next`.
- Produces `CatalogIssue(path: str, message: str)` and `CatalogValidationError(issues: tuple[CatalogIssue, ...])`.
- Produces `validate_row_expression` and `validate_metric_expression` with the exact symbol environments defined below.
- Staged `SemanticLayer.load(path)` either returns a fully valid immutable layer or raises one sorted aggregate error.
- Leaves the currently exported model and runtime untouched until Task 5.

- [ ] **Step 1: Write failing model and catalog tests**

```python
def test_catalog_requires_schema_version_one(tmp_path: Path) -> None:
    path = tmp_path / "layer.yaml"
    path.write_text("version: 2\nname: bad\ndata_sources: {}\n")
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert caught.value.issues == (
        CatalogIssue(path="version", message="expected schema version 1"),
    )


def test_catalog_collects_and_sorts_multiple_issues(tmp_path: Path) -> None:
    path = tmp_path / "layer.yaml"
    path.write_text(
        "version: 1\nname: Bad Name\ndata_sources:\n"
        "  orders:\n    type: parquet\n    path: x\n    grain: []\n"
    )
    with pytest.raises(CatalogValidationError) as caught:
        SemanticLayer.load(path)
    assert list(caught.value.issues) == sorted(
        caught.value.issues, key=lambda issue: (issue.path, issue.message)
    )
```

- [ ] **Step 2: Run catalog tests and verify RED**

```bash
uv run pytest -q tests/next/test_catalog.py tests/expressions/test_validation.py
```

Expected: failures because the old mutable model accepts no version or grain.

- [ ] **Step 3: Implement frozen schema-version-1 model types**

Implement the approved interfaces in `src/selayer/_next/model.py` and add `Aggregation` and `Cardinality` literals:

```python
Aggregation = Literal["sum", "avg", "min", "max", "count", "count_distinct"]
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
```

`SemanticLayer` stores each collection as `Mapping[str, T]` wrapped with `MappingProxyType`. Add lookup helpers that raise deterministic `KeyError` only for internal programmer mistakes; user-facing catalog and planning code must convert lookup failures into domain errors.

- [ ] **Step 4: Implement strict YAML parsing and aggregate issue collection**

`selayer._next.catalog.SemanticLayer.load(path: str | Path) -> SemanticLayer` must:

1. parse YAML safely;
2. validate required top-level and object fields;
3. validate identifiers with `[a-z][a-z0-9_]*`;
4. parse every fact and metric expression;
5. resolve all references;
6. validate relationship endpoints/cardinalities;
7. collect all independent issues;
8. sort by `(path, message)`;
9. raise one `CatalogValidationError` when any issue exists;
10. return only immutable objects when no issue exists.

- [ ] **Step 5: Implement row and metric expression validation**

Use these constants and signatures:

```python
ROW_FUNCTIONS = frozenset({"abs", "coalesce", "if", "lower", "nullif", "upper"})
METRIC_FUNCTIONS = frozenset({"abs", "coalesce", "nullif"})
```

- `references(expression: Expression) -> tuple[Reference, ...]` walks nodes depth-first from left to right.
- `validate_row_expression(expression: Expression, sources: frozenset[str]) -> tuple[str, ...]` returns sorted issue messages.
- `validate_metric_expression(expression: Expression, declared_measures: frozenset[str]) -> tuple[str, ...]` returns sorted issue messages.

Metric validation requires every one-part reference to be declared and requires the set of actual measure references to equal the declared set. Row validation permits exactly two-part source-field references, verifies the source exists, and defers relationship reachability to the planner task.

- [ ] **Step 6: Add tests for every catalog rule**

Cover missing fields, invalid IDs, duplicate IDs, unknown source/fact/measure references, empty grains, unsupported aggregation/cardinality, invalid expression syntax, unknown functions, metric declaration mismatch, and deterministic ordering.

- [ ] **Step 7: Run the Task 2 suite and full regression suite**

```bash
uv run pytest -q tests/next tests/expressions
uv run pytest -q
uv run ruff check src tests
uv run pyright src tests
```

All commands must exit zero. The old runtime remains untouched and green while the breaking model is staged under `_next`; mark no tests as skipped.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/selayer/_next src/selayer/expressions tests/next tests/expressions

git commit -m "feat(catalog): stage immutable grain-aware schema"
```

---

### Task 3: Build the typed grain-aware planner

**Files:**

- Create: `src/selayer/planning/types.py`
- Create: `src/selayer/planning/planner.py`
- Create: `src/selayer/planning/__init__.py`
- Create: `tests/planning/test_planner.py`

**Interfaces:**

- Produces `QueryRequest`, `JoinStep`, `PlannedDimension`, `PlannedMeasure`, `PlannedMetric`, `PlannedFilter`, `QueryPlan`.
- Produces `QueryPlanningError(code: str, message: str)`.
- Produces `plan_query(layer: selayer._next.model.SemanticLayer, request: QueryRequest) -> QueryPlan`.
- Plan objects contain resolved objects/expressions and no unresolved catalog identifiers.
- The temporary `_next` import is replaced with `selayer.model` during Task 5.

- [ ] **Step 1: Write failing happy-path planner test**

```python
def test_plans_item_margin_by_product_category(ecommerce_layer: SemanticLayer) -> None:
    plan = plan_query(
        ecommerce_layer,
        QueryRequest(metrics=("gross_margin",), dimensions=("product_category",)),
    )
    assert plan.anchor_source == "order_items"
    assert [join.relationship_id for join in plan.joins] == [
        "product_order_items"
    ]
    assert [measure.id for measure in plan.measures] == [
        "total_item_revenue",
        "total_item_cost",
    ]
```

- [ ] **Step 2: Run planner test and verify RED**

```bash
uv run pytest -q tests/planning/test_planner.py
```

Expected: collection failure because planning modules do not exist.

- [ ] **Step 3: Implement immutable plan and request types**

```python
@dataclass(frozen=True, slots=True)
class QueryRequest:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: Mapping[str, FilterValue] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class QueryPlan:
    anchor_source: str
    joins: tuple[JoinStep, ...]
    dimensions: tuple[PlannedDimension, ...]
    measures: tuple[PlannedMeasure, ...]
    metrics: tuple[PlannedMetric, ...]
    filters: tuple[PlannedFilter, ...]
```

Define `FilterValue` as scalar, list-like tuple, or two-value range represented by explicit immutable `ScalarFilter`, `ListFilter`, and `RangeFilter` variants during request normalization.

- [ ] **Step 4: Implement deterministic relationship graph traversal**

The planner sorts relationships by ID, computes all shortest paths, rejects no-path and multiple equal-length paths, and marks traversal as row-expanding using:

```python
def expands_rows(relationship: Relationship, current_source: str) -> bool:
    if relationship.type == "one_to_one":
        return False
    if relationship.type == "one_to_many":
        return current_source == relationship.source
    if relationship.type == "many_to_one":
        return current_source == relationship.target
    return True
```

- [ ] **Step 5: Implement request planning**

Resolve metrics to measures and facts, require one anchor source, collect source-field references from fact expressions, resolve dimension/filter sources, compute and deduplicate joins, reject row-expanding paths, and preserve requested output order. Use stable error codes:

```text
unknown_metric
unknown_dimension
unknown_filter_dimension
mixed_grain
no_relationship_path
ambiguous_relationship_path
row_expanding_path
```

- [ ] **Step 6: Add planner failure and determinism tests**

Cover empty metrics, every unknown identifier, mixed grains, no path, ambiguous equal paths, one-to-many fan-out, many-to-many, filter-only joins, join deduplication, mapping insertion-order independence, and stable output order.

- [ ] **Step 7: Run planner and active suites**

```bash
uv run pytest -q tests/planning
uv run pytest -q tests/expressions tests/test_catalog.py tests/planning
uv run ruff check src/selayer/planning tests/planning
uv run pyright src/selayer/planning tests/planning
```

Expected: all commands exit zero.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/selayer/planning tests/planning
git commit -m "feat(planning): add grain-aware query planner"
```

---

### Task 4: Compile validated plans to parameterized DuckDB SQL

**Files:**

- Create: `src/selayer/compilation/duckdb.py`
- Create: `src/selayer/compilation/__init__.py`
- Create: `tests/compilation/test_duckdb.py`

**Interfaces:**

- Produces `CompiledQuery(sql: str, parameters: tuple[object, ...])`.
- Produces `compile_duckdb(plan: QueryPlan) -> CompiledQuery`.
- Compiler consumes only resolved plan objects and never reads `SemanticLayer`.

- [ ] **Step 1: Write failing compiler structure test**

```python
def test_compiles_metrics_outside_aggregate_cte(item_margin_plan: QueryPlan) -> None:
    compiled = compile_duckdb(item_margin_plan)
    assert compiled.sql.startswith("WITH aggregated AS (")
    assert 'SUM("order_items"."total") AS "total_item_revenue"' in compiled.sql
    assert 'AS "gross_margin"' in compiled.sql.split(") SELECT", maxsplit=1)[1]
```

- [ ] **Step 2: Run compiler tests and verify RED**

```bash
uv run pytest -q tests/compilation/test_duckdb.py
```

Expected: collection failure because compilation modules do not exist.

- [ ] **Step 3: Implement expression compilation with typed symbol resolvers**

Use separate functions:

Implement these exact compiler interfaces:

- `compile_row_expression(expression: Expression) -> str`
- `compile_metric_expression(expression: Expression) -> str`
- `quote_identifier(identifier: str) -> str`

Each function exhaustively matches the immutable AST node variants and returns SQL only for validated nodes.

Row references compile from `source.column`; metric references compile as aggregate CTE aliases. Compile only known AST node classes and allowlisted function names. Raise an internal assertion for impossible validated nodes rather than accepting arbitrary SQL.

- [ ] **Step 4: Implement aggregate CTE and outer metric projection**

Compile dimensions first, then measures in resolved plan order. Group with positional indexes. Compile metric formulas in the outer projection so aggregate aliases are available and aggregations are never repeated after joins.

- [ ] **Step 5: Implement bound filter compilation**

- Scalar: `column = ?`.
- Range: `column BETWEEN ? AND ?`.
- Non-empty list: `column IN (?, ...)`.
- Empty list: `FALSE`.

Append parameters in SQL occurrence order. Never include `repr(value)` or `str(value)` in SQL or exceptions.

- [ ] **Step 6: Add complete compiler tests**

Cover all AST operators/functions, quoted identifiers containing quotes, every aggregation, all filter variants, multiple filters and parameter order, shared joins, stable output order, and proof that malicious filter strings appear only in `parameters`.

- [ ] **Step 7: Run compiler checks**

```bash
uv run pytest -q tests/compilation
uv run ruff check src/selayer/compilation tests/compilation
uv run pyright src/selayer/compilation tests/compilation
```

Expected: all commands exit zero.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/selayer/compilation tests/compilation
git commit -m "feat(duckdb): compile grain-safe query plans"
```

---

### Task 5: Integrate planning and compilation into QueryEngine

**Files:**

- Move: `src/selayer/_next/model.py` → `src/selayer/model.py`
- Move: `src/selayer/_next/catalog.py` → `src/selayer/catalog.py`
- Delete: `src/selayer/_next/__init__.py`
- Replace: `src/selayer/query.py`
- Modify imports: `src/selayer/planning/planner.py`
- Modify: `src/selayer/__init__.py`
- Replace: `tests/conftest.py`
- Replace: `tests/test_catalog.py`
- Replace: `tests/test_query.py`
- Remove staging tests: `tests/next/`

**Interfaces:**

- Preserves `QueryEngine.query(metrics, dimensions=None, filters=None) -> polars.DataFrame`.
- Adds `QueryEngine.plan(metrics: list[str], dimensions: list[str] | None = None, filters: dict[str, object] | None = None) -> QueryPlan` for inspection and testing.
- Adds `QueryExecutionError(query_id: str, message: str)` without parameter values.

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_query_engine_exposes_resolved_plan(ecommerce_engine: QueryEngine) -> None:
    plan = ecommerce_engine.plan(["gross_margin"], ["product_category"])
    assert plan.anchor_source == "order_items"


def test_query_engine_executes_compiled_query(ecommerce_engine: QueryEngine) -> None:
    result = ecommerce_engine.query(["gross_margin"], ["product_category"])
    assert result.columns == ["product_category", "gross_margin"]
```

- [ ] **Step 2: Run QueryEngine tests and verify RED**

```bash
uv run pytest -q tests/test_query.py
```

Expected: failures because the existing engine owns compilation and rejects the now-valid item-grain query.

- [ ] **Step 3: Promote the staged model and catalog atomically**

Move `_next/model.py` and `_next/catalog.py` to their final package paths, remove `_next`, update planner imports, promote relevant `tests/next` coverage into `tests/test_catalog.py`, and update shared fixtures. Run `uv run pytest -q tests/test_catalog.py tests/planning tests/compilation` before replacing QueryEngine.

- [ ] **Step 4: Replace QueryEngine with orchestration-only implementation**

`QueryEngine.__init__` loads every source into one DuckDB connection. `plan()` normalizes mutable caller collections into immutable `QueryRequest` variants and calls `plan_query`. `query()` calls `plan()`, `compile_duckdb()`, executes with bound parameters, and returns `.pl()`.

Keep context-manager cleanup. Remove old `_compile_metric`, `_compile_filters`, `_find_join_path`, and expression-evaluation SQL paths from `query.py`.

- [ ] **Step 5: Implement execution error handling**

Generate a random query ID with `uuid.uuid4()`. Wrap `duckdb.Error` as `QueryExecutionError`. Include generated SQL only when it contains no parameter values; never include the parameter tuple.

- [ ] **Step 6: Curate public exports**

Export the domain model, catalog/planning errors, `QueryPlan`, and `QueryEngine`. Do not export compiler internals or parser implementation classes beyond the approved expression node interface.

- [ ] **Step 7: Run QueryEngine and full active tests**

```bash
uv run pytest -q tests/test_query.py
uv run pytest -q
uv run ruff check src tests
uv run pyright src tests
```

Expected: all core, planner, compiler, and query tests pass. The example integration test is introduced and migrated in Task 6; no existing test may fail.

- [ ] **Step 8: Commit Task 5**

```bash
git add src/selayer/model.py src/selayer/catalog.py src/selayer/query.py src/selayer/planning src/selayer/__init__.py tests
git commit -m "refactor(core): activate typed query planning"
```

---

### Task 6: Migrate the e-commerce catalog and prove numerical correctness

**Files:**

- Replace: `ecommerce_semantic_layer.yaml`
- Modify: `examples/e_commerce/selayer1.py`
- Create: `tests/integration/test_ecommerce.py`
- Modify: `README.md`
- Modify: `.github/copilot-instructions.md`

**Interfaces:**

- Produces schema-version-1 example catalog.
- Proves product-category item revenue, item cost, units sold, average item price, and gross margin against independent Polars calculations.

- [ ] **Step 1: Write failing numerical integration tests**

```python
def expected_product_metrics(root: Path) -> pl.DataFrame:
    items = pl.read_parquet(root / "data/order_items.parquet")
    products = pl.read_parquet(root / "data/products.parquet")
    return (
        items.join(products, left_on="product_id", right_on="id")
        .with_columns(
            (pl.col("quantity") * pl.col("cost")).alias("item_cost")
        )
        .group_by("category")
        .agg(
            pl.col("total").sum().alias("total_item_revenue"),
            pl.col("item_cost").sum().alias("total_item_cost"),
        )
        .with_columns(
            (
                (pl.col("total_item_revenue") - pl.col("total_item_cost"))
                / pl.col("total_item_revenue")
            ).alias("gross_margin")
        )
        .sort("category")
    )


def test_gross_margin_by_product_category_matches_polars(root: Path) -> None:
    engine = QueryEngine(SemanticLayer.load(root / "ecommerce_semantic_layer.yaml"))
    actual = engine.query(
        ["gross_margin"], ["product_category"]
    ).sort("product_category")
    expected = expected_product_metrics(root)
    assert actual["gross_margin"].to_list() == pytest.approx(
        expected["gross_margin"].to_list()
    )
```

- [ ] **Step 2: Run integration test and verify RED**

```bash
uv run pytest -q tests/integration/test_ecommerce.py
```

Expected: catalog loading fails because the example still uses the old schema.

- [ ] **Step 3: Rewrite the example catalog directly to schema version 1**

Declare grains for every source. Define item-grain facts and measures exactly as approved:

```yaml
facts:
  item_quantity:
    source: order_items
    data_type: integer
    expression: order_items.quantity
  item_revenue:
    source: order_items
    data_type: decimal
    expression: order_items.total
  item_cost:
    source: order_items
    data_type: decimal
    expression: order_items.quantity * products.cost
```

Keep order metrics anchored at `orders`. Do not combine order- and item-grain measures in one metric. Add safe relationships enabling `order_items → orders → customers` and `order_items → products` traversal.

- [ ] **Step 4: Add full numerical and classification coverage**

Numerically compare overall order metrics, product-category item metrics, and item metrics by customer and order date. Enumerate every supported example metric/dimension combination and assert either the expected result or the exact planning error code.

- [ ] **Step 5: Update example script and documentation**

Show catalog loading, plan inspection, order-level queries, product-category gross margin, filters, and expected mixed-grain rejection. Document grain, anchor source, safe traversal, expression DSL, and the distinction between order revenue and item revenue. State explicitly that OKF and re-graining are out of scope.

- [ ] **Step 6: Run complete verification**

```bash
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests examples
uv run python examples/e_commerce/selayer1.py
rm -rf dist && uv build
python - <<'PY'
from pathlib import Path
import zipfile
wheel = next(Path("dist").glob("*.whl"))
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
assert any(name == "selayer/expressions/__init__.py" for name in names)
assert any(name == "selayer/planning/__init__.py" for name in names)
assert any(name == "selayer/compilation/__init__.py" for name in names)
assert not any("legacy/" in name or "selayer_chat" in name for name in names)
print(f"verified {wheel}: {len(names)} files")
PY
```

Expected: tests, Ruff, Pyright, example, build, and wheel assertions all exit zero.

- [ ] **Step 7: Request independent code review and resolve findings**

Review against `docs/superpowers/specs/2026-07-27-grain-aware-query-planning-design.md`, focusing on numerical correctness, grain safety, DSL escape paths, deterministic planning, SQL parameterization, and complete requirement coverage. Fix all critical and important issues and repeat review until no issues remain.

- [ ] **Step 8: Commit Task 6**

```bash
git add ecommerce_semantic_layer.yaml examples README.md .github tests/integration
git commit -m "feat(example): demonstrate grain-aware product analytics"
```

---

## Completion audit

Before declaring the plan complete, map every design requirement to evidence:

- Mandatory source grain → catalog tests and schema-version-1 example.
- Restricted row and metric DSL → parser and environment-validation tests.
- Stable typed identifiers → catalog tests and public documentation.
- Same-grain metrics → planner mixed-grain tests.
- Safe joins only → planner cardinality/path tests.
- Correct product margin → independent Polars numerical integration test.
- Engine-neutral planning → planner has no DuckDB import.
- Compiler isolation → compiler consumes `QueryPlan`, not `SemanticLayer`.
- Bound filters → compiler SQL/parameter separation tests.
- Small execution interface → QueryEngine orchestration tests.
- No OKF, re-graining, or extra engines → dependency/code search and review.
- Package integrity → wheel-content assertion.
- Repository quality → full pytest, Ruff, Pyright, example, and build verification.
