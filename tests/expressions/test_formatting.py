import math

import pytest

from selayer.expressions import Literal, format_expression, parse_expression


@pytest.mark.parametrize(
    "source",
    [
        "order_items.quantity * products.cost",
        "(revenue - cost) / revenue",
        "a + (b + c)",
        "-(a + 2)",
        'coalesce(name, "unknown")',
        "enabled = true",
        "value = null",
        "- -a",
        "+ -a",
        "(a = b) = c",
        "(a < b) != c",
    ],
)
def test_formatted_expression_round_trips(source: str) -> None:
    expression = parse_expression(source)
    assert parse_expression(format_expression(expression)) == expression


def test_formatting_is_canonical() -> None:
    assert format_expression(parse_expression("a+(b*2)")) == "a + b * 2"


def test_formatting_keeps_associative_arithmetic_minimal() -> None:
    assert format_expression(parse_expression("(a + b) + c")) == "a + b + c"


def test_parser_produced_tiny_float_round_trips_as_fixed_point() -> None:
    expression = parse_expression("0.0000000000000000000001")

    formatted = format_expression(expression)

    assert "e" not in formatted.lower()
    assert parse_expression(formatted) == expression


def test_parser_produced_whole_float_retains_float_literal() -> None:
    expression = parse_expression("100.0")

    formatted = format_expression(expression)

    assert formatted == "100.0"
    reparsed = parse_expression(formatted)
    assert reparsed == expression
    assert isinstance(reparsed, Literal)
    assert type(reparsed.value) is float


def test_parser_produced_overflow_float_round_trips_as_decimal_magnitude() -> None:
    expression = parse_expression("2" + "0" * 308 + ".0")

    assert expression == Literal(value=math.inf)
    formatted = format_expression(expression)

    assert formatted == "1" + "0" * 309 + ".0"
    assert parse_expression(formatted) == expression
