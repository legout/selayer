# selayer Repository Cleanup Design

## Purpose

Turn this repository into a focused, independently installable semantic-layer library. Preserve the existing chat backend and UI applications as unsupported legacy artifacts without packaging or maintaining them as part of `selayer`.

## Repository structure

```text
selayer/
├── src/selayer/
│   ├── __init__.py
│   ├── model.py
│   ├── catalog.py
│   ├── query.py
│   └── py.typed
├── tests/
├── examples/e_commerce/
├── legacy/
│   ├── README.md
│   ├── selayer_chat/
│   ├── apps/
│   ├── scripts/
│   └── example_questions.md
├── docs/
├── pyproject.toml
└── uv.lock
```

## Package interface

`src/selayer/__init__.py` remains the curated public interface and continues to export `DataSource`, `Fact`, `Measure`, `Dimension`, `Hierarchy`, `Metric`, `Relationship`, `SemanticLayer`, and `QueryEngine`. Existing `from selayer import ...` imports and the current YAML representation remain compatible.

The implementation is split into three deep modules:

- `model.py`: semantic model dataclasses and data-source loading.
- `catalog.py`: catalog mutation, serialization, loading, and Mermaid rendering.
- `query.py`: deterministic DuckDB query compilation and execution.

## Legacy policy

The committed `selayer_chat` package, UI applications, chat smoke scripts, endpoint probe, and chat example questions move under `legacy/`. `legacy/README.md` states that these files are historical, unsupported, excluded from the wheel, excluded from lint/test scope, and may be extracted into another project later.

## Packaging and dependencies

The wheel contains only `src/selayer`. Runtime dependencies are limited to DuckDB, Polars, PyArrow, and PyYAML. OpenAI, PydanticAI, Plotly, dotenv, Streamlit, Gradio, Panel, and Marimo are removed from the active package dependency graph. The development group includes pytest, Ruff, and Pyright.

## Query behavior

The cleanup fixes known correctness defects while retaining the existing `QueryEngine.query(metrics, dimensions, filters)` interface:

- unknown metrics and dimensions fail with explicit `ValueError` messages;
- empty metric lists fail explicitly;
- grouping uses SQL select positions, avoiding alias/column collisions;
- required filter tables participate in join planning;
- filter values use DuckDB parameters instead of SQL string interpolation;
- filters must refer to declared dimensions;
- join planning is deterministic and raises when no relationship path exists instead of silently cross joining;
- the unimplemented Polars engine option is rejected explicitly.

## Testing

Tests cover public exports, YAML/JSON round trips, file loading, Mermaid rendering, measure SQL, metric-only queries, all metric/dimension combinations in the example catalog, validation failures, parameterized filters, relationship joins, and unsupported engines. Characterization tests are written before the module split; defect regression tests are observed failing before fixes.

## Repository hygiene

Generated query-result CSV files, `.ruff_cache` directories, `.DS_Store`, build products, and local transient state are removed or ignored. Committed example datasets and `.env.example` remain. Local agent configuration directories are ignored rather than deleted.

## Non-goals

- Creating a uv workspace in this repository.
- Moving legacy code into SemaLoom.
- Changing the semantic YAML schema.
- Adding OKF, LLM, HTTP, MCP, UI, or remote catalog behavior.
- Implementing a Polars query engine.
