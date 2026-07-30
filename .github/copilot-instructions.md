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

## Sources, profiles, and lifecycle

The closed source matrix is Parquet, CSV, programmatic PyArrow, Delta, Iceberg,
SQLite, DuckDB files, and PostgreSQL. S3 is only a named runtime transport
profile for file/lakehouse connectors. Optional extras are `delta`, `iceberg`,
`postgres`, `s3`, and `all`; keep their ranges synchronized with `pyproject.toml`
and `uv.lock`.

Every source has exactly one complete inline `schema` or contained `schema_ref`
and a non-empty `grain`. Runtime profiles hold credentials, DSNs, endpoints, and
other secrets only at execution time. Never put profile values, authenticated
locations, connector options, or observed schemas in catalogs, plans, OKF,
reprs, logs, statuses, or errors. The catalog declaration is execution
authority; OKF summaries are advisory and cannot change planning or execution.

`QueryEngine.reload_source()` and `reload_all()` are explicit. A failed single
reload preserves the old source, and `reload_all()` is all-or-nothing. Arrow
Dataset and Delta paths preserve projection/filter pushdown. PyIceberg owns
snapshot metadata and creates query-scoped projected/filterable readers. Native
SQLite, DuckDB-file, and PostgreSQL attachments are read-only and detached when
replaced or closed. Do not eagerly materialize sources into Polars; Polars is
only the result-frame boundary.

## Catalog and query conventions

- Schema version is exactly integer `1`; do not add compatibility loaders.
- Every source declares a non-empty `grain`.
- Catalog mapping keys are stable lowercase typed semantic identifiers.
- Facts use only the restricted row-expression DSL and declare an anchor source.
- Metric formulas reference only their declared measures and use the restricted
  metric-expression DSL.
- Measures in one metric must share one anchor source and grain.
- In the migrated e-commerce catalog, from an `order_items` anchor, only
  grain-preserving many-to-one traversal is supported (`order_items -> products`
  and `order_items -> orders -> customers`). This documents that catalog's
  current path restriction; it is not a hardcoded rule for the generic planner.
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
