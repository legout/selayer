from __future__ import annotations

import math
from decimal import Decimal

from .ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
)

_BINARY_PRECEDENCE = {
    "=": 1,
    "!=": 1,
    "<": 1,
    "<=": 1,
    ">": 1,
    ">=": 1,
    "+": 2,
    "-": 2,
    "*": 3,
    "/": 3,
}
_UNARY_PRECEDENCE = 4
_ATOM_PRECEDENCE = 5
_INFINITY_MAGNITUDE = "1" + "0" * 309 + ".0"


def format_expression(expression: Expression) -> str:
    return _format(expression, parent_precedence=0, right_child=False)


def _format(
    expression: Expression,
    *,
    parent_precedence: int,
    right_child: bool,
) -> str:
    if isinstance(expression, Literal):
        if expression.value is None:
            return "null"
        if expression.value is True:
            return "true"
        if expression.value is False:
            return "false"
        if isinstance(expression.value, str):
            return _format_string(expression.value)
        if isinstance(expression.value, float):
            return _format_float(expression.value)
        return str(expression.value)

    if isinstance(expression, Reference):
        return ".".join(expression.parts)

    if isinstance(expression, FunctionCall):
        arguments = ", ".join(
            _format(argument, parent_precedence=0, right_child=False)
            for argument in expression.arguments
        )
        return f"{expression.name}({arguments})"

    if isinstance(expression, UnaryOperation):
        operand = _format(
            expression.operand,
            parent_precedence=_UNARY_PRECEDENCE,
            right_child=True,
        )
        separator = " " if expression.operator == "not" else ""
        rendered = f"{expression.operator}{separator}{operand}"
        if _UNARY_PRECEDENCE < parent_precedence:
            return f"({rendered})"
        return rendered

    if isinstance(expression, BinaryOperation):
        precedence = _BINARY_PRECEDENCE[expression.operator]
        left = _format(
            expression.left,
            parent_precedence=precedence,
            right_child=False,
        )
        right = _format(
            expression.right,
            parent_precedence=precedence,
            right_child=True,
        )
        rendered = f"{left} {expression.operator} {right}"
        needs_parentheses = precedence < parent_precedence or (
            right_child and precedence == parent_precedence
        )
        if needs_parentheses:
            return f"({rendered})"
        return rendered

    raise TypeError(f"unsupported expression node: {type(expression).__name__}")


def _format_float(value: float) -> str:
    if math.isinf(value):
        return _INFINITY_MAGNITUDE if value > 0 else f"-{_INFINITY_MAGNITUDE}"
    rendered = format(Decimal.from_float(value), "f")
    return rendered if "." in rendered else f"{rendered}.0"


def _format_string(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        escaped.append(
            {
                "\\": "\\\\",
                '"': '\\"',
                "\n": "\\n",
                "\r": "\\r",
                "\t": "\\t",
            }.get(character, character)
        )
    return f'"{"".join(escaped)}"'
