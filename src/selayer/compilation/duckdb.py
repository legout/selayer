"""Compile immutable, validated query plans into parameterized DuckDB SQL.

This module deliberately accepts a :class:`~selayer.planning.QueryPlan` and no
semantic-layer or connection object.  It is therefore only a renderer: all
name resolution and grain checks have already happened in planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from selayer.expressions.ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Reference,
    UnaryOperation,
)
from selayer.expressions.ast import (
    Literal as LiteralExpression,
)
from selayer.expressions.validation import METRIC_FUNCTIONS, ROW_FUNCTIONS
from selayer.model import Aggregation
from selayer.planning.types import (
    ListFilter,
    PlannedFilter,
    QueryPlan,
    RangeFilter,
    ScalarFilter,
)


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A SQL statement and values bound to its positional placeholders."""

    sql: str
    parameters: tuple[object, ...]


def quote_identifier(identifier: str) -> str:
    """Quote one SQL identifier using DuckDB's double-quoted syntax."""

    return '"' + identifier.replace('"', '""') + '"'


def _compile_literal(literal: LiteralExpression) -> str:
    value = literal.value
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, str):
        # Expression literals are part of the validated AST, not query input.
        # Escape them as SQL string literals; filter input is always bound below.
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float)):
        return repr(value)
    raise AssertionError(
        f"validated literal has unsupported value type: {type(value)!r}"
    )


def _compile_expression(expression: Expression, mode: Literal["row", "metric"]) -> str:
    if isinstance(expression, LiteralExpression):
        return _compile_literal(expression)
    if isinstance(expression, Reference):
        if mode == "row":
            assert len(expression.parts) == 2, (
                "validated row reference is not qualified"
            )
            source, column = expression.parts
            return f"{quote_identifier(source)}.{quote_identifier(column)}"
        assert len(expression.parts) == 1, "validated metric reference is not a measure"
        return quote_identifier(expression.parts[0])
    if isinstance(expression, UnaryOperation):
        assert expression.operator in {"+", "-", "not"}, (
            "validated unary operator is not allowlisted"
        )
        operand = _compile_expression(expression.operand, mode)
        if expression.operator == "not":
            return f"(NOT {operand})"
        return f"({expression.operator}{operand})"
    if isinstance(expression, BinaryOperation):
        assert expression.operator in {
            "+",
            "-",
            "*",
            "/",
            "=",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
        }, "validated binary operator is not allowlisted"
        left = _compile_expression(expression.left, mode)
        right = _compile_expression(expression.right, mode)
        return f"({left} {expression.operator} {right})"
    if isinstance(expression, FunctionCall):
        allowed = ROW_FUNCTIONS if mode == "row" else METRIC_FUNCTIONS
        assert expression.name in allowed, "validated function is not allowlisted"
        # Names come from the AST allowlist, rather than user SQL, and are
        # rendered as function keywords. Arguments remain recursively typed.
        arguments = ", ".join(
            _compile_expression(argument, mode) for argument in expression.arguments
        )
        return f"{expression.name.upper()}({arguments})"
    raise AssertionError("validated expression has an unknown node type")


def compile_row_expression(expression: Expression) -> str:
    """Compile a validated row-level expression to DuckDB SQL."""

    return _compile_expression(expression, "row")


def compile_metric_expression(expression: Expression) -> str:
    """Compile a validated metric formula using aggregate aliases."""

    return _compile_expression(expression, "metric")


def _filter_column(item: PlannedFilter) -> str:
    dimension = item.dimension
    return f"{quote_identifier(dimension.source)}.{quote_identifier(dimension.column)}"


def _compile_filter(item: PlannedFilter, parameters: list[object]) -> str:
    column = _filter_column(item)
    value = item.value
    if isinstance(value, ScalarFilter):
        parameters.append(value.value)
        return f"{column} = ?"
    if isinstance(value, RangeFilter):
        parameters.extend((value.start, value.end))
        return f"{column} BETWEEN ? AND ?"
    if isinstance(value, ListFilter):
        if not value.values:
            return "FALSE"
        parameters.extend(value.values)
        placeholders = ", ".join("?" for _ in value.values)
        return f"{column} IN ({placeholders})"
    raise AssertionError("validated filter has an unknown value type")


def _join_sql(plan: QueryPlan) -> list[str]:
    """Render joins in planner order, orienting each join from known sources."""

    available = {plan.anchor_source}
    joins: list[str] = []
    for step in plan.joins:
        source_known = step.source in available
        target_known = step.target in available
        if source_known and not target_known:
            joins.append(
                "JOIN "
                f"{quote_identifier(step.target)} ON "
                f"{quote_identifier(step.source)}.{quote_identifier(step.source_column)} = "
                f"{quote_identifier(step.target)}.{quote_identifier(step.target_column)}"
            )
            available.add(step.target)
        elif target_known and not source_known:
            joins.append(
                "JOIN "
                f"{quote_identifier(step.source)} ON "
                f"{quote_identifier(step.source)}.{quote_identifier(step.source_column)} = "
                f"{quote_identifier(step.target)}.{quote_identifier(step.target_column)}"
            )
            available.add(step.source)
        elif source_known and target_known:
            # A planner can share a path between requirements. Such a join is
            # already present in the FROM graph and must not be repeated.
            continue
        else:
            raise AssertionError("validated join path is disconnected")
    return joins


def _aggregate_sql(aggregation: Aggregation, expression: str) -> str:
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {expression})"
    functions = {
        "sum": "SUM",
        "avg": "AVG",
        "min": "MIN",
        "max": "MAX",
        "count": "COUNT",
    }
    try:
        function = functions[aggregation]
    except KeyError as error:
        raise AssertionError("validated aggregation is not allowlisted") from error
    return f"{function}({expression})"


def compile_duckdb(plan: QueryPlan) -> CompiledQuery:
    """Compile a validated plan into a grain-safe parameterized query."""

    parameters: list[object] = []
    dimensions = [
        f"{quote_identifier(item.dimension.source)}.{quote_identifier(item.dimension.column)} "
        f"AS {quote_identifier(item.id)}"
        for item in plan.dimensions
    ]
    measures = [
        f"{_aggregate_sql(item.aggregation, compile_row_expression(item.expression))} "
        f"AS {quote_identifier(item.id)}"
        for item in plan.measures
    ]
    inner_select = ", ".join(dimensions + measures)
    from_sql = f"FROM {quote_identifier(plan.anchor_source)}"
    joins = _join_sql(plan)
    if joins:
        from_sql += " " + " ".join(joins)
    predicates = [_compile_filter(item, parameters) for item in plan.filters]
    where_sql = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    group_sql = (
        " GROUP BY " + ", ".join(str(index) for index in range(1, len(dimensions) + 1))
        if dimensions
        else ""
    )
    cte_name = quote_identifier("aggregated")
    cte = f"WITH {cte_name} AS (SELECT {inner_select} {from_sql}{where_sql}{group_sql})"

    outer_items = [quote_identifier(item.id) for item in plan.dimensions]
    outer_items.extend(
        f"{compile_metric_expression(item.expression)} AS {quote_identifier(item.id)}"
        for item in plan.metrics
    )
    outer_select = ", ".join(outer_items)
    sql = f"{cte} SELECT {outer_select} FROM {cte_name}"
    return CompiledQuery(sql=sql, parameters=tuple(parameters))


__all__ = [
    "CompiledQuery",
    "compile_duckdb",
    "compile_metric_expression",
    "compile_row_expression",
    "quote_identifier",
]
