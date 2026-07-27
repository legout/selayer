"""Tests for the restricted semantic expression parser.

These tests pin the public behaviour of ``selayer.expressions``: the immutable
node tree shape, operator precedence, and the syntactic rejections that keep the
DSL restricted (no raw SQL, comments, semicolons, three-part references, or
unknown function names).
"""

from __future__ import annotations

import duckdb
import pytest

from selayer.expressions import (
    BinaryOperation,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    Reference,
    Scalar,
    UnaryOperation,
    parse_expression,
)

# ---------------------------------------------------------------------------
# Precedence and parentheses (brief Step 1)
# ---------------------------------------------------------------------------


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


def test_subtraction_is_left_associative() -> None:
    assert parse_expression("a - b - c") == BinaryOperation(
        operator="-",
        left=BinaryOperation(
            operator="-",
            left=Reference(parts=("a",)),
            right=Reference(parts=("b",)),
        ),
        right=Reference(parts=("c",)),
    )


def test_division_is_left_associative() -> None:
    assert parse_expression("a / b / c") == BinaryOperation(
        operator="/",
        left=BinaryOperation(
            operator="/",
            left=Reference(parts=("a",)),
            right=Reference(parts=("b",)),
        ),
        right=Reference(parts=("c",)),
    )


def test_parentheses_override_precedence() -> None:
    expression = parse_expression("(a + b) * 2")
    assert isinstance(expression, BinaryOperation)
    assert expression.operator == "*"
    assert isinstance(expression.left, BinaryOperation)


def test_nested_parentheses() -> None:
    expression = parse_expression("((a))")
    assert expression == Reference(parts=("a",))


def test_comparison_is_not_chainable() -> None:
    # comparison := additive (comparison_op additive)?  -- a single optional op.
    expression = parse_expression("a = b")
    assert expression == BinaryOperation(
        operator="=",
        left=Reference(parts=("a",)),
        right=Reference(parts=("b",)),
    )


# ---------------------------------------------------------------------------
# Unary operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("+a", UnaryOperation(operator="+", operand=Reference(parts=("a",)))),
        ("-a", UnaryOperation(operator="-", operand=Reference(parts=("a",)))),
        ("not a", UnaryOperation(operator="not", operand=Reference(parts=("a",)))),
    ],
)
def test_unary_operators(source: str, expected: object) -> None:
    assert parse_expression(source) == expected


def test_unary_operators_nest_and_bind_tighter_than_binary() -> None:
    assert parse_expression("not -a") == UnaryOperation(
        operator="not",
        operand=UnaryOperation(operator="-", operand=Reference(parts=("a",))),
    )
    assert parse_expression("-a * b") == BinaryOperation(
        operator="*",
        left=UnaryOperation(operator="-", operand=Reference(parts=("a",))),
        right=Reference(parts=("b",)),
    )


# ---------------------------------------------------------------------------
# Comparison operators (every operator)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operator", ["=", "!=", "<", "<=", ">", ">="])
def test_every_comparison_operator(operator: str) -> None:
    expression = parse_expression(f"a {operator} b")
    assert expression == BinaryOperation(
        operator=operator,
        left=Reference(parts=("a",)),
        right=Reference(parts=("b",)),
    )


# ---------------------------------------------------------------------------
# Literals: strings, booleans, null, numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("0", 0),
        ("42", 42),
        ("3.14", 3.14),
        ("0.5", 0.5),
        ("100.0", 100.0),
    ],
)
def test_number_literals(source: str, value: Scalar) -> None:
    assert parse_expression(source) == Literal(value=value)


def test_integer_literal_is_int_not_float() -> None:
    expression = parse_expression("7")
    assert isinstance(expression, Literal)
    assert expression.value == 7
    assert type(expression.value) is int


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("'hello'", "hello"),
        ("''", ""),
        ("'O\\\\Reilly'", "O\\Reilly"),
        ("'it\\'s'", "it's"),
        ("'line\\nbreak'", "line\nbreak"),
        ("'tab\\there'", "tab\there"),
    ],
)
def test_string_literals(source: str, value: Scalar) -> None:
    assert parse_expression(source) == Literal(value=value)


