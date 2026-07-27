# Task 3 Final Fix Pass

## Changes

- Filter variants now defensively freeze runtime-invalid mutable values in `__post_init__`; `QueryRequest` reconstructs prebuilt variants at its normalization boundary.
- Added independent matrix tests for defensive immutability, shared join deduplication, relationship insertion-order determinism, requested output ordering, filter mapping determinism, stable invalid identifier diagnostics, and mixed-grain sources.
- Identifier validation is deterministic while planned metrics and dimensions retain caller order.

## Evidence

- `uv run pytest -q tests/planning`: 18 passed.
- `uv run pytest -q tests/expressions tests/test_catalog.py tests/planning`: 318 passed.
- `uv run pytest -q`: 391 passed.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 27 files already formatted.
- `uv run pyright src tests`: 0 errors, 0 warnings, 0 informations.

## Scope

Only planning types/planner and planning tests changed. `.superpowers` is not tracked.
