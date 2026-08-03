# E-commerce Fixture Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all e-commerce fixture files while preserving on-demand generation and temporary-fixture tests, including empty-frame generator handling.

**Architecture:** Keep the e-commerce runner unchanged. Remove the committed legacy datasets and the ignored generated copy, use explicit ignore rules for generated e-commerce and shop-floor data directories, and make the generator apply declared Arrow types before enforcing non-nullable grain fields. Leave unrelated working-tree changes untouched.

**Tech Stack:** Python 3.13, uv, pytest, Git, shell fixture cleanup.

## Global Constraints

- Delete the 13 legacy fixtures under `data/`.
- Delete the 13 generated copies under `examples/e_commerce/data/`.
- Keep `examples/e_commerce/gen_data.py` and its default output behavior while fixing empty object-typed frames.
- Keep `examples/e_commerce/selayer1.py`, the catalog, and the e-commerce tests.
- Keep the two existing research documents; they are unrelated to fixture cleanup.
- Empty object-typed grain columns must be written as declared string fields before being marked non-nullable.
- Replace the broad `**/data/` ignore rule with explicit generated-data directories for the e-commerce and shop-floor examples.
- A clean checkout will not contain e-commerce data.
- Running `gen_data.py` creates the default local dataset directory, or callers can provide `--output-dir`.
- Running `selayer1.py` continues to accept `--data-dir`; users must generate or provide data before executing the example.
- Tests continue to create deterministic fixtures under `tmp_path`.

---

### Task 1: Remove fixture files and narrow ignore rules

**Files:**

- Modify: `.gitignore`
- Delete: the 13 tracked files matching `data/*.csv` and `data/*.parquet`
- Delete: the 13 generated files under `examples/e_commerce/data/`
- Do not modify: `README.md`, `ecommerce_semantic_layer.yaml`, `examples/e_commerce/selayer1.py`, research documents, or shop-floor knowledge.

**Interfaces:**

- Consumes: the existing generator default path `examples/e_commerce/data/`.
- Produces: a checkout with no e-commerce fixture files and explicit ignore rules for generated data.

- [ ] **Step 1: Confirm only the intended fixture paths are removed**

Run:

```bash
find data -maxdepth 1 -type f \( -name '*.csv' -o -name '*.parquet' \) -print | sort
find examples/e_commerce/data -maxdepth 1 -type f \( -name '*.csv' -o -name '*.parquet' \) -print | sort
```

Expected: the legacy files are absent or listed only as the paths being cleaned, and the generated directory contains only the 13 known e-commerce fixture files.

- [ ] **Step 2: Remove only the generated e-commerce files**

Run:

```bash
for name in \
  campaigns.parquet customers.csv customers.parquet inventory.parquet \
  marketing_touches.parquet order_items.csv order_items.parquet orders.csv \
  orders.parquet products.csv products.parquet support_tickets.parquet \
  website_visits.parquet; do
  rm -f "examples/e_commerce/data/$name"
done
rmdir examples/e_commerce/data 2>/dev/null || true
```

Expected: `examples/e_commerce/data/` is absent and no other directory is touched.

- [ ] **Step 3: Set explicit generated-data ignore rules**

Update the generated-data section of `.gitignore` to retain local tool-output ignores while avoiding the global `**/data/` rule:

```gitignore
# Generated e-commerce example data
examples/e_commerce/data/

# Generated shop-floor example data
examples/shopfloor/data/

# Local code graph and Graphify output
.codegraph/
graphify-out/
```

Expected: `git check-ignore examples/e_commerce/data/orders.parquet` reports the explicit e-commerce rule, while unrelated directories named `data/` are not globally ignored.

- [ ] **Step 4: Verify the cleanup diff is scoped**

Run:

```bash
git diff --check
git status --short -- data .gitignore examples/e_commerce/data
```

Expected: only the 13 tracked legacy deletions and `.gitignore` appear in this scoped view; existing unrelated modifications remain untouched.

### Task 2: Verify regeneration and temporary-fixture workflows

**Files:**

- Modify: `examples/e_commerce/gen_data.py`
- Modify: `tests/conftest.py`
- Test: `tests/integration/test_ecommerce.py`
- Run: `examples/e_commerce/gen_data.py`
- Run: `examples/e_commerce/selayer1.py`

**Interfaces:**