@pytest.mark.parametrize(
    ("source", "value"),
    [("true", True), ("false", False)],
)
def test_boolean_literals(source: str, value: Scalar) -> None:
    expression = parse_expression(source)
    assert expression == Literal(value=value)
    assert isinstance(expression, Literal)
    assert expression.value is value


def test_null_literal() -> None:
    expression = parse_expression("null")
    assert expression == Literal(value=None)
    assert isinstance(expression, Literal)
    assert expression.value is None


# ---------------------------------------------------------------------------
# References: simple, qualified, repeated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "parts"),
    [
        ("total_item_revenue", ("total_item_revenue",)),
        ("order_items.quantity", ("order_items", "quantity")),
        ("products.cost", ("products", "cost")),
        ("a1", ("a1",)),
        ("snake_case_name", ("snake_case_name",)),
    ],
)
def test_references(source: str, parts: tuple[str, ...]) -> None:
    assert parse_expression(source) == Reference(parts=parts)


def test_repeated_references_in_one_expression() -> None:
    assert parse_expression("a + a") == BinaryOperation(
        operator="+",
        left=Reference(parts=("a",)),
        right=Reference(parts=("a",)),
    )


# ---------------------------------------------------------------------------
# Function calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "name", "argument_count"),
    [
        ("coalesce(a, b)", "coalesce", 2),
        ("nullif(a, b)", "nullif", 2),
        ("abs(a)", "abs", 1),
        ("lower(a)", "lower", 1),
        ("upper(a)", "upper", 1),
        ("if(a, b, c)", "if", 3),
    ],
)
def test_allowlisted_function_calls(
    source: str, name: str, argument_count: int
) -> None:
    expression = parse_expression(source)
    assert isinstance(expression, FunctionCall)
    assert expression.name == name
    assert len(expression.arguments) == argument_count


def test_function_call_arguments_are_expressions() -> None:
    expression = parse_expression("coalesce(a + b, 0)")
    assert expression == FunctionCall(
        name="coalesce",
        arguments=(
            BinaryOperation(
                operator="+",
                left=Reference(parts=("a",)),
                right=Reference(parts=("b",)),
            ),
            Literal(value=0),
        ),
    )


def test_nested_function_calls() -> None:
    expression = parse_expression("abs(nullif(a, 0))")
    assert expression == FunctionCall(
        name="abs",
        arguments=(
            FunctionCall(
                name="nullif",
                arguments=(Reference(parts=("a",)), Literal(value=0)),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Combined real-world expressions
# ---------------------------------------------------------------------------


def test_metric_formula_parses_to_aggregate_tree() -> None:
    expression = parse_expression(
        "(total_item_revenue - total_item_cost) / nullif(total_item_revenue, 0)"
    )
    assert expression == BinaryOperation(
        operator="/",
        left=BinaryOperation(
            operator="-",
            left=Reference(parts=("total_item_revenue",)),
            right=Reference(parts=("total_item_cost",)),
        ),
        right=FunctionCall(
            name="nullif",
            arguments=(Reference(parts=("total_item_revenue",)), Literal(value=0)),
        ),
    )


def test_fact_formula_with_qualified_fields() -> None:
    expression = parse_expression("order_items.quantity * products.cost")
    assert expression == BinaryOperation(
        operator="*",
        left=Reference(parts=("order_items", "quantity")),
        right=Reference(parts=("products", "cost")),
    )


# ---------------------------------------------------------------------------
# Rejections (brief Step 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "a; b",
        "SELECT a",
        "a -- comment",
        "a.b.c",
        "FROM a",
        "a /* block */ b",
        "select a",
        "where a = b",
        "a and b",
        "a or b",
        "case when a then b end",
    ],
)
def test_rejects_non_dsl_syntax(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "sum(a)",
        "avg(a)",
        "count(a)",
        "unknown_function(a)",
        "SUM(a)",
    ],
)
def test_rejects_function_names_outside_allowlist(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "a % b",
        "a & b",
        "a | b",
        "a ^ b",
        "a ~ b",
        "a @ b",
        "a ` b",
        "a # b",
        "a $ b",
        "a ! b",
    ],
)
def test_rejects_unknown_characters(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "a +",
        "(a",
        "(a + b",
        ")",
        "coalesce(a",
        "* a",
        ",",
        "",
    ],
)
def test_rejects_missing_or_unexpected_tokens(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "a b",
        "1 2",
        "a + b c",
        "(a) (b)",
        "a ()",
        "1 + 2 3",
    ],
)
def test_rejects_trailing_tokens(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


def test_rejects_qualified_function_name() -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression("a.b(c)")


def test_rejects_four_part_reference() -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression("a.b.c.d")


def test_rejects_uppercase_identifier() -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression("CamelCase")


def test_syntax_error_records_offset_and_source() -> None:
    with pytest.raises(ExpressionSyntaxError) as info:
        parse_expression("a + @")
    assert info.value.expression == "a + @"
    assert info.value.offset == 4
    assert info.value.message


# ---------------------------------------------------------------------------
# Review fixes: ASCII-only numbers, keyword completeness, per-segment keywords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["²", "³", "² + 1", "٢", "٣", "¹²"])
def test_non_ascii_digits_are_rejected_as_unknown_characters(source: str) -> None:
    # ``str.isdigit`` is Unicode-aware; only ASCII digits may start a number so
    # unknown numeric syntax raises ExpressionSyntaxError instead of leaking a
    # ValueError out of int()/float().
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


def test_non_ascii_digit_error_reports_original_offset() -> None:
    with pytest.raises(ExpressionSyntaxError) as info:
        parse_expression("a + ²")
    assert info.value.expression == "a + ²"
    assert info.value.offset == 4
    assert info.value.message


@pytest.mark.parametrize(
    "source",
    [
        "window",
        "over",
        "partition",
        "qualify",
        "ilike",
        "glob",
        "begin",
        "commit",
        "rollback",
        "primary",
        "foreign",
        "references",
        "constraint",
        "unique",
        "default",
        "function",
        "procedure",
        "return",
        "grant",
        "revoke",
        "right",
        "left",
        "union",
        "between",
    ],
)
def test_rejects_duckdb_sql_keywords(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "a.select",
        "a.window",
        "a.from",
        "a.order",
        "a.group",
        "a.partition",
        "a.over",
        "a.distinct",
    ],
)
def test_rejects_sql_keyword_in_any_reference_segment(source: str) -> None:
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(source)


