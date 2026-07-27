"""SQL safety validation.

Defense in depth: even though the semantic layer is read-only, we still
guard at the SQL level. The LLM *should* only emit SELECT, but if it
hallucinates a DROP TABLE we want to refuse the query before it reaches
DuckDB rather than after.
"""

from __future__ import annotations

import re

_FORBIDDEN = (
    # DDL
    "create",
    "drop",
    "alter",
    "truncate",
    "rename",
    # DML
    "insert",
    "update",
    "delete",
    "merge",
    "replace",
    # session / system
    "attach",
    "detach",
    "install",
    "load",
    "pragma",
    "copy",
    "export",
)

_ALLOWED_STARTS = ("select", "with")


def is_safe_sql(sql: str) -> tuple[bool, str]:
    """Return (is_safe, reason_if_not). Reads are allowed, anything else is not."""
    if not sql or not sql.strip():
        return False, "empty sql"

    s = sql.strip()
    # allow a leading CTE paren (rare; be lenient)
    head = s.lstrip("(").lstrip().lower()

    if not head.startswith(_ALLOWED_STARTS):
        return False, f"statement must begin with SELECT or WITH, got: {head[:32]}"

    # Simple keyword blocklist — case-insensitive word-boundary match
    tokens = re.findall(r"[a-z_]+", s.lower())
    for tok in tokens:
        if tok in _FORBIDDEN:
            return False, f"forbidden keyword '{tok}' in statement"

    # Disallow trailing non-space '; ' multiple statements
    bare = s.rstrip().rstrip(";")
    if ";" in bare:
        return False, "only single statements allowed"

    return True, ""
