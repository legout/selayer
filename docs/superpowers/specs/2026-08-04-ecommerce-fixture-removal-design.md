# E-commerce fixture removal design

## Goal

Remove all checked-out e-commerce fixture data while preserving the example's ability to generate data on demand and the tests' temporary-fixture workflow.

## Scope

- Delete the 13 legacy fixtures under `data/`.
- Delete the 13 generated copies under `examples/e_commerce/data/`.
- Keep `examples/e_commerce/gen_data.py` and its default output behavior.
- Keep `examples/e_commerce/selayer1.py`, the catalog, and the e-commerce tests.
- Keep the two existing research documents; they are unrelated to fixture cleanup.
- Replace the broad `**/data/` ignore rule with explicit generated-data directories for the e-commerce and shop-floor examples.

## Behavior

A clean checkout will not contain e-commerce data. Running `gen_data.py` creates the default local dataset directory, or callers can provide `--output-dir`. Running `selayer1.py` continues to accept `--data-dir`; users must generate or provide data before executing the example. Tests continue to create deterministic fixtures under `tmp_path`.

## Verification

- Confirm neither fixture directory contains data files after cleanup.
- Run `uv run pytest tests/integration/test_ecommerce.py`.
- Run the generator into a temporary directory and confirm the runner can consume it.
- Run `git diff --check` and inspect status to ensure only intended fixture/ignore changes are included.
