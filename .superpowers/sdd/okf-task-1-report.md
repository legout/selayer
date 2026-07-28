# OKF Task 1: Canonical expression formatting

## RED

Added `tests/expressions/test_formatting.py` with parser/formatter round-trip cases and a canonical-spacing assertion. The required RED command:

```text
uv run pytest -q tests/expressions/test_formatting.py
```

failed during collection because `format_expression` was not yet exported from `selayer.expressions` (`ImportError`).

The brief's equality examples used `==`, but the active approved parser supports `=`. The tests use `enabled = true` and `value = null` so they exercise the real parser contract without widening scope.

## GREEN

Added `src/selayer/expressions/formatting.py` with a complete formatter for literals, references, function calls, unary operations, and binary operations. Formatting preserves precedence and right-side associativity with minimal parentheses, emits parser-compatible string escapes, and exports `format_expression` from `selayer.expressions`.

Focused formatter tests pass: **8 passed**.

## Checks

- `uv run pytest -q tests/expressions/test_formatting.py` — **8 passed**
- `uv run pytest -q tests/expressions/test_parser.py tests/expressions/test_formatting.py` — **79 passed**
- `uv run pytest -q` — **251 passed**
- `uv run ruff check src/selayer/expressions tests/expressions` — **passed**
- `uv run ruff format --check src/selayer/expressions tests/expressions` — **passed**
- `uv run pyright src/selayer/expressions tests/expressions` — **0 errors, 0 warnings, 0 informations**

## Changed files

- `src/selayer/expressions/formatting.py`
- `src/selayer/expressions/__init__.py`
- `tests/expressions/test_formatting.py`
- `.superpowers/sdd/okf-task-1-report.md`

## Residual risks

None identified within the active parser grammar. The equality spelling correction is documented above.
