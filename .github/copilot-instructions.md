# Copilot Instructions for `selayer`

## Scope

This repository contains only the installable `selayer` semantic-layer library. Code under `legacy/` is unsupported historical reference material and must not be imported, packaged, linted, or extended as active product code.

## Active modules

- `src/selayer/model.py`: semantic dataclasses and data-source loading.
- `src/selayer/catalog.py`: `SemanticLayer`, serialization, and Mermaid rendering.
- `src/selayer/query.py`: deterministic DuckDB query compilation and execution.
- `src/selayer/__init__.py`: curated public exports.
- `tests/`: behavior and regression tests.
- `examples/e_commerce/`: example catalog and data generation.

## Conventions

- Semantic objects reference one another by name.
- Preserve the current YAML and JSON representation unless a migration is explicitly designed.
- Preserve public imports from `selayer`; implementation modules are internal organization.
- Bind user-provided filter values as DuckDB parameters.
- Quote catalog identifiers and reject unknown metrics, dimensions, and filters.
- Do not silently cross join tables without a declared relationship path.
- DuckDB is the only implemented query engine.

## Workflow

Use uv for all dependency and command execution:

```bash
uv sync
uv run pytest
uv run ruff check src tests examples
uv run pyright src tests
uv build
```

Add a failing test before changing behavior or fixing a defect. Keep chat, LLM, HTTP, MCP, UI, and SemaLoom concerns outside the active package.
