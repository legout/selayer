"""Public execution errors."""

from __future__ import annotations


class QueryExecutionError(RuntimeError):
    """Raised when DuckDB cannot execute a planned query."""

    def __init__(self, query_id: str, message: str) -> None:
        self.query_id = query_id
        self.message = message
        super().__init__(f"query {query_id}: {message}")


__all__ = ["QueryExecutionError"]
