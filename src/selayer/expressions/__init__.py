"""Public expression types and parse entrypoint for the restricted DSL.

Later tasks consume these immutable nodes from the catalog model, validator,
planner, and compiler. Callers parse formulas with :func:`parse_expression`
rather than constructing nodes directly. Row and metric symbol environments are
applied by :mod:`selayer.expressions.validation`.
"""

from selayer.expressions.ast import (
    BinaryOperation,
    Expression,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    Reference,
    Scalar,
    UnaryOperation,
)
from selayer.expressions.formatting import format_expression
from selayer.expressions.parser import parse_expression
from selayer.expressions.validation import (
    METRIC_FUNCTIONS,
    ROW_FUNCTIONS,
    references,
    validate_metric_expression,
    validate_row_expression,
)

__all__ = [
    "METRIC_FUNCTIONS",
    "ROW_FUNCTIONS",
    "BinaryOperation",
    "Expression",
    "ExpressionSyntaxError",
    "FunctionCall",
    "Literal",
    "Reference",
    "Scalar",
    "UnaryOperation",
    "format_expression",
    "parse_expression",
    "references",
    "validate_metric_expression",
    "validate_row_expression",
]
