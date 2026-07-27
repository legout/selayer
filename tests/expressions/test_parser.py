import pytest

from selayer.expressions import (
    BinaryOperation,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
    parse_expression,
)


def test_multiplication_binds_tighter_than_addition() -> None:
    assert parse_expression("a + b * 2") == BinaryOperation(
        operator="+",
        left=Reference(parts=("a",)),
        right=BinaryOperation(
            operator="*",
            left=Reference(parts=("b",)),
            right=Literal(value=2),
        ),
    )


def test_parentheses_override_precedence() -> None:
    expression = parse_expression("(a + b) * 2")
    assert isinstance(expression, BinaryOperation)
    assert expression.operator == "*"
    assert isinstance(expression.left, BinaryOperation)


def test_expression_nodes_are_immutable() -> None:
    expression = parse_expression("a + 1")
    with pytest.raises((AttributeError, TypeError)):
        expression.operator = "-"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("-a", UnaryOperation(operator="-", operand=Reference(parts=("a",)))),
        ("+1", UnaryOperation(operator="+", operand=Literal(value=1))),
        ("not a", UnaryOperation("not", Reference(("a",)))),
        ("a = b", BinaryOperation("=", Reference(("a",)), Reference(("b",)))),
        ("a != b", BinaryOperation("!=", Reference(("a",)), Reference(("b",)))),
        ("a < b", BinaryOperation("<", Reference(("a",)), Reference(("b",)))),
        ("a <= b", BinaryOperation("<=", Reference(("a",)), Reference(("b",)))),
        ("a > b", BinaryOperation(">", Reference(("a",)), Reference(("b",)))),
        ("a >= b", BinaryOperation(">=", Reference(("a",)), Reference(("b",)))),
        ("'hello\\'s'", Literal(value="hello's")),
        ('"hello\\nworld"', Literal(value="hello\nworld")),
        ("true", Literal(value=True)),
        ("false", Literal(value=False)),
        ("null", Literal(value=None)),
        (
            "coalesce(a, 0)",
            FunctionCall(name="coalesce", arguments=(Reference(("a",)), Literal(0))),
        ),
        (
            "if(a > 1, a, 0)",
            FunctionCall(
                name="if",
                arguments=(
                    BinaryOperation(">", Reference(("a",)), Literal(1)),
                    Reference(("a",)),
                    Literal(0),
                ),
            ),
        ),
        ("orders.total", Reference(parts=("orders", "total"))),
        ("a + a", BinaryOperation("+", Reference(("a",)), Reference(("a",)))),
    ],
)
def test_accepts_supported_syntax(source: str, expected: object) -> None:
    assert parse_expression(source) == expected


@pytest.mark.parametrize("source", ["a; b", "SELECT a", "a -- comment", "a.b.c"])
def test_rejects_non_dsl_syntax(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source", ["a @ b", "a +", "(a + b", "a b", "fn(a,)", "fn(", "unknown(a)"]
)
def test_rejects_malformed_syntax(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError) as error:
        parse_expression(source)
    assert error.value.expression == source
    assert 0 <= error.value.offset <= len(source)


def test_syntax_error_reports_first_invalid_offset() -> None:
    with pytest.raises(ExpressionSyntaxError) as error:
        parse_expression("a + @")
    assert error.value.offset == 4


def test_syntax_error_prefers_earlier_parser_error() -> None:
    with pytest.raises(ExpressionSyntaxError, match="trailing tokens") as error:
        parse_expression("a b @")
    assert error.value.offset == 2


def test_unterminated_string_reports_its_start_offset() -> None:
    with pytest.raises(ExpressionSyntaxError, match="unterminated string") as error:
        parse_expression("a + 'unterminated")
    assert error.value.offset == 4


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "COALESCE(a, 0)",
            FunctionCall("coalesce", (Reference(("a",)), Literal(0))),
        ),
        (
            "NuLlIf(a, 0)",
            FunctionCall("nullif", (Reference(("a",)), Literal(0))),
        ),
    ],
)
def test_allowlisted_function_calls_are_case_insensitive(
    source: str, expected: FunctionCall
) -> None:
    assert parse_expression(source) == expected


@pytest.mark.parametrize(
    "source",
    ["a AND b", "a OR b", "a LIKE b", "a IN b", "FROM", "WITH", "VALUES", "a.b.c"],
)
def test_rejects_unsupported_keywords_and_reference_depth(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "INTO",
        "SET",
        "RETURNING",
        "EXISTS",
        "BETWEEN",
        # Representative DDL and DML keywords.
        "DATABASE",
        "SCHEMA",
        "VIEW",
        "MERGE",
        "CALL",
        "COPY",
        # Representative join keywords.
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "NATURAL",
        "USING",
        # Representative window keywords.
        "WINDOW",
        "OVER",
        "PARTITION",
        "ROWS",
        "RANGE",
        "PRECEDING",
        "FOLLOWING",
    ],
)
def test_rejects_sql_keywords_as_references(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError, match="SQL keyword is not allowed"):
        parse_expression(source)
