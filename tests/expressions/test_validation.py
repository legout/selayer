"""Tests for row and metric expression symbol-environment validation.

These tests pin the public behaviour of ``selayer.expressions.validation``:
``references`` walks the immutable tree depth-first left-to-right, and the two
``validate_*`` functions apply the narrow symbol environments defined by the
grain-aware design (two-part source-field references for rows, one-part declared
measure references for metrics).
"""

from __future__ import annotations

import pytest

from selayer.expressions import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    parse_expression,
)
from selayer.expressions.validation import (
    METRIC_FUNCTIONS,
    ROW_FUNCTIONS,
    references,
    validate_metric_expression,
    validate_row_expression,
)

# ---------------------------------------------------------------------------
# Function allowlist constants
# ---------------------------------------------------------------------------


def test_row_functions_allowlist_matches_design() -> None:
    assert ROW_FUNCTIONS == frozenset(
        {"abs", "coalesce", "if", "lower", "nullif", "upper"}
    )


def test_metric_functions_allowlist_is_strict_subset_of_row() -> None:
    assert METRIC_FUNCTIONS == frozenset({"abs", "coalesce", "nullif"})
    assert METRIC_FUNCTIONS <= ROW_FUNCTIONS


# ---------------------------------------------------------------------------
# references(): depth-first, left-to-right walk
# ---------------------------------------------------------------------------


def test_references_of_simple_reference() -> None:
    assert references(parse_expression("orders.amount")) == (
        Reference(parts=("orders", "amount")),
    )


def test_references_walks_depth_first_left_to_right() -> None:
    expression = parse_expression("a + b.c - d")
    assert references(expression) == (
        Reference(parts=("a",)),
        Reference(parts=("b", "c")),
        Reference(parts=("d",)),
    )


def test_references_includes_function_arguments_in_order() -> None:
    expression = parse_expression("coalesce(a, b) + c")
    assert references(expression) == (
        Reference(parts=("a",)),
        Reference(parts=("b",)),
        Reference(parts=("c",)),
    )


def test_references_yields_repeated_reference_each_time() -> None:
    expression = parse_expression("a + a")
    assert references(expression) == (
        Reference(parts=("a",)),
        Reference(parts=("a",)),
    )


def test_references_of_literal_only_expression_is_empty() -> None:
    assert references(parse_expression("1 + 2")) == ()


# ---------------------------------------------------------------------------
# validate_row_expression
# ---------------------------------------------------------------------------


def test_validate_row_expression_accepts_known_two_part_references() -> None:
    expression = parse_expression("orders.amount + order_items.total")
    assert (
        validate_row_expression(expression, frozenset({"orders", "order_items"})) == ()
    )


def test_validate_row_expression_accepts_allowlisted_row_functions() -> None:
    expression = parse_expression("coalesce(orders.amount, 0) + lower(orders.label)")
    assert validate_row_expression(expression, frozenset({"orders"})) == ()


def test_validate_row_expression_rejects_one_part_reference() -> None:
    expression = parse_expression("amount + orders.total")
    issues = validate_row_expression(expression, frozenset({"orders"}))
    assert issues == ("reference 'amount' must be a two-part source-field reference",)


def test_validate_row_expression_rejects_unknown_source() -> None:
    expression = parse_expression("orders.amount + products.cost")
    issues = validate_row_expression(expression, frozenset({"orders"}))
    assert issues == ("source 'products' is not known",)


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("abs", (), 1),
        ("abs", (Reference(("orders", "value")), Reference(("orders", "value"))), 1),
        ("lower", (), 1),
        ("upper", (), 1),
        ("coalesce", (Reference(("orders", "value")),), 2),
        ("nullif", (Reference(("orders", "value")),), 2),
        ("if", (Literal(True), Reference(("orders", "value"))), 3),
    ],
)
def test_validate_row_expression_rejects_invalid_function_arity(
    name: str, arguments: tuple[Expression, ...], expected: int
) -> None:
    issues = validate_row_expression(
        FunctionCall(name, arguments), frozenset({"orders"})
    )
    assert issues == (
        f"function '{name}' expects {expected} argument(s), got {len(arguments)}",
    )


