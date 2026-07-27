"""System prompt construction.

Produces the single system prompt that primes the LLM to behave like a
DuckDB-fluent analyst who knows the user's semantic layer. Exposes:

    build_system_prompt(schema: Schema) -> str

The prompt is deliberately terse — every token costs latency and money.
"""

from __future__ import annotations

from .types import FieldSpec, Schema, TableSpec

# A small but high-leverage set of patterns. Real production systems tune
# these by adding new examples that match their own data shape.
FEW_SHOT: list[tuple[str, str]] = [
    (
        "Revenue and order count by country × quarter matrix",
        (
            "SELECT c.country AS country,\n"
            "       strftime(DATE_TRUNC('quarter', CAST(o.created_at AS DATE)), '%Y-Q')\n"
            "         || CAST(EXTRACT(QUARTER FROM CAST(o.created_at AS DATE)) AS INT) AS quarter,\n"
            "       COUNT(DISTINCT o.id) AS order_count,\n"
            "       ROUND(SUM(o.amount), 2) AS revenue\n"
            "FROM orders o JOIN customers c ON o.customer_id = c.id\n"
            "GROUP BY 1, 2\n"
            "ORDER BY 1, 2;"
        ),
    ),
    (
        "Top 10 products by units sold",
        (
            "SELECT p.id AS product_id, p.name,\n"
            "       SUM(oi.quantity) AS units_sold\n"
            "FROM order_items oi JOIN products p ON oi.product_id = p.id\n"
            "GROUP BY 1, 2 ORDER BY units_sold DESC LIMIT 10;"
        ),
    ),
    (
        "Order completion rate last quarter",
        (
            "SELECT ROUND(\n"
            "  100.0 * SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)\n"
            "  / COUNT(*), 2) AS completion_rate_pct\n"
            "FROM orders\n"
            "WHERE created_at >= DATE_TRUNC('quarter', CURRENT_DATE - INTERVAL 3 MONTH)\n"
            "  AND created_at <  DATE_TRUNC('quarter', CURRENT_DATE);"
        ),
    ),
]


SYSTEM_HEADER = """You translate business questions into a single DuckDB SQL
statement. The DuckDB instance has the tables and semantic concepts below.
DuckDB SQL is the dialect; use read-only statements (SELECT / WITH).

RULES:
1. Output ONE DuckDB SQL statement, no commentary, no markdown.
2. Time bucketing: DATE_TRUNC('quarter'|'month'|'day', orders.created_at).
   For quarter labels use:
     strftime(DATE_TRUNC('quarter', CAST(created_at AS DATE)), '%Y-Q')
     || CAST(EXTRACT(QUARTER FROM CAST(created_at AS DATE)) AS INT)
3. Always qualify columns with the table name.
4. SELECT-only — never write INSERT/UPDATE/DELETE/DDL.
5. Use COUNT(DISTINCT <id>) when counting orders.
6. If the question is unanswerable, output exactly:
   {"error": "short reason"}

Below is the schema and the user question. Respond with ONLY the SQL.
"""


def build_system_prompt(schema: Schema) -> str:
    parts = [
        SYSTEM_HEADER,
        "",
        f"# Semantic layer: {schema.layer_name}",
        schema.layer_description,
        "",
    ]

    parts.append("## Measures (aggregations — use directly in SELECT/GROUP BY)")
    for f in schema.fields:
        if f.kind == "measure":
            parts.append(f"- {f.name}: {f.description}  (SQL: {f.sql_hint})")

    parts.append("")
    parts.append("## Dimensions (group-by keys)")
    for f in schema.fields:
        if f.kind == "dimension":
            parts.append(f"- {f.name}: {f.description}  (SQL: {f.sql_hint})")

    parts.append("")
    parts.append("## Metrics (derived ratios)")
    for f in schema.fields:
        if f.kind == "metric":
            parts.append(f"- {f.name}: {f.description}")

    parts.append("")
    parts.append(
        "## Tables (raw column listings — fall back to these when the semantic layer is insufficient)"
    )
    for t in schema.tables:
        cols = ", ".join(f"{c['name']} {c['type']}" for c in t.columns)
        parts.append(f"- {t.name} ({cols})")

    parts.append("")
    parts.append("## Examples (NL question → DuckDB SQL)")
    for q, sql in FEW_SHOT:
        parts.append(f"Q: {q}")
        parts.append(sql)
        parts.append("")

    # NOTE: the user question used to be embedded here as a
    # `{user_question}` placeholder, but it was never substituted — the
    # actual question is sent as a separate user-role message by
    # `OpenAIClient.complete()`. Leaving the literal placeholder in the
    # prompt contradicted the system header's "below is the user
    # question" claim and caused the LLM to return empty content for
    # questions that didn't match a few-shot example verbatim.
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Schema introspection from a `selayer.SemanticLayer`
# ---------------------------------------------------------------------------


def schema_from_semantic_layer(layer) -> Schema:
    """Translate a `selayer.SemanticLayer` into our `Schema` for the prompt builder."""

    fields: list[FieldSpec] = []

    for name, m in layer.measures.items():
        sql_hint = _measure_sql_hint(layer, m)
        fields.append(
            FieldSpec(
                name=name,
                description=m.description,
                kind="measure",
                sql_hint=sql_hint,
            )
        )

    for name, d in layer.dimensions.items():
        hiers = list(d.hierarchies) if isinstance(d.hierarchies, list) else []
        fields.append(
            FieldSpec(
                name=name,
                description=d.description,
                kind="dimension",
                data_type=d.data_type,
                sql_hint=f"{d.source}.{d.column}",
                hierarchies=hiers,
            )
        )

    for name, mt in layer.metrics.items():
        fields.append(
            FieldSpec(
                name=name,
                description=mt.description,
                kind="metric",
                sql_hint=mt.expression,
            )
        )

    tables = [
        TableSpec(name=ds.name, description=f"Data source ({ds.type})", columns=[])
        for ds in layer.data_sources.values()
    ]
    return Schema(
        layer_name=layer.name,
        layer_description=layer.description,
        fields=fields,
        tables=tables,
    )


def _measure_sql_hint(layer, measure) -> str:
    """Return the SQL rendering of a measure, e.g. `SUM(orders.amount)`."""
    fact = layer.facts.get(measure.fact)
    if fact is None:
        return f"<unknown fact: {measure.fact}>"
    column = f"{fact.source}.{fact.column}"
    agg = (measure.aggregation or "sum").upper()
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {column})"
    if measure.filter_expression:
        return f"{agg}(CASE WHEN {measure.filter_expression} THEN {column} END)"
    return f"{agg}({column})"
