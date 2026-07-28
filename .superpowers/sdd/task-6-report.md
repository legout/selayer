# Task 6 Report: E-commerce catalog migration and numerical proof

## Status

Implemented the grain-aware schema-version-1 e-commerce catalog, migrated the
example and repository guidance, and added independent Polars integration tests.
No OKF, re-graining, allocation, additional compiler, or active-package changes
were added.

## RED evidence

The integration test was created before the catalog migration and run with:

```text
uv run pytest -q tests/integration/test_ecommerce.py
```

Result: **7 failed**. Every failure occurred while loading the legacy catalog.
`CatalogValidationError` reported 22 expected migration issues, including the
missing schema version, invalid catalog name, missing source grains, missing fact
expressions, and legacy `{{...}}` metric expressions. This established that the
new tests exercised the requested schema-v1 behavior rather than already-working
behavior.

## GREEN evidence

After rewriting `ecommerce_semantic_layer.yaml` directly to schema version 1:

```text
uv run pytest -q tests/integration/test_ecommerce.py
.......                                                                  [100%]
7 passed in 1.06s
```

The focused integration suite was repeated after the example and documentation
migration and passed again (`7 passed in 0.77s`).

## Numerical comparison details

`tests/integration/test_ecommerce.py` builds deterministic parquet fixtures and
calculates expected results directly with Polars, independently of selayer's
planner, DuckDB compiler, and runtime.

- Product-category coverage independently joins `order_items` to `products`,
  calculates `quantity * cost`, aggregates item revenue, item cost, and units,
  and derives average item price and gross margin. For example, Books produces
  revenue `310`, cost `180`, units `7`, average item price `310 / 7`, and gross
  margin `130 / 310`; Electronics produces revenue `40`, cost `25`, units `1`,
  average price `40`, and margin `15 / 40`.
- Overall order coverage independently verifies revenue `350`, order count `3`,
  average order value `350 / 3`, discount rate `35 / 350`, and completion rate
  `2 / 3`.
- Safe multi-hop coverage compares item metrics grouped by customer segment and
  order timestamp after independent `order_items -> orders -> customers` and
  `order_items -> products` Polars joins.
- Filter coverage verifies the Books-only revenue, units, and margin values.
- Exhaustive classification locks the catalog at 13 metrics and 9 dimensions.
  All 93 safe metric/dimension pairs are numerically compared to Polars; all 24
  order-metric/product-dimension fan-out pairs must raise
  `row_expanding_path`.
- All 40 order-metric/item-metric combinations must raise `mixed_grain`.

The repository-data test separately loads the checked-in catalog and compares
product-category gross margins to an independent Polars computation.

## Complete checks

```text
uv run pytest -q
Pytest: 243 passed

uv run ruff format --check src tests examples
26 files already formatted

uv run ruff check src tests examples
[]

uv run pyright src tests examples
0 errors, 0 warnings, 0 informations

uv run python examples/e_commerce/selayer1.py
Loaded schema version 1; printed the orders anchor, order query, product-category
margin, filtered item query, and expected `mixed_grain` rejection.

rm -rf dist && uv build
Successfully built the sdist and wheel.

wheel-content assertion
verified dist/selayer-0.1.0-py3-none-any.whl: 18 files
```

The wheel contains the expression, planning, and compilation packages and does
not contain `legacy/` or `selayer_chat` artifacts.

## Files

- `ecommerce_semantic_layer.yaml`: replaced with the schema-v1, grain-aware
  catalog and safe relationships.
- `examples/e_commerce/selayer1.py`: migrated to catalog loading, plan
  inspection, order/item queries, filtering, and mixed-grain rejection.
- `tests/integration/test_ecommerce.py`: added independent numerical and
  rejection coverage.
- `README.md`: documented grains, anchors, traversal, DSL, typed identifiers,
  order versus item revenue, and explicit non-goals.
- `.github/copilot-instructions.md`: updated active architecture and development
  conventions.
- `.superpowers/sdd/task-6-report.md`: this evidence report.

## Self-review

- Confirmed every retained source declares a non-empty grain.
- Confirmed the three approved item facts are anchored at `order_items` and use
  the required expressions.
- Confirmed order measures remain anchored at `orders` and no metric combines
  order and item measures.
- Confirmed relationship directions permit only safe many-to-one traversal from
  item grain and reject order-to-item fan-out.
- Confirmed metric formulas use the restricted DSL and guard zero denominators
  with `nullif`.
- Confirmed filters remain bound through the existing compiler interface.
- Confirmed no OKF or other out-of-scope behavior was introduced.

## Concerns

None. The required external review gate remains an orchestration/reviewer step;
this task did not launch a reviewer, per the no-subagent instruction.
