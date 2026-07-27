"""Tokenizer and recursive-descent parser for the restricted expression DSL.

The grammar implemented here is::

    expression     := comparison
    comparison     := additive (comparison_op additive)?
    additive       := multiplicative (("+" | "-") multiplicative)*
    multiplicative := unary (("*" | "/") unary)*
    unary          := ("+" | "-" | "not") unary | primary
    primary        := identifier | number | string | boolean | null
                     | function_call | "(" expression ")"
    function_call  := identifier "(" arguments? ")"
    arguments      := expression ("," expression)*
    comparison_op  := "=" | "!=" | "<" | "<=" | ">" | ">="

The parser is intentionally restrictive: it rejects comments, semicolons, SQL
keywords, subqueries, attribute chains longer than two segments, function names
outside the allowlist, unknown characters, and any trailing input. It never
emits SQL and carries no engine knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

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

_COMPARISON_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
_ADDITIVE_OPS = frozenset({"+", "-"})
_MULTIPLICATIVE_OPS = frozenset({"*", "/"})
_UNARY_OPS = frozenset({"+", "-", "not"})

# Function names accepted by the parser. The narrower row/metric environments
# (which function each context may use) are enforced by the validation module in
# a later task; the parser only keeps raw SQL aggregates and arbitrary names out.
_FUNCTION_NAMES = frozenset({"abs", "coalesce", "nullif", "lower", "upper", "if"})

# Reserved SQL/DuckDB keywords. The set is a superset of DuckDB's reserved
# vocabulary, snapshotted at design time from ``duckdb_keywords()`` where
# ``category = 'reserved'``; DuckDB itself is NOT imported here so the parser
# stays engine-neutral. Every reference segment (first or qualified) is checked
# against this set and rejected. ``true``, ``false``, ``null``, and ``not`` are
# members too, but the tokenizer recognizes them as DSL keywords (boolean/null
# literals and the unary ``not``) *before* this check, so they still parse as
# literals/operators when used standalone; their presence here additionally
# rejects them as non-first reference segments (e.g. ``source.null``). SQL type
# names (``int``, ``date``, ``timestamp`` ...) are deliberately not reserved so
# they remain usable as column references.
_SQL_KEYWORDS = frozenset(
    {
        "all",
        "alter",
        "analyse",
        "analyze",
        "and",
        "anti",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "attach",
        "begin",
        "between",
        "both",
        "by",
        "cascade",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "commit",
        "constraint",
        "copy",
        "create",
        "cross",
        "database",
        "default",
        "deferrable",
        "delete",
        "desc",
        "describe",
        "detach",
        "distinct",
        "do",
        "drop",
        "else",
        "end",
        "escape",
        "except",
        "exclude",
        "exists",
        "explain",
        "export",
        "false",
        "fetch",
        "following",
        "for",
        "foreign",
        "from",
        "full",
        "function",
        "glob",
        "grant",
        "group",
        "groups",
        "having",
        "ilike",
        "import",
        "in",
        "index",
        "initially",
        "inner",
        "insert",
        "intersect",
        "into",
        "is",
        "join",
        "lambda",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "load",
        "macro",
        "materialized",
        "minus",
        "natural",
        "not",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "over",
        "partition",
        "pivot",
        "pivot_longer",
        "pivot_wider",
        "placing",
        "pragma",
        "preceding",
        "primary",
        "procedure",
        "prune",
        "qualify",
        "range",
        "recursive",
        "references",
        "restrict",
        "return",
        "returning",
        "returns",
        "revoke",
        "right",
        "rollback",
        "rows",
        "sample",
        "savepoint",
        "schema",
        "select",
        "semi",
        "sequence",
        "set",
        "show",
        "similar",
        "some",
        "summarize",
        "symmetric",
        "table",
        "tablesample",
        "temporary",
        "then",
        "to",
        "trailing",
        "transaction",
        "trigger",
        "true",
        "unbounded",
        "union",
        "unique",
        "unpivot",
        "update",
        "using",
        "vacuum",
        "values",
        "variadic",
        "view",
        "when",
        "where",
        "window",
        "with",
    }
)

_STRING_ESCAPES = {
    "'": "'",
    "\\": "\\",
    "n": "\n",
    "t": "\t",
    "r": "\r",
}
_IDENTIFIER_START = "abcdefghijklmnopqrstuvwxyz"
_IDENTIFIER_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
# ASCII digits only: ``str.isdigit`` is Unicode-aware and would accept
# superscripts/Arabic-Indic digits that ``int``/``float`` cannot consume.
_DIGITS = "0123456789"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    offset: int
    value: object


def tokenize(source: str) -> list[_Token]:
    """Split ``source`` into tokens, raising on the first invalid character."""
    tokens: list[_Token] = []
    pos = 0
    length = len(source)
    while pos < length:
        char = source[pos]

        if char in " \t\r\n":
            pos += 1
            continue

        if char == "-" and pos + 1 < length and source[pos + 1] == "-":
            raise ExpressionSyntaxError(source, pos, "comments are not allowed")
        if char == "/" and pos + 1 < length and source[pos + 1] == "*":
            raise ExpressionSyntaxError(source, pos, "comments are not allowed")

        if char in _DIGITS:
            start = pos
            while pos < length and source[pos] in _DIGITS:
                pos += 1
            is_decimal = (
                pos + 1 < length and source[pos] == "." and source[pos + 1] in _DIGITS
            )
            if is_decimal:
                pos += 1
                while pos < length and source[pos] in _DIGITS:
                    pos += 1
            text = source[start:pos]
            value: object = float(text) if is_decimal else int(text)
            tokens.append(_Token("number", start, value))
            continue

        if char == "'":
            start = pos
            pos += 1
            chars: list[str] = []
            while True:
                if pos >= length:
                    raise ExpressionSyntaxError(
                        source, start, "unterminated string literal"
                    )
                current = source[pos]
                if current == "\n":
                    raise ExpressionSyntaxError(
                        source, pos, "newline is not allowed inside a string literal"
                    )
                if current == "'":
                    pos += 1
                    break
                if current == "\\":
                    if pos + 1 >= length:
                        raise ExpressionSyntaxError(
                            source, pos, "unterminated escape sequence"
                        )
                    escaped = source[pos + 1]
                    if escaped not in _STRING_ESCAPES:
                        raise ExpressionSyntaxError(
                            source,
                            pos,
                            f"invalid escape sequence \\{escaped}",
                        )
                    chars.append(_STRING_ESCAPES[escaped])
                    pos += 2
                    continue
                chars.append(current)
                pos += 1
            tokens.append(_Token("string", start, "".join(chars)))
            continue

        if char in _IDENTIFIER_START:
            start = pos
            pos += 1
            while pos < length and source[pos] in _IDENTIFIER_CHARS:
                pos += 1
            word = source[start:pos]
            if word == "true":
                tokens.append(_Token("boolean", start, True))
                continue
            if word == "false":
                tokens.append(_Token("boolean", start, False))
                continue
            if word == "null":
                tokens.append(_Token("null", start, None))
                continue
            if word == "not":
                tokens.append(_Token("not", start, None))
                continue
            if word in _SQL_KEYWORDS:
                raise ExpressionSyntaxError(
                    source, start, f"sql keyword '{word}' is not allowed"
                )
            parts: list[str] = [word]
            dot_offsets: list[int] = []
            while pos < length and source[pos] == ".":
                dot_offset = pos
                pos += 1
                if pos >= length or source[pos] not in _IDENTIFIER_START:
                    raise ExpressionSyntaxError(
                        source, dot_offset, "expected an identifier after '.'"
                    )
                part_start = pos
                pos += 1
                while pos < length and source[pos] in _IDENTIFIER_CHARS:
                    pos += 1
                segment = source[part_start:pos]
                if segment in _SQL_KEYWORDS:
                    raise ExpressionSyntaxError(
                        source, part_start, f"sql keyword '{segment}' is not allowed"
                    )
                parts.append(segment)
                dot_offsets.append(dot_offset)
            if len(parts) > 2:
                raise ExpressionSyntaxError(
                    source,
                    dot_offsets[1],
                    "qualified references may have at most two segments",
                )
            tokens.append(_Token("name", start, tuple(parts)))
            continue

        two_char = source[pos : pos + 2]
        if two_char in {"!=", "<=", ">="}:
            tokens.append(_Token(two_char, pos, two_char))
            pos += 2
            continue
        if char in "+-*/=<>":
            tokens.append(_Token(char, pos, char))
            pos += 1
            continue
        if char == "(":
            tokens.append(_Token("(", pos, char))
            pos += 1
            continue
        if char == ")":
            tokens.append(_Token(")", pos, char))
            pos += 1
            continue
        if char == ",":
            tokens.append(_Token(",", pos, char))
            pos += 1
            continue

        raise ExpressionSyntaxError(source, pos, f"unknown character {char!r}")

    tokens.append(_Token("eof", pos, None))
    return tokens


class Parser:
    """Recursive-descent parser over a token list."""

    def __init__(self, tokens: list[_Token], source: str) -> None:
        self._tokens = tokens
        self._source = source
        self._index = 0

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _error(self, offset: int, message: str) -> ExpressionSyntaxError:
        return ExpressionSyntaxError(self._source, offset, message)

    def parse(self) -> Expression:
        expression = self.parse_comparison()
        token = self._peek()
        if token.kind != "eof":
            raise self._error(token.offset, "unexpected trailing input")
        return expression

    def parse_comparison(self) -> Expression:
        left = self.parse_additive()
        token = self._peek()
        if token.kind in _COMPARISON_OPS:
            self._advance()
            right = self.parse_additive()
            return BinaryOperation(operator=token.kind, left=left, right=right)
        return left

    def parse_additive(self) -> Expression:
        expression = self.parse_multiplicative()
        while self._peek().kind in _ADDITIVE_OPS:
            operator = self._advance().kind
            right = self.parse_multiplicative()
            expression = BinaryOperation(
                operator=operator, left=expression, right=right
            )
        return expression

    def parse_multiplicative(self) -> Expression:
        expression = self.parse_unary()
        while self._peek().kind in _MULTIPLICATIVE_OPS:
            operator = self._advance().kind
            right = self.parse_unary()
            expression = BinaryOperation(
                operator=operator, left=expression, right=right
            )
        return expression

    def parse_unary(self) -> Expression:
        token = self._peek()
        if token.kind in _UNARY_OPS:
            self._advance()
            operand = self.parse_unary()
            return UnaryOperation(operator=token.kind, operand=operand)
        return self.parse_primary()

    def parse_primary(self) -> Expression:
        token = self._peek()
        if token.kind == "number":
            self._advance()
            return Literal(value=cast(Scalar, token.value))
        if token.kind == "string":
            self._advance()
            return Literal(value=cast(Scalar, token.value))
        if token.kind == "boolean":
            self._advance()
            return Literal(value=cast(Scalar, token.value))
        if token.kind == "null":
            self._advance()
            return Literal(value=None)
        if token.kind == "(":
            self._advance()
            expression = self.parse_comparison()
            closing = self._peek()
            if closing.kind != ")":
                raise self._error(closing.offset, "expected ')'")
            self._advance()
            return expression
        if token.kind == "name":
            self._advance()
            parts = cast("tuple[str, ...]", token.value)
            if self._peek().kind == "(":
                if len(parts) != 1:
                    raise self._error(
                        token.offset, "function names may not be qualified"
                    )
                name = parts[0]
                if name not in _FUNCTION_NAMES:
                    raise self._error(token.offset, f"unknown function '{name}'")
                self._advance()
                arguments = self.parse_arguments()
                closing = self._peek()
                if closing.kind != ")":
                    raise self._error(closing.offset, "expected ')'")
                self._advance()
                return FunctionCall(name=name, arguments=arguments)
            return Reference(parts=parts)
        raise self._error(token.offset, f"unexpected token {token.kind!r}")

    def parse_arguments(self) -> tuple[Expression, ...]:
        if self._peek().kind == ")":
            return ()
        arguments: list[Expression] = [self.parse_comparison()]
        while self._peek().kind == ",":
            self._advance()
            arguments.append(self.parse_comparison())
        return tuple(arguments)


def parse_expression(source: str) -> Expression:
    return Parser(tokenize(source), source).parse()
