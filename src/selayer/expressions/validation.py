"""Symbol-environment validation for parsed expression trees.

The parser is intentionally permissive about *which* names may appear: it accepts
the union of every function the DSL knows. This module narrows that to the two
symbol environments defined by the grain-aware design:

- **Row expressions** (fact formulas) reference two-part ``source.field`` names
  whose source is known; they may use the row function allowlist.
- **Metric expressions** reference one-part declared measure names and may use
  only the narrower metric function allowlist.

Both validators are pure functions over an immutable :class:`~selayer.expressions.ast.Expression`
tree. They never touch SQL or any engine, and they defer relationship
reachability (whether a referenced source is joinable to the anchor without
expanding rows) to the planner task.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from selayer.expressions.ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Reference,
    UnaryOperation,
)

# Functions permitted inside a row-level (fact) expression.
ROW_FUNCTIONS = frozenset({"abs", "coalesce", "if", "lower", "nullif", "upper"})

# Functions permitted inside a metric (aggregate-level) expression. This is a
# strict subset of ``ROW_FUNCTIONS``: ``lower``/``upper``/``if`` are row-only.
METRIC_FUNCTIONS = frozenset({"abs", "coalesce", "nullif"})

# The DSL deliberately exposes fixed-arity scalar functions. Keeping this
# contract in semantic validation (rather than the parser) lets the parser
# remain context-neutral while both row and metric validators report ordinary
# catalog issues for malformed calls.
_FUNCTION_ARITIES = {
    "abs": 1,
    "lower": 1,
    "upper": 1,
    "coalesce": 2,
    "nullif": 2,
    "if": 3,
}


def _walk(expression: Expression) -> Iterator[Expression]:
    """Yield ``expression`` and every sub-expression, depth-first left to right."""
    yield expression
    if isinstance(expression, UnaryOperation):
        yield from _walk(expression.operand)
    elif isinstance(expression, BinaryOperation):
        yield from _walk(expression.left)
        yield from _walk(expression.right)
    elif isinstance(expression, FunctionCall):
        for argument in expression.arguments:
            yield from _walk(argument)
    # Literal and Reference carry no child expressions.


def references(expression: Expression) -> tuple[Reference, ...]:
    """Return every :class:`Reference` node, depth-first from left to right."""
    return tuple(
        cast(Reference, node)
        for node in _walk(expression)
        if isinstance(node, Reference)
    )


def _function_calls(expression: Expression) -> tuple[FunctionCall, ...]:
    return tuple(
        cast(FunctionCall, node)
        for node in _walk(expression)
        if isinstance(node, FunctionCall)
    )


def validate_row_expression(
    expression: Expression, sources: frozenset[str]
) -> tuple[str, ...]:
    """Validate a row-level (fact) expression against the row symbol environment.

    Returns sorted, de-duplicated issue messages. Row expressions permit exactly
    two-part ``source.field`` references, require the source to be known, and
    restrict functions to :data:`ROW_FUNCTIONS`. Relationship reachability from
    the anchor source is deferred to the planner.
    """
    issues: list[str] = []
    for reference in references(expression):
        if len(reference.parts) != 2:
            issues.append(
                f"reference '{'.'.join(reference.parts)}' "
                "must be a two-part source-field reference"
            )
        elif reference.parts[0] not in sources:
            issues.append(f"source '{reference.parts[0]}' is not known")
    for call in _function_calls(expression):
        if call.name not in ROW_FUNCTIONS:
            issues.append(f"function '{call.name}' is not allowed in row expressions")
        expected = _FUNCTION_ARITIES.get(call.name)
        if expected is not None and len(call.arguments) != expected:
            issues.append(
                f"function '{call.name}' expects {expected} argument(s), "
                f"got {len(call.arguments)}"
            )
    return tuple(sorted(set(issues)))


def validate_metric_expression(
    expression: Expression, declared_measures: frozenset[str]
) -> tuple[str, ...]:
    """Validate a metric expression against the metric symbol environment.

    Returns sorted, de-duplicated issue messages. Metric expressions reference
    only one-part measure names; every referenced measure must be declared, the
    set of referenced measures must equal the declared set, and only
    :data:`METRIC_FUNCTIONS` are allowed.
    """
    issues: list[str] = []
    actual_measures: set[str] = set()
    for reference in references(expression):
        if len(reference.parts) != 1:
            issues.append(
                f"reference '{'.'.join(reference.parts)}' "
                "must be a one-part measure name"
            )
            continue
        measure = reference.parts[0]
        actual_measures.add(measure)
        if measure not in declared_measures:
            issues.append(f"measure '{measure}' is not declared")
    for declared in declared_measures - actual_measures:
        issues.append(
            f"declared measure '{declared}' is not referenced in the expression"
        )
    for call in _function_calls(expression):
        if call.name not in METRIC_FUNCTIONS:
            issues.append(
                f"function '{call.name}' is not allowed in metric expressions"
            )
        expected = _FUNCTION_ARITIES.get(call.name)
        if expected is not None and len(call.arguments) != expected:
            issues.append(
                f"function '{call.name}' expects {expected} argument(s), "
                f"got {len(call.arguments)}"
            )
    return tuple(sorted(set(issues)))
