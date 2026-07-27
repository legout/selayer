from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from selayer.expressions.ast import (
    BinaryOperation,
    Expression,
    ExpressionSyntaxError,
    FunctionCall,
    Literal,
    Reference,
    UnaryOperation,
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    offset: int


_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE: Final = re.compile(r"(?:[0-9]+\.[0-9]+|[0-9]+)")
_COMPARISON_OPERATORS: Final = ("!=", "<=", ">=", "=", "<", ">")
_FUNCTIONS: Final = frozenset({"coalesce", "nullif", "abs", "lower", "upper", "if"})
_SQL_KEYWORDS: Final = frozenset(
    {
        "select",
        "from",
        "where",
        "group",
        "by",
        "order",
        "having",
        "join",
        "on",
        "as",
        "case",
        "when",
        "then",
        "else",
        "end",
        "limit",
        "offset",
        "union",
        "all",
        "distinct",
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "table",
        "and",
        "or",
        "like",
        "in",
        "is",
    }
)


def _syntax_error(source: str, offset: int, message: str) -> ExpressionSyntaxError:
    return ExpressionSyntaxError(source, offset, message)


def tokenize(source: str) -> tuple[Token, ...]:
    tokens: list[Token] = []
    offset = 0
    while offset < len(source):
        character = source[offset]
        if source.startswith("--", offset) or source.startswith("/*", offset):
            raise _syntax_error(source, offset, "comments are not allowed")
        if character.isspace():
            offset += 1
            continue

        identifier = _IDENTIFIER_RE.match(source, offset)
        if identifier is not None:
            value = source[identifier.start() : identifier.end()]
            lowered = value.lower()
            if lowered in {"true", "false", "null"}:
                tokens.append(Token("literal", lowered, offset))
            elif lowered == "not":
                tokens.append(Token("operator", "not", offset))
            elif lowered in _SQL_KEYWORDS:
                raise _syntax_error(source, offset, f"SQL keyword is not allowed: {value}")
            else:
                tokens.append(Token("identifier", value, offset))
            offset = identifier.end()
            continue

        number = _NUMBER_RE.match(source, offset)
        if number is not None:
            value = source[number.start() : number.end()]
            tokens.append(Token("number", value, offset))
            offset = number.end()
            continue

        if character in "'\"":
            quote = character
            start = offset
            offset += 1
            characters: list[str] = []
            while offset < len(source):
                character = source[offset]
                if character == quote:
                    tokens.append(Token("string", "".join(characters), start))
                    offset += 1
                    break
                if character == "\\":
                    offset += 1
                    if offset >= len(source):
                        raise _syntax_error(source, start, "unterminated string literal")
                    escaped = source[offset]
                    characters.append(
                        {
                            "n": "\n",
                            "r": "\r",
                            "t": "\t",
                            "\\": "\\",
                            "'": "'",
                            '"': '"',
                        }.get(escaped, escaped)
                    )
                    offset += 1
                    continue
                if character in "\r\n":
                    raise _syntax_error(source, offset, "newline in string literal")
                characters.append(character)
                offset += 1
            else:
                raise _syntax_error(source, start, "unterminated string literal")
            continue

        if source.startswith("!=", offset) or source.startswith("<=", offset) or source.startswith(">=", offset):
            tokens.append(Token("operator", source[offset : offset + 2], offset))
            offset += 2
            continue
        if character in "+-*/=<>(),.":
            kind = "operator" if character in "+-*/=<>" else "punctuation"
            tokens.append(Token(kind, character, offset))
            offset += 1
            continue
        raise _syntax_error(source, offset, f"unexpected character: {character!r}")

    tokens.append(Token("eof", "", len(source)))
    return tuple(tokens)


class Parser:
    def __init__(self, tokens: tuple[Token, ...], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.position = 0

    def current(self) -> Token:
        return self.tokens[self.position]

    def advance(self) -> Token:
        token = self.current()
        self.position += 1
        return token

    def fail(self, message: str, token: Token | None = None) -> None:
        raise _syntax_error(self.source, (token or self.current()).offset, message)

    def accept(self, value: str) -> Token | None:
        token = self.current()
        if token.value == value:
            self.position += 1
            return token
        return None

    def expect(self, value: str, message: str) -> Token:
        token = self.accept(value)
        if token is None:
            self.fail(message)
            raise AssertionError("unreachable")
        return token

    def parse(self) -> Expression:
        expression = self.parse_comparison()
        token = self.current()
        if token.kind != "eof":
            self.fail("trailing tokens are not allowed", token)
        return expression

    def parse_comparison(self) -> Expression:
        expression = self.parse_additive()
        token = self.current()
        if token.kind == "operator" and token.value in _COMPARISON_OPERATORS:
            operator = self.advance().value
            expression = BinaryOperation(operator, expression, self.parse_additive())
        return expression

    def parse_additive(self) -> Expression:
        expression = self.parse_multiplicative()
        while self.current().value in {"+", "-"}:
            operator = self.advance().value
            expression = BinaryOperation(operator, expression, self.parse_multiplicative())
        return expression

    def parse_multiplicative(self) -> Expression:
        expression = self.parse_unary()
        while self.current().value in {"*", "/"}:
            operator = self.advance().value
            expression = BinaryOperation(operator, expression, self.parse_unary())
        return expression

    def parse_unary(self) -> Expression:
        token = self.current()
        if token.value in {"+", "-", "not"}:
            self.advance()
            return UnaryOperation(token.value, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Expression:
        token = self.current()
        if token.kind == "number":
            self.advance()
            numeric_value: int | float = (
                float(token.value) if "." in token.value else int(token.value)
            )
            return Literal(numeric_value)
        if token.kind == "string":
            self.advance()
            return Literal(token.value)
        if token.kind == "literal":
            self.advance()
            literal_value: bool | None = {
                "true": True,
                "false": False,
                "null": None,
            }[token.value]
            return Literal(literal_value)
        if token.kind == "identifier":
            self.advance()
            if self.accept("(") is not None:
                if token.value not in _FUNCTIONS:
                    self.fail(f"function is not allowed: {token.value}", token)
                return FunctionCall(token.value, self.parse_arguments())
            parts = [token.value]
            if self.accept(".") is not None:
                next_token = self.current()
                if next_token.kind != "identifier":
                    self.fail("expected identifier after '.'", next_token)
                parts.append(self.advance().value)
                if self.current().value == ".":
                    self.fail("references may have at most two parts")
            return Reference(tuple(parts))
        if self.accept("(") is not None:
            expression = self.parse_comparison()
            self.expect(")", "expected ')'")
            return expression
        self.fail("expected expression", token)
        raise AssertionError("unreachable")

    def parse_arguments(self) -> tuple[Expression, ...]:
        if self.accept(")") is not None:
            return ()
        arguments = [self.parse_comparison()]
        while self.accept(",") is not None:
            arguments.append(self.parse_comparison())
        self.expect(")", "expected ')' after function arguments")
        return tuple(arguments)


def parse_expression(source: str) -> Expression:
    return Parser(tokenize(source), source).parse()
