# Task 2 Report — Staged immutable grain-aware catalog + expression validation

**Status:** DONE

## Commit

- `fd2efd8` — `feat(catalog): stage immutable grain-aware schema` on branch `feat/grain-aware-query-planning`

## Changed files

Created:
- `src/selayer/_next/__init__.py` — unexported staging namespace; re-exports the frozen model types, `CatalogIssue`, `CatalogValidationError`.
- `src/selayer/_next/model.py` — frozen schema-v1 model: `DataSource`, `Dimension`, `Fact`, `Measure`, `Metric`, `Relationship`, `SemanticLayer`; `Aggregation`/`Cardinality` literals; read-only `MappingProxyType` collections; lookup helpers raising deterministic `KeyError`; lazy `SemanticLayer.load` classmethod.
- `src/selayer/_next/catalog.py` — strict YAML loader (`load`), `CatalogIssue`, `CatalogValidationError`, the validation pipeline (safe parse + duplicate-key detection via the node tree, top-level/field validation, identifier checks, expression parsing, reference resolution, relationship endpoints/cardinalities, aggregate sorted issue collection, immutable construction).
- `src/selayer/expressions/validation.py` — `ROW_FUNCTIONS`, `METRIC_FUNCTIONS`, `references`, `validate_row_expression`, `validate_metric_expression`.
- `tests/next/conftest.py` — valid catalog YAML + `valid_catalog_path` fixture.
- `tests/next/test_catalog.py` — full catalog rule coverage.
- `tests/expressions/test_validation.py` — symbol-environment validation coverage.
- `tests/__init__.py`, `tests/next/__init__.py`, `tests/expressions/__init__.py` — package `__init__.py` files so pytest imports test modules by fully-qualified name (resolves the `test_catalog` basename collision between `tests/test_catalog.py` and `tests/next/test_catalog.py`; required by the brief's specified file layout).

Modified:
- `src/selayer/expressions/__init__.py` — exports the new validation symbols (`ROW_FUNCTIONS`, `METRIC_FUNCTIONS`, `references`, `validate_row_expression`, `validate_metric_expression`).

The currently exported runtime (`src/selayer/__init__.py`, `model.py`, `catalog.py`, `query.py`) is **untouched** (verified `git diff 59a48aa HEAD` over those four files is empty). `_next` is not re-exported from the top-level package.

## Interfaces produced (as required by the brief)

- Frozen `DataSource`, `Dimension`, `Relationship`, `Fact`, `Measure`, `Metric`, `SemanticLayer` under `selayer._next`.
- `Aggregation = Literal["sum","avg","min","max","count","count_distinct"]`, `Cardinality = Literal["one_to_one","one_to_many","many_to_one","many_to_many"]`.
- `CatalogIssue(path: str, message: str)` and `CatalogValidationError(issues: tuple[CatalogIssue, ...])`.
- `references(expression) -> tuple[Reference, ...]` (depth-first, left-to-right).
- `validate_row_expression(expression, sources: frozenset[str]) -> tuple[str, ...]`.
- `validate_metric_expression(expression, declared_measures: frozenset[str]) -> tuple[str, ...]`.
- `ROW_FUNCTIONS = frozenset({"abs","coalesce","if","lower","nullif","upper"})`, `METRIC_FUNCTIONS = frozenset({"abs","coalesce","nullif"})`.
- `SemanticLayer.load(path)` returns a fully valid immutable layer or raises one sorted aggregate error.

`Relationship.cardinality` (Python field) maps from the YAML `type` key; the `Cardinality` literal name is the source of truth. `many_to_many` is accepted by the catalog (planning it is deferred to the planner task).

## Binding constraints honored

- schema version exactly 1 (`version != 1` → issue).
- mandatory non-empty source grain (grain required + non-empty + list-of-strings).
- immutable mappings (frozen dataclasses + `MappingProxyType` collections).
- stable lowercase IDs (`[a-z][a-z0-9_]*` for the catalog `name` and every collection key).
- same-grain and relationship reachability deferred to the planner (`validate_row_expression` only checks the source exists; no grain/join analysis).
- no compatibility shims, no OKF, no allocation/re-graining, no many-to-many planning, no raw SQL, no extra engines. `_next` is temporary unexported staging.

## RED evidence (tests written first, run before implementation)

```
$ uv run pytest -q tests/next/test_catalog.py tests/expressions/test_validation.py
...
tests/next/test_catalog.py:17: in <module>
    from selayer._next import ( ... )
E   ModuleNotFoundError: No module named 'selayer._next'
tests/expressions/test_validation.py:13: in <module>
    from selayer.expressions.validation import ( ... )
E   ModuleNotFoundError: No module named 'selayer.expressions.validation'
2 errors in 0.09s
```

This matches the brief's Step 2 expectation (collection fails because the staging modules do not exist). A second RED iteration occurred after the initial GREEN: `get_args(Aggregation)` returned `()` for the PEP 695 `type` alias, so valid aggregations/cardinalities were flagged "unsupported"; fixed by reading `__value__`. Final pass added the package `__init__.py` files after the full suite hit the `test_catalog` basename collision.

## Exact test/check commands and results

| Command | Result |
| --- | --- |
| `uv run pytest -q tests/next tests/expressions` (RED, pre-impl) | failed — collection error: `No module named 'selayer._next'` / `selayer.expressions.validation` |
| `uv run pytest -q tests/next tests/expressions` (GREEN) | **passed — 341 passed** |
| `uv run pytest -q` (full regression suite) | **passed — 361 passed** (292 prior + 69 new) |
| `uv run ruff check src tests` | **passed — All checks passed!** |
| `uv run ruff format --check src tests` | **passed — 21 files already formatted** |
| `uv run pyright src tests` | **passed — 0 errors, 0 warnings, 0 informations** |

All commands exit zero. No tests are skipped. The existing 20 runtime tests (`tests/test_catalog.py`, `tests/test_query.py`) remain green and unchanged.

## Self-review findings

- **Immutability**: every model node is `@dataclass(frozen=True, slots=True)`; collections are `MappingProxyType`; mutation raises `TypeError`/`FrozenInstanceError` (pinned by `test_catalog_collections_are_immutable` and `test_catalog_model_objects_are_frozen`).
- **Aggregate + sorted errors**: all independent issues are collected and sorted by `(path, message)`; one `CatalogValidationError` raised (pinned by the two brief-given tests plus `test_catalog_multiple_independent_issues_all_collected`, which asserts ≥6 independent issues all present and sorted).
- **Reference resolution**: dimension/fact `.source`, measure `.fact`, metric declared `.measures`, and relationship `source`/`target` all resolve against the known-name sets built from the mapping keys; unknown references are reported independently.
- **Expression validation**: fact expressions are parsed and run through `validate_row_expression` (two-part source-field, source must exist); metric expressions through `validate_metric_expression` (one-part declared measures, actual == declared, `METRIC_FUNCTIONS` subset). Relationship/join reachability is deliberately not checked here (deferred to planner).
- **Duplicate keys**: detected by composing the YAML node tree and walking mappings (PyYAML's `safe_load` silently collapses duplicates); reported with the duplicated key's dotted path.
- **No scope widening**: only `src/selayer/expressions/__init__.py` was modified (additive exports); runtime `model.py`/`catalog.py`/`query.py`/top-level `__init__.py` are untouched.
- **`next` builtin safety**: test packages are fully-qualified (`tests.next.*`), so no top-level `next` module shadows the builtin.

No blockers found in self-review.

## Concerns

1. **`tests/__init__.py` (tests root) is outside the brief's literal `git add` list.** The brief's list is `src/selayer/_next src/selayer/expressions tests/next tests/expressions`. The existing repo has `tests/test_catalog.py`, and the brief mandates `tests/next/test_catalog.py`, so the two share a basename; pytest's default prepend import mode collides on collection (full suite failed with `ERROR tests/test_catalog.py … use a unique basename`). Resolving this without renaming the brief's file required making the test tree proper packages (`tests/`, `tests/next/`, `tests/expressions/` `__init__.py`), and `tests/__init__.py` lives at the tests root. It is included in the commit (added explicitly). The alternative (`--import-mode=importlib` in pyproject) would change global pytest behaviour, which felt heavier; the supervisor may prefer that option.
2. **`Relationship.cardinality` field name vs YAML `type`.** The Python field is `cardinality` (matching the `Cardinality` literal the brief added) and maps from the YAML `type` key. The design's "Query-plan model" later introduces `JoinStep` without a cardinality field, so this name is a forward-compatible choice; if later tasks expect `relationship.type`, the mapping is a one-line change.
3. **Version strictness.** `version != 1` treats `version: 1.0` (float) as valid because `1.0 == 1` in Python. The given tests pin `version: 2` fails and `version: 1` passes; the float edge case is negligible in practice but is not strictly "exactly int 1". Easy to tighten (`isinstance(version, int) and version == 1`) if desired.
4. **Row-function check is currently a no-op against parser output.** `ROW_FUNCTIONS` equals the parser's function allowlist, so a row expression that parses can never trip the row-function branch; it is exercised only by direct node construction in `test_validate_row_expression_rejects_function_outside_row_allowlist`. The validator is intentionally self-contained (it does not assume parser behaviour), so this is by design rather than dead code.
5. **`validate_metric_expression` on a malformed (two-part) reference** also reports the declared measure as unreferenced (e.g. `orders.total` with `declared={orders}` yields both "must be a one-part measure name" and "declared measure 'orders' is not referenced"). Both are independently true; the catalogue surfaces them sorted/de-duplicated. The unit test asserts membership rather than exact equality for that case.

## Task 2 review-fix report (current head: fd2efd8)

### Status
All review findings fixed in one pass. The staged catalog now rejects non-exact schema versions (`bool` and `float` included), validates collection/entry shapes and field types before construction, converts malformed YAML and unhashable/type-invalid values into one sorted `CatalogValidationError`, defensively copies every `SemanticLayer` collection in `__post_init__`, and exposes `Relationship.type` (with no `cardinality` alias). The package marker files are ratified and retained to prevent the test-module basename collision required by the brief.

### RED evidence

Command run after adding focused regression tests and before fixes:
`uv run pytest -q tests/next/test_catalog.py tests/expressions/test_validation.py`

Result: **failed, 11 failures** (schema `True`/`1.0` accepted, optional list sections silently emptied, wrong field types not reported, direct mappings aliased, and `Relationship.type` absent).

### GREEN evidence

- `uv run pytest -q tests/next tests/expressions` — **353 passed**
- `uv run pytest -q` — **373 passed**
- `uv run ruff check src tests` — **passed**
- `uv run ruff format --check src tests` — **21 files already formatted**
- `uv run pyright src tests` — **0 errors, 0 warnings, 0 informations**

### Changed files

- `src/selayer/_next/catalog.py` — strict exact-int version check, top-level section shape checks, string/type guards, safe malformed-YAML/type-error conversion, strict measure lists, and `Relationship.type` construction.
- `src/selayer/_next/model.py` — `Relationship.type`; defensive `dict` copies wrapped in `MappingProxyType` for every collection during `__post_init__`.
- `tests/next/test_catalog.py` — focused regression tests for all review findings, including bool/float versions, malformed YAML, unhashable values, section/field types, direct-construction immutability, and approved relationship interface.
- Existing `tests/__init__.py`, `tests/next/__init__.py`, and `tests/expressions/__init__.py` package markers are ratified and retained.

### Self-review

No blockers. Validation checks types before set membership or model construction; invalid inputs cannot produce incorrectly typed model objects. All issues remain sorted by `(path, message)` and aggregate into one domain exception. Loaded and directly constructed layers defensively isolate caller mappings.

### Concerns

None beyond the already documented staging/test-package rationale above; no staged files remain after commit.
