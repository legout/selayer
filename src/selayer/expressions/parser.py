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
# Static union of ISO/IEC 9075:2023 reserved words and the DuckDB keyword
# catalog. Some DuckDB keywords are categorized as unreserved, but references in
# this engine-neutral DSL intentionally reject SQL vocabulary in every category.
# Sources (snapshotted; never queried at runtime):
# - https://en.wikipedia.org/wiki/SQL_reserved_words (SQL:2023 column)
# - https://duckdb.org/docs/stable/sql/dialect/keywords_and_identifiers
_SQL_KEYWORDS_TEXT: Final = """
abort abs absent absolute access acos action add admin after aggregate all
allocate also alter always analyse analyze and anti any any_value are array
array_agg array_max_cardinality as asc asensitive asin asof assertion
assignment asymmetric at atan atomic attach attribute authorization avg
backward before begin begin_frame begin_partition between bigint binary bit
blob boolean both btrim by cache call called cardinality cascade cascaded case
cast catalog ceil ceiling centuries century chain char char_length character
character_length characteristics check checkpoint class classifier clob close
cluster coalesce collate collation collect column columns comment comments
commit committed compression concurrently condition configuration conflict
connect connection constraint constraints contains content continue conversion
convert copy corr corresponding cos cosh cost count covar_pop covar_samp
create cross csv cube cume_dist current current_catalog current_date
current_default_transform_group current_path current_role current_row
current_schema current_time current_timestamp current_transform_group_for_type
current_user cursor cycle data database date day days deallocate dec decade
decades decfloat decimal declare default defaults deferrable deferred define
definer delete delimiter delimiters dense_rank depends deref desc describe
detach deterministic dictionary disable discard disconnect distinct do
document domain double drop dynamic each element else empty enable encoding
encrypted end end_frame end_partition enum equals error escape event every
except exclude excluding exclusive exec execute exists exp explain export
export_state extension extensions external extract false family fetch filter
first first_value float floor following for force foreign forward frame_row
free freeze from full function functions fusion generated get glob global
grant granted greatest group grouping grouping_id groups handler having header
hold hour hours identity if ignore ilike immediate immutable implicit import
in include including increment index indexes indicator inherit inherits
initial initially inline inner inout input insensitive insert install instead
int integer intersect intersection interval into invoker is isnull isolation
join json json_array json_arrayagg json_exists json_object json_objectagg
json_query json_scalar json_serialize json_table json_table_primitive
json_value key label lag lambda language large last last_value lateral lead
leading leakproof least left level like like_regex limit listagg listen ln
load local localtime localtimestamp location lock locked log log10 logged
lower lpad ltrim macro map mapping match match_number match_recognize matched
matches materialized max maxvalue member merge method microsecond microseconds
millennia millennium millisecond milliseconds min minute minutes minvalue mod
mode modifies module month months move multiset name names national natural
nchar nclob new next no none normalize not nothing notify notnull nowait
nth_value ntile null nullif nulls numeric object occurrences_regex
octet_length of off offset oids old omit on one only open operator option
options or order ordinality others out outer over overlaps overlay overriding
owned owner parallel parameter parser partial partition partitioned passing
password pattern per percent percent_rank percentile_cont percentile_disc
period persistent pivot pivot_longer pivot_wider placing plans policy portion
position position_regex positional power pragma precedes preceding precision
prepare prepared preserve primary prior privileges procedural procedure
program ptf publication qualify quarter quarters quote range rank read reads
real reassign recheck recursive ref references referencing refresh regr_avgx
regr_avgy regr_count regr_intercept regr_r2 regr_slope regr_sxx regr_sxy
regr_syy reindex relative release rename repeatable replace replica reset
respect restart restrict result return returning returns revoke right role
rollback rollup row row_number rows rpad rtrim rule running sample savepoint
schema schemas scope scroll search second seconds secret security seek select
semi sensitive sequence sequences serializable server session session_user set
setof sets share show similar simple sin sinh skip smallint snapshot some
sorted source specific specifictype sql sqlexception sqlstate sqlwarning sqrt
stable standalone start statement static statistics stddev_pop stddev_samp
stdin stdout storage stored strict strip struct submultiset subscription
subset substring substring_regex succeeds sum summarize symmetric sysid system
system_time system_user table tables tablesample tablespace tan tanh target
temp template temporary text then ties time timestamp timezone_hour
timezone_minute to trailing transaction transform translate translate_regex
translation treat trigger trim trim_array true truncate trusted try_cast type
types uescape unbounded uncommitted unencrypted union unique unknown unlisten
unlogged unnest unpack unpivot until update upper use user using vacuum valid
validate validator value value_of values var_pop var_samp varbinary varchar
variable variadic varying verbose version versioning view views virtual
volatile week weeks when whenever where whitespace width_bucket window with
within without work wrapper write xml xmlattributes xmlconcat xmlelement
xmlexists xmlforest xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable
year years yes zone
"""
_SQL_KEYWORDS: Final[frozenset[str]] = frozenset(_SQL_KEYWORDS_TEXT.split())


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
                following_source = source[identifier.end() :].lstrip()
                if lowered not in _FUNCTIONS or not following_source.startswith("("):
                    raise _syntax_error(
                        source, offset, f"SQL keyword is not allowed: {value}"
                    )
                tokens.append(Token("identifier", value, offset))
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
                        raise _syntax_error(
                            source, start, "unterminated string literal"
                        )
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

        if (
            source.startswith("!=", offset)
            or source.startswith("<=", offset)
            or source.startswith(">=", offset)
        ):
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
            expression = BinaryOperation(
                operator, expression, self.parse_multiplicative()
            )
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
