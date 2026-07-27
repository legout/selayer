"""Immutable, engine-neutral expression nodes.

The expression tree is the single representation of every calculated fact and
metric formula in selayer. Nodes carry no SQL text and no engine-specific
information; later tasks validate and compile them.
"""

from __future__ import annotations

from dataclasses import dataclass

type Scalar = str | int | float | bool | None


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


type Expression = Literal | Reference | UnaryOperation | BinaryOperation | FunctionCall


class ExpressionSyntaxError(ValueError):
    """Raised when source text is not valid restricted-DSL syntax.

    Carries the original expression, the byte offset of the first invalid token,
    and a concise explanation so callers can report a precise location.
    """

    def __init__(self, expression: str, offset: int, message: str) -> None:
        self.expression = expression
        self.offset = offset
        self.message = message
        super().__init__(f"expression error at offset {offset}: {message}")
