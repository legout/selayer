import pytest

from selayer.expressions import format_expression, parse_expression


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
    ],
)
def test_formatted_expression_round_trips(source: str) -> None:
    expression = parse_expression(source)
    assert parse_expression(format_expression(expression)) == expression


def test_formatting_is_canonical() -> None:
    assert format_expression(parse_expression("a+(b*2)")) == "a + b * 2"
