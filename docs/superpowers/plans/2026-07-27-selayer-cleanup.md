# selayer Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a clean, selayer-only Python package with tested query behavior while preserving chat/UI code under `legacy/`.

**Architecture:** Keep the public package interface stable while extracting model, catalog, and query responsibilities from the monolithic module. Archive chat/UI code outside the build and reduce dependencies to those used by the semantic library.

**Tech Stack:** Python 3.13+, uv, Hatchling, DuckDB, Polars, PyArrow, PyYAML, pytest, Ruff, Pyright.

## Global Constraints

- Preserve `from selayer import ...` imports.
- Preserve the current semantic YAML representation.
- Package only `src/selayer`.
- Keep legacy chat/apps in this repository but unsupported and excluded from normal tooling.
- Do not add workspace, SemaLoom, OKF, LLM, HTTP, MCP, or UI behavior.
- Do not implement a Polars query engine.

---

### Task 1: Add characterization and defect tests

**Files:**

- Create: `tests/conftest.py`
- Create: `tests/test_catalog.py`
- Create: `tests/test_query.py`

**Interfaces:**

- Consumes: current public imports from `selayer`.
- Produces: executable behavior contract for all later tasks.

- [ ] Write fixtures that create related customer/order Parquet sources and a semantic layer.
- [ ] Add passing characterization tests for serialization, loading, Mermaid output, measure SQL, and simple queries.
- [ ] Run characterization tests and confirm they pass against the current implementation.
- [ ] Add regression tests for dimension alias collisions, unknown fields, safe filter values, missing join paths, and unsupported Polars execution.
- [ ] Run regression tests and confirm failures match the known defects.

### Task 2: Isolate legacy artifacts and simplify project configuration

**Files:**

- Move: `src/selayer_chat/` → `legacy/selayer_chat/`
- Move: `apps/` → `legacy/apps/`
- Move: `scripts/` → `legacy/scripts/`
- Move: `example_questions.md` → `legacy/example_questions.md`
- Create: `legacy/README.md`
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `uv.lock`

**Interfaces:**

- Consumes: no production interfaces.
- Produces: selayer-only wheel and documented legacy archive.

- [ ] Move tracked and untracked legacy source files without changing their contents.
- [ ] Document unsupported legacy status and historical run requirements.
- [ ] Remove chat/UI dependencies and extras; add pytest, Ruff, and Pyright to the dev group.
- [ ] Restrict Hatchling to `src/selayer` and configure pytest/Ruff to exclude `legacy/`.
- [ ] Update README to describe only selayer, with a short pointer to the archive.
- [ ] Extend `.gitignore` for tool caches, generated query outputs, and local agent state.
- [ ] Regenerate `uv.lock` and verify `uv build` contains only `selayer`.

### Task 3: Split the monolithic module without changing public imports

**Files:**

- Create: `src/selayer/model.py`
- Create: `src/selayer/catalog.py`
- Create: `src/selayer/query.py`
- Replace: `src/selayer/__init__.py`

**Interfaces:**

- Produces: the existing public names from `selayer.__init__`.
- Internal dependency direction: `model` ← `catalog` ← `query`, with type-only references where needed.

- [ ] Move data model dataclasses and source loading to `model.py`.
- [ ] Move `SemanticLayer` serialization and Mermaid behavior to `catalog.py`.
- [ ] Move `QueryEngine` to `query.py`.
- [ ] Re-export the stable public interface from `__init__.py`.
- [ ] Run characterization tests and confirm behavior remains green.

### Task 4: Fix deterministic query compilation

**Files:**

- Modify: `src/selayer/query.py`
- Test: `tests/test_query.py`

**Interfaces:**

- `QueryEngine.query(metrics: list[str], dimensions: list[str] | None = None, filters: dict[str, object] | None = None) -> polars.DataFrame`.

- [ ] Make empty/unknown metrics and unknown dimensions fail explicitly.
- [ ] Reject every engine type except `duckdb`.
- [ ] Include filter dimensions in required tables and reject undeclared filters.
- [ ] Compile filter values to `?` parameters, including ranges and lists.
- [ ] Use positional grouping to avoid alias collisions.
- [ ] Make join order deterministic and reject missing relationship paths instead of cross joining.
- [ ] Run each regression test after its minimal fix, then run the full suite.

### Task 5: Remove generated artifacts and verify the repository

**Files:**

- Delete generated root CSV outputs.
- Delete `.ruff_cache` and `.DS_Store` artifacts.
- Review all changed files.

**Interfaces:**

- Produces: clean source tree and reproducible verification evidence.

- [ ] Remove generated files covered by `.gitignore`.
- [ ] Run `uv run pytest -q` and require zero failures.
- [ ] Run `uv run ruff check src tests examples` and require zero findings.
- [ ] Run Pyright/LSP diagnostics for `src/selayer` and require zero errors.
- [ ] Run `uv build` and inspect wheel contents for only `selayer`.
- [ ] Run `git status --short` and report any intentionally retained unrelated user changes.
