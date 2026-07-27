from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

Scalar: TypeAlias = str | int | float | bool | None  # noqa: UP040


@dataclass(frozen=True, slots=True)
class Literal:
    value: Scalar


@dataclass(frozen=True, slots=True)
class Reference:
    parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnaryOperation:
    operator: str
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryOperation:
    operator: str
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str
    arguments: tuple[Expression, ...]


Expression: TypeAlias = Literal | Reference | UnaryOperation | BinaryOperation | FunctionCall  # noqa: UP040


class ExpressionSyntaxError(ValueError):
    def __init__(self, expression: str, offset: int, message: str) -> None:
        self.expression = expression
        self.offset = offset
        self.message = message
        super().__init__(f"expression error at offset {offset}: {message}")
