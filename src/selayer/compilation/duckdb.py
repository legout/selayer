"""Compile resolved query plans into parameterized DuckDB SQL."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from selayer.expressions import (
    METRIC_FUNCTIONS,
    ROW_FUNCTIONS,
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
)
from selayer.planning import (
    ListFilter,
    QueryPlan,
    RangeFilter,
    ScalarFilter,
)

_BINARY_OPERATORS: Final = frozenset(
    {"+", "-", "*", "/", "=", "!=", "<", "<=", ">", ">="}
)
_UNARY_OPERATORS: Final = frozenset({"+", "-", "not"})
_AGGREGATIONS: Final = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
}


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """DuckDB SQL and its positional bound parameters."""

    sql: str
    parameters: tuple[object, ...]


def quote_identifier(identifier: str) -> str:
    """Return one safely quoted DuckDB identifier."""
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _compile_literal(expression: Literal) -> str:
    value = expression.value
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, int):
        return format(value, "d")
    if isinstance(value, float) and math.isfinite(value):
        return format(value)
    raise AssertionError("invalid validated literal")


def compile_row_expression(expression: Expression) -> str:
    """Compile an expression resolved in the row-level symbol environment."""
    if isinstance(expression, Literal):
        return _compile_literal(expression)
    if isinstance(expression, Reference):
        if len(expression.parts) != 2:
            raise AssertionError("invalid validated row reference")
        return ".".join(quote_identifier(part) for part in expression.parts)
    if isinstance(expression, UnaryOperation):
        if expression.operator not in _UNARY_OPERATORS:
            raise AssertionError("invalid validated unary operator")
        operator = "NOT " if expression.operator == "not" else expression.operator
        return f"({operator}{compile_row_expression(expression.operand)})"
    if isinstance(expression, BinaryOperation):
        if expression.operator not in _BINARY_OPERATORS:
            raise AssertionError("invalid validated binary operator")
        return (
            f"({compile_row_expression(expression.left)} {expression.operator} "
            f"{compile_row_expression(expression.right)})"
        )
    if isinstance(expression, FunctionCall):
        if expression.name not in ROW_FUNCTIONS:
            raise AssertionError("invalid validated row function")
        arguments = ", ".join(
            compile_row_expression(argument) for argument in expression.arguments
        )
        return f"{expression.name.upper()}({arguments})"
    raise AssertionError("unsupported row expression node")


def compile_metric_expression(
    expression: Expression,
    measure_aliases: Mapping[str, str] | None = None,
) -> str:
    """Compile an expression resolved in the aggregate-level symbol environment."""
    if isinstance(expression, Literal):
        return _compile_literal(expression)
    if isinstance(expression, Reference):
        if len(expression.parts) != 1:
            raise AssertionError("invalid validated metric reference")
        identifier = expression.parts[0]
        if measure_aliases is not None:
            try:
                identifier = measure_aliases[identifier]
            except KeyError:
                raise AssertionError("unknown validated metric reference") from None
        return quote_identifier(identifier)
    if isinstance(expression, UnaryOperation):
        if expression.operator not in _UNARY_OPERATORS:
            raise AssertionError("invalid validated unary operator")
        operator = "NOT " if expression.operator == "not" else expression.operator
        return (
            f"({operator}"
            f"{compile_metric_expression(expression.operand, measure_aliases)})"
        )
    if isinstance(expression, BinaryOperation):
        if expression.operator not in _BINARY_OPERATORS:
            raise AssertionError("invalid validated binary operator")
        return (
            f"({compile_metric_expression(expression.left, measure_aliases)} "
            f"{expression.operator} "
            f"{compile_metric_expression(expression.right, measure_aliases)})"
        )
    if isinstance(expression, FunctionCall):
        if expression.name not in METRIC_FUNCTIONS:
            raise AssertionError("invalid validated metric function")
        arguments = ", ".join(
            compile_metric_expression(argument, measure_aliases)
            for argument in expression.arguments
        )
        return f"{expression.name.upper()}({arguments})"
    raise AssertionError("unsupported metric expression node")


def _compile_aggregation(aggregation: str, expression: Expression) -> str:
    compiled_expression = compile_row_expression(expression)
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {compiled_expression})"
    function = _AGGREGATIONS.get(aggregation)
    if function is None:
        raise AssertionError("invalid validated aggregation")
    return f"{function}({compiled_expression})"


def _compile_joins(plan: QueryPlan) -> str:
    available_sources = {plan.anchor_source}
    clauses: list[str] = []
    for join in plan.joins:
        source_available = join.source in available_sources
        target_available = join.target in available_sources
        if source_available == target_available:
            raise AssertionError("invalid resolved join order")
        joined_source = join.target if source_available else join.source
        clauses.append(
            f" JOIN {quote_identifier(joined_source)} ON "
            f"{quote_identifier(join.source)}.{quote_identifier(join.source_column)} = "
            f"{quote_identifier(join.target)}.{quote_identifier(join.target_column)}"
        )
        available_sources.add(joined_source)
    return "".join(clauses)


def _compile_filters(plan: QueryPlan) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for planned_filter in plan.filters:
        dimension = planned_filter.dimension
        column = (
            f"{quote_identifier(dimension.source)}.{quote_identifier(dimension.column)}"
        )
        value = planned_filter.value
        if isinstance(value, ScalarFilter):
            clauses.append(f"{column} = ?")
            parameters.append(value.value)
        elif isinstance(value, RangeFilter):
            clauses.append(f"{column} BETWEEN ? AND ?")
            parameters.extend((value.lower, value.upper))
        elif isinstance(value, ListFilter):
            if value.values:
                placeholders = ", ".join("?" for _ in value.values)
                clauses.append(f"{column} IN ({placeholders})")
                parameters.extend(value.values)
            else:
                clauses.append("FALSE")
        else:
            raise TypeError("invalid validated filter")
    if not clauses:
        return "", tuple(parameters)
    return " WHERE " + " AND ".join(clauses), tuple(parameters)


def compile_duckdb(plan: QueryPlan) -> CompiledQuery:
    """Compile a fully resolved plan without consulting its source catalog."""
    dimension_aliases = {
        item.id: f"__selayer_dimension_{item.id}" for item in plan.dimensions
    }
    measure_aliases = {
        item.id: f"__selayer_measure_{item.id}" for item in plan.measures
    }
    projections = [
        f"{quote_identifier(item.source)}.{quote_identifier(item.column)} "
        f"AS {quote_identifier(dimension_aliases[item.id])}"
        for item in plan.dimensions
    ]
    projections.extend(
        f"{_compile_aggregation(measure.aggregation, measure.expression)} "
        f"AS {quote_identifier(measure_aliases[measure.id])}"
        for measure in plan.measures
    )

    filters, parameters = _compile_filters(plan)
    grouping = (
        " GROUP BY "
        + ", ".join(str(index) for index in range(1, len(plan.dimensions) + 1))
        if plan.dimensions
        else ""
    )
    outer = [
        f"{quote_identifier(dimension_aliases[item.id])} AS {quote_identifier(item.id)}"
        for item in plan.dimensions
    ]
    outer.extend(
        f"{compile_metric_expression(item.expression, measure_aliases)} "
        f"AS {quote_identifier(item.id)}"
        for item in plan.metrics
    )
    sql = (
        "WITH aggregated AS (SELECT "
        + ", ".join(projections)
        + f" FROM {quote_identifier(plan.anchor_source)}"
        + _compile_joins(plan)
        + filters
        + grouping
        + ") SELECT "
        + ", ".join(outer)
        + " FROM aggregated"
    )
    return CompiledQuery(sql=sql, parameters=parameters)


__all__ = [
    "CompiledQuery",
    "compile_duckdb",
    "compile_metric_expression",
    "compile_row_expression",
    "quote_identifier",
]
