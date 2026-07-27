"""SQL extraction from LLM text responses.

The LLM is told to emit SQL only, but in practice it returns:
    * plain SQL
    * ```sql ... ``` fenced blocks
    * ``` ... ``` unfenced blocks
    * leading prose like "Here is the query: ..." before the SQL
    * {"error": "reason"} JSON for unanswerable questions

This module normalises all of these to (sql, error) where one is set.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(
    r"```(?:sql|sqlite|duckdb)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE
)


def extract_sql(text: str) -> tuple[str, str | None]:
    """Return (sql, error_message). Exactly one will be non-empty."""
    if not text:
        return "", "empty LLM response"

    t = text.strip()

    # JSON error sentinel first
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if "error" in obj:
                return "", str(obj["error"])
            if "sql" in obj and isinstance(obj["sql"], str):
                return obj["sql"].strip(), None

    # Fenced code blocks first
    m = _FENCE.search(t)
    if m:
        return _clean(m.group(1)), None

    # Otherwise: strip any leading prose before the first SQL keyword
    sql_kw = re.search(r"\b(?:select|with)\b", t, re.IGNORECASE)
    if sql_kw:
        candidate = t[sql_kw.start() :]
        # cut off after the last ';' (or end)
        semi = candidate.rfind(";")
        return _clean(candidate if semi == -1 else candidate[: semi + 1]), None

    return "", "could not locate SQL in LLM response"


def _clean(sql: str) -> str:
    """Trim whitespace and trailing semicolons (DuckDB tolerates them either way)."""
    s = sql.strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s