- Consumes: the generator's declared Arrow schemas, runner, catalog, schemas, and temporary-fixture tests.
- Produces: empty-frame-safe generator output, temporary query fixtures, and evidence that removing checked-out fixtures does not remove the supported generation or test workflows.

- [ ] **Step 1: Add and run the empty-frame regression test**

Add this test next to the generator tests in `tests/integration/test_ecommerce.py`:

```python
def test_generator_writes_empty_non_nullable_object_column(tmp_path: Path) -> None:
    from examples.e_commerce import gen_data

    output = tmp_path / "empty.parquet"
    frame = pd.DataFrame({"id": pd.Series(dtype="object")})

    gen_data._write_parquet(frame, output, non_nullable={"id"})

    schema = pq.read_schema(output)
    assert schema.field("id").type == pa.string()
    assert not schema.field("id").nullable
```

Run:

```bash
uv run pytest tests/integration/test_ecommerce.py::test_generator_writes_empty_non_nullable_object_column -q
```

Expected before the fix: FAIL with `ValueError: A null type field may not be non-nullable`.

- [ ] **Step 2: Make the generator apply declared schemas before nullability**

In `_write_parquet`, use `_SOURCE_SCHEMAS[path.name]` when available to construct the Arrow schema before calling `pa.Table.from_pandas`. Preserve the declared field types and set only names in `non_nullable` to `nullable=False`; for the fallback path, map an empty null-typed non-nullable field to `pa.string()` before casting.

Run:

```bash
uv run pytest tests/integration/test_ecommerce.py::test_generator_writes_empty_non_nullable_object_column -q
```

Expected: PASS.

- [ ] **Step 3: Run the focused integration tests**

Run:

```bash
uv run pytest tests/integration/test_ecommerce.py -q
```

Expected: all tests pass; any environment-only integration limitation must be reported rather than worked around by adding fixtures.

- [ ] **Step 4: Generate and consume datasets in one temporary-directory run**

Run:

```bash
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
uv run python examples/e_commerce/gen_data.py --output-dir "$tmpdir"
test -f "$tmpdir/orders.parquet"
test -f "$tmpdir/products.parquet"
uv run python examples/e_commerce/selayer1.py --data-dir "$tmpdir"
```

Expected: generation succeeds outside the repository; the runner loads the generated data, prints the expected query sections, and reports the intentional mixed-grain rejection.

- [ ] **Step 5: Confirm the repository remains fixture-free**

Run:

```bash
test ! -e data/campaigns.parquet
test ! -e examples/e_commerce/data
```

Expected: both checks succeed.

### Task 3: Commit only the cleanup

**Files:**

- Commit only: `.gitignore`, the 13 tracked paths under `data/`, `examples/e_commerce/gen_data.py`, `tests/conftest.py`, and the focused regression test.
- Exclude: `README.md`, catalog changes, runner changes, research, shop-floor, Graphify, `.codegraph`, and the implementation plan.

- [ ] **Step 1: Review the exact staged candidate**

Run:

```bash
git diff -- .gitignore
git diff -- data
git diff --cached --name-only
```

Expected: the candidate contains only the explicit ignore-rule update, the 13 legacy fixture deletions, the generator hardening, and its focused regression test.

- [ ] **Step 2: Stage only cleanup paths**

Run:

```bash
git add .gitignore \
  data/campaigns.parquet data/customers.csv data/customers.parquet \
  data/inventory.parquet data/marketing_touches.parquet \
  data/order_items.csv data/order_items.parquet data/orders.csv \
  data/orders.parquet data/products.csv data/products.parquet \
  data/support_tickets.parquet data/website_visits.parquet \
  examples/e_commerce/gen_data.py tests/conftest.py \
  tests/integration/test_ecommerce.py
```

Expected: no unrelated path becomes staged.

- [ ] **Step 3: Commit the cleanup**

Run:

```bash
git commit -m "chore(examples): remove checked-in ecommerce fixtures"
```

Expected: one cleanup commit containing only fixture deletions, ignore-rule changes, generator hardening, and its focused regression test.

- [ ] **Step 4: Verify the final status and commit contents**

Run:

```bash
git show --stat --summary HEAD
git status --short
```

Expected: the cleanup commit contains only the intended fixture/generator/test-fixture paths, and pre-existing unrelated changes remain visible but uncommitted.
