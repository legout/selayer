# Copilot Instructions for `selayer`

## Scope

This repository contains only the installable `selayer` semantic-layer library.
Code under `legacy/` is unsupported historical reference material and must not
be imported, packaged, linted, or extended as active product code.

## Active modules

- `src/selayer/model.py`: immutable schema-version-1 semantic dataclasses.
- `src/selayer/catalog.py`: strict schema-version-1 catalog loading and validation.
- `src/selayer/expressions/`: restricted engine-neutral expression DSL.
- `src/selayer/planning/`: grain-aware typed query planning.
- `src/selayer/compilation/duckdb.py`: DuckDB SQL compilation from `QueryPlan`.
- `src/selayer/query.py`: `QueryEngine` orchestration and execution.
- `src/selayer/__init__.py`: curated public exports.
- `tests/`: behavior, regression, and integration tests.
- `examples/e_commerce/`: migrated schema-version-1 example catalog and script.

## Catalog and query conventions

- Schema version is exactly integer `1`; do not add compatibility loaders.
- Every source declares a non-empty `grain`.
- Catalog mapping keys are stable lowercase typed semantic identifiers.
- Facts use only the restricted row-expression DSL and declare an anchor source.
- Metric formulas reference only their declared measures and use the restricted
  metric-expression DSL.
- Measures in one metric must share one anchor source and grain.
- From an `order_items` anchor, only grain-preserving many-to-one traversal is
  supported (`order_items -> products` and `order_items -> orders -> customers`).
- Never add raw SQL, OKF, re-graining/allocation, many-to-many analytics, or an
  additional execution engine.
- Bind user-provided filter values as DuckDB parameters.
- Quote catalog identifiers and reject unknown metrics, dimensions, and filters.
- Do not silently cross join tables without a declared relationship path.

## Public interface

Use `SemanticLayer.load(path)` and `QueryEngine.query(metrics, dimensions,
filters)` (or `QueryEngine.plan(...)` for inspection). `QueryEngine` is the
small public orchestration boundary; compiler and parser internals are not
public exports.

## Workflow

Use uv for all dependency and command execution:

```bash
uv sync
uv run pytest -q
uv run ruff check src tests examples
uv run pyright src tests examples
uv build
```

Add a failing test before changing behavior or fixing a defect. Keep chat, LLM,
HTTP, MCP, UI, and SemaLoom concerns outside the active package.
