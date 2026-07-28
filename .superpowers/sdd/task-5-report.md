# Task 5 completion report

## Inherited partial state

The worktree began with a dirty partial cutover: the staged `_next` model/catalog and staging tests were already deleted, the grain-aware model/catalog had been promoted, planner imports had been changed, and catalog/planner/compiler fixtures and tests had been migrated. The public package still imported the removed `Hierarchy`, and `QueryEngine` was still the legacy compilation/join implementation. I preserved those valid edits, completed the promotion, and did not modify the example catalog, integration documentation, or README.

## Implementation

- Removed the remaining `_next` package and `tests/next` staging files.
- Added orchestration-only `QueryEngine.plan()` and `query()` around `plan_query()` and `compile_duckdb()`.
- QueryEngine loads parquet/CSV sources into one DuckDB connection, supports context-manager cleanup, and returns Polars frames.
- Added UUID-bearing `QueryExecutionError`; parameterized execution diagnostics do not include SQL, parameter values, or chained driver exceptions. Unparameterized failures include generated SQL.
- Curated top-level exports for the grain-aware model, catalog/planning errors, `QueryPlan`, and `QueryEngine`; compiler/parser internals remain unexported.
- Replaced the stale legacy query tests with the active grain-aware behavioral contract.

## Validation evidence

- `uv run pytest -q tests/test_catalog.py tests/planning tests/compilation` — 136 passed.
- `uv run pytest -q tests/test_query.py` — 6 passed.
- `uv run pytest -q` — 236 passed.
- `uv run ruff format --check src tests` — passed (23 files already formatted).
- `uv run ruff check src tests` — passed.
- `uv run pyright src tests` — 0 errors, 0 warnings, 0 informations.
- `git diff --check` — passed.

## Changed files

Production: `src/selayer/__init__.py`, `src/selayer/catalog.py`, `src/selayer/errors.py`, `src/selayer/model.py`, `src/selayer/planning/planner.py`, `src/selayer/planning/types.py`, `src/selayer/query.py`; removed `src/selayer/_next/*`.

Tests: `tests/conftest.py`, `tests/test_catalog.py`, `tests/test_query.py`, `tests/planning/test_planner.py`, `tests/compilation/test_duckdb.py`; removed `tests/next/*`.

## Self-review and concerns

No blockers found. The query execution boundary intentionally catches DuckDB failures without exception chaining so bound values cannot leak through traceback/context. Source-loading failures are similarly sanitized and close the connection before raising.