# ---------------------------------------------------------------------------
# Review fix (round 2): the reserved-keyword set must be a superset of the
# DuckDB reserved vocabulary reported by the installed engine.
# ---------------------------------------------------------------------------

# DuckDB publishes its reserved words through the ``duckdb_keywords()`` table.
# Every reserved word must be rejected as a reference, both as a bare one-part
# name and as the second segment of a qualified ``source.<keyword>`` reference,
# so engine-reserved vocabulary can never leak through as an identifier.
#
# ``true``, ``false``, ``null``, and ``not`` are also reported as reserved, but
# the tokenizer recognizes them as DSL keywords (boolean/null literals and the
# unary ``not`` operator) *before* the reserved-word check runs, so they are
# excluded here: they cannot be rejected as one-part references by design, and
# the representative tests above pin them as literals/operators.
_RESERVED_DSL_KEYWORDS = frozenset({"true", "false", "null", "not"})


def _duckdb_reserved_references() -> list[str]:
    connection = duckdb.connect()
    rows = connection.execute(
        "select keyword_name from duckdb_keywords() "
        "where keyword_category = 'reserved' order by keyword_name"
    ).fetchall()
    keywords: list[str] = []
    for row in rows:
        word = row[0]
        if isinstance(word, str) and word not in _RESERVED_DSL_KEYWORDS:
            keywords.append(word)
    return keywords


_DUCKDB_RESERVED_REFERENCES = _duckdb_reserved_references()


@pytest.mark.parametrize("keyword", _DUCKDB_RESERVED_REFERENCES)
def test_rejects_every_duckdb_reserved_word_as_reference(keyword: str) -> None:
    # A reserved word must not parse as a bare one-part reference.
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(keyword)


@pytest.mark.parametrize("keyword", _DUCKDB_RESERVED_REFERENCES)
def test_rejects_every_duckdb_reserved_word_in_qualified_segment(
    keyword: str,
) -> None:
    # A reserved word must not parse as the second segment of ``source.<kw>``.
    with pytest.raises(ExpressionSyntaxError):
        parse_expression(f"source.{keyword}")
