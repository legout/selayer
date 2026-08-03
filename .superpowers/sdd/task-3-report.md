# Task 3 Report — Share model rules and add static invariants

Status: **complete**

## Summary

Implemented the four new declaration rules (`catalog.grain.duplicate_column`,
`catalog.grain.nullable_column`, `catalog.relationship.join_type_mismatch`,
`catalog.measure.invalid_aggregation_type`) with a single shared rule
implementation used by **both** the raw YAML loader and the typed
`collect_model_issues(layer)` / `verify_static(layer)` path. Added the
`verify(layer, check)` dispatcher with exact-type `StaticCheck` dispatch and a
`TypeError` fallback. Migrated every valid grain declaration (schemas,
gen_data, fixtures, repo data) to non-nullable grain columns so the new
nullable-grain rule does not reject previously-valid catalogs.

## Commit

```
feat(catalog): verify model invariants
```
See `git log -1` in the worktree for the exact SHA.

## Changed files

Source:
- `src/selayer/catalog.py` — shared rule helpers, typed model validators,
  `collect_model_issues`, raw-loader rule additions.
- `src/selayer/verification/static.py` — `verify_static(layer)`.
- `src/selayer/verification/__init__.py` — `verify(layer, check)` dispatcher +
  exports.

Tests:
- `tests/verification/test_static.py` — 4 new rule tests + clean-layer +
  unknown-check tests.
- `tests/test_catalog.py` — 4 YAML-variant tests asserting the same codes via
  `SemanticLayer.load`.
- `tests/conftest.py` — valid fixture grain fields → non-nullable.
- `tests/okf/test_generation.py` — updated 2 stale nullability assertions
  (`order_id` now `(required)`, a downstream effect of the grain migration).
- `tests/integration/test_ecommerce.py` — `root` fixture Arrow grain fields →
  `nullable=False` so the observed parquet satisfies the stricter declared
  schemas.

Examples / data:
- `examples/e_commerce/gen_data.py` — writes catalog parquet through an
  explicit Arrow schema (`utf8` strings + non-nullable grain columns).
- `examples/e_commerce/schemas/{orders,order_items,customers,products}.yaml`
  — grain columns → `nullable: false`.
- `data/{orders,order_items,products,customers}.parquet` — in-place
  nullability patch of grain columns to `nullable=False` (values/types
  preserved; see "Data migration" note below).

## Exact commands & output

Step 2 (red, before implementation):
```
uv run pytest tests/verification/test_static.py tests/test_catalog.py -q
# test_static.py: ImportError: cannot import name 'verify'
# test_catalog.py: 4 failed (DID NOT RAISE) for the 4 YAML variants
```

Step 6 (focused):
```
uv run pytest tests/verification/test_static.py tests/test_catalog.py \
  tests/okf/test_mlfb_scenario.py tests/integration/test_ecommerce.py -q
# 126 passed
```

Step 7 (catalog/model/expressions/planner):
```
uv run pytest tests/test_catalog.py tests/test_model.py tests/expressions \
  tests/planning -q
# 458 passed
```

Lint/format:
```
uv run ruff check <changed files>      # All checks passed!
uv run ruff format --check <changed>   # all changed files formatted
```

Full suite:
```
uv run pytest -q
# 1266 passed, 19 skipped, 2 failed, 1 error
# The 2 failures + 1 error are pre-existing, missing optional deps
# (pyiceberg, boto3/s3); confirmed identical on the stashed baseline.
```

Data-regeneration verification (temp dir, Step 5):
```
# gen_data.py now writes grain columns non-nullable with `string` types
orders / order_items / products / customers grain non-nullable + string OK
```

## Design / implementation notes

- **Shared rule cores.** The graph, fact-reachability, and metric-grain logic
  were extracted into `_safe_relationship_edges`, `_fact_reachability_issues`,
  and `_metric_grain_issues`, each consuming plain triples so both the raw
  loader (raw mappings) and the typed model validators (`SemanticLayer`) call
  one implementation. The new grain/measure/join checks live in
  `_validate_grain_columns`, `_validate_measure_aggregation`, and
  `_validate_relationship_join`, also shared by both paths. The raw loader's
  existing message text and `(path, message)` sort are preserved verbatim.
- **Typed validators.** `collect_model_issues` walks the typed layer through
  `_validate_layer_identity_model`, `_validate_named_models`, and per-object
  `_validate_*_model` helpers, reusing the existing `_logical_type_kind`,
  `_data_type_compatible`, `_schema_field`, `references`,
  `validate_metric_expression`, and `_has_safe_path` primitives — never
  re-parsing YAML.
- **Join equivalence.** No coercion: logical kinds are grouped into integer /
  float / exact-kind families via `_logical_kind_join_group`, so
  `utf8`↔`int64` mismatches while `int32`↔`int64` and `float64`↔`decimal` are
  accepted.
- **Dispatch.** `verify` uses `type(check) is StaticCheck` (exact type) and
  raises `TypeError("unsupported verification check")` otherwise; physical /
  compatibility branches are deferred to later tasks per the brief.
- **Data migration.** `gen_data.py` now produces non-nullable grain columns,
  verified in a temp dir. The committed `data/*.parquet` were patched in place
  (grain-column nullability only) rather than regenerated, because
  `products`/`customers` use `uuid4()` (non-deterministic) and the installed
  pandas writes `large_string`; in-place patching preserves all row values and
  the existing `string` (utf8) physical types, keeping the on-disk diff to
  nullability metadata only.

## Self-review

- Verified all four new codes fire from both the programmatic path
  (`verify(bad, StaticCheck())`) and the loaded path (`SemanticLayer.load`).
- Verified a clean valid layer yields a passing report with no diagnostics
  (`test_static_check_passes_clean_layer`), guarding against false positives.
- Verified `verify` raises `TypeError` for non-`StaticCheck` objects.
- Verified the raw loader's existing pinned messages (reachability, grain,
  metric-grain, ordering) are unchanged — all pre-existing catalog tests pass.
- Confirmed the 2 remaining full-suite failures + 1 error are environmental
  (missing `pyiceberg`/`boto3`) and identical on the stashed baseline.

## Concerns / residual risks

- **Scope additions vs. the brief's file list.** Two files not in the brief's
  explicit list were necessarily modified: `tests/integration/test_ecommerce.py`
  (its `root` fixture writes parquet that must satisfy the now-stricter
  schemas) and `tests/okf/test_generation.py` (stale nullability assertions
  for the migrated `order_id` grain column). Both are direct, unavoidable
  consequences of the required Step 5 grain migration.
- **Committed binary data.** `data/*.parquet` grain columns were patched to
  non-nullable. The diff is nullability metadata only (values and utf8 types
  preserved); reviewers may prefer regenerating via the updated `gen_data.py`
  instead, at the cost of re-rolling the non-deterministic uuid-based ids.
- `model.py` (Task 1) has a pre-existing `ruff format` deviation; left
  untouched to preserve Task 1's API as instructed.