def test_validate_row_expression_rejects_function_outside_row_allowlist() -> None:
    # The parser already restricts function names, so construct a call with a
    # name outside ``ROW_FUNCTIONS`` directly to exercise the validator branch.
    expression: Expression = FunctionCall(
        name="sum", arguments=(Reference(parts=("orders", "amount")),)
    )
    issues = validate_row_expression(expression, frozenset({"orders"}))
    assert issues == ("function 'sum' is not allowed in row expressions",)


def test_validate_row_expression_returns_sorted_unique_messages() -> None:
    expression = parse_expression("rogue + products.cost + missing.x")
    issues = validate_row_expression(expression, frozenset())
    assert issues == tuple(sorted(set(issues)))
    assert len(issues) == len(set(issues))


# ---------------------------------------------------------------------------
# validate_metric_expression
# ---------------------------------------------------------------------------


def test_validate_metric_expression_accepts_declared_measures() -> None:
    expression = parse_expression(
        "(total_revenue - total_cost) / nullif(total_revenue, 0)"
    )
    assert (
        validate_metric_expression(
            expression, frozenset({"total_revenue", "total_cost"})
        )
        == ()
    )


def test_validate_metric_expression_accepts_allowlisted_metric_functions() -> None:
    expression = parse_expression("abs(total_revenue)")
    assert validate_metric_expression(expression, frozenset({"total_revenue"})) == ()


def test_validate_metric_expression_rejects_two_part_reference() -> None:
    expression = parse_expression("orders.total")
    issues = validate_metric_expression(expression, frozenset({"orders"}))
    assert "reference 'orders.total' must be a one-part measure name" in issues


def test_validate_metric_expression_rejects_undeclared_measure() -> None:
    expression = parse_expression("total_revenue + extra")
    issues = validate_metric_expression(expression, frozenset({"total_revenue"}))
    assert issues == ("measure 'extra' is not declared",)


def test_validate_metric_expression_rejects_declared_but_unreferenced() -> None:
    expression = parse_expression("total_revenue")
    issues = validate_metric_expression(
        expression, frozenset({"total_revenue", "total_cost"})
    )
    assert issues == (
        "declared measure 'total_cost' is not referenced in the expression",
    )


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("abs", (), 1),
        ("abs", (Reference(("measure",)), Reference(("measure",))), 1),
        ("coalesce", (Reference(("measure",)),), 2),
        ("nullif", (Reference(("measure",)),), 2),
    ],
)
def test_validate_metric_expression_rejects_invalid_function_arity(
    name: str, arguments: tuple[Expression, ...], expected: int
) -> None:
    issues = validate_metric_expression(
        FunctionCall(name, arguments), frozenset({"measure"})
    )
    assert (
        f"function '{name}' expects {expected} argument(s), got {len(arguments)}"
        in issues
    )


def test_validate_metric_expression_rejects_row_only_function() -> None:
    # ``lower`` parses (it is a row-context function) but is not allowed in the
    # metric environment.
    expression = parse_expression("lower(a)")
    issues = validate_metric_expression(expression, frozenset({"a"}))
    assert issues == ("function 'lower' is not allowed in metric expressions",)


def test_validate_metric_expression_returns_sorted_unique_messages() -> None:
    expression = parse_expression("a + b")
    issues = validate_metric_expression(expression, frozenset())
    assert issues == tuple(sorted(set(issues)))
    assert len(issues) == len(set(issues))


def test_validate_metric_expression_combined_mismatch_is_sorted() -> None:
    # ``extra`` is referenced but undeclared; ``declared_only`` is declared but
    # never referenced; ``a`` is referenced and declared.
    expression = parse_expression("a + extra")
    issues = validate_metric_expression(expression, frozenset({"a", "declared_only"}))
    assert issues == tuple(sorted(set(issues)))
    assert "measure 'extra' is not declared" in issues
    assert (
        "declared measure 'declared_only' is not referenced in the expression" in issues
    )


# ---------------------------------------------------------------------------
# Static typing sanity: imported nodes are usable as Expression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        BinaryOperation(
            operator="+",
            left=Reference(parts=("orders", "amount")),
            right=Literal(value=1),
        ),
        FunctionCall(name="abs", arguments=(Reference(parts=("orders", "amount")),)),
    ],
)
def test_validate_row_expression_accepts_constructed_nodes(
    expression: Expression,
) -> None:
    assert validate_row_expression(expression, frozenset({"orders"})) == ()
