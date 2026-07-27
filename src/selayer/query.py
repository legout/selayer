from __future__ import annotations

from contextlib import suppress
from typing import Self
from uuid import uuid4

import duckdb
import polars as pl

from selayer.catalog import SemanticLayer
from selayer.compilation import compile_duckdb
from selayer.errors import QueryExecutionError
from selayer.planning import QueryPlan, QueryRequest, plan_query

_REDACTED_PARAMETER = "<redacted>"


def _redact_parameter_values(message: str, parameters: tuple[object, ...]) -> str:
    """Remove every useful scalar representation from a driver diagnostic."""
    candidates: set[str] = set()
    for value in parameters:
        candidates.add(str(value))
        candidates.add(repr(value))
    for candidate in sorted(candidates, key=lambda item: (-len(item), item)):
        if candidate:
            message = message.replace(candidate, _REDACTED_PARAMETER)
    return message


class QueryEngine:
    """Orchestrate catalog loading, planning, compilation, and execution."""

    def __init__(self, semantic_layer: SemanticLayer) -> None:
        self.semantic_layer = semantic_layer
        self.conn = duckdb.connect(":memory:")
        source_error: QueryExecutionError | None = None
        try:
            for name, data_source in semantic_layer.data_sources.items():
                self.conn.register(
                    name, self._load_source(data_source.type, data_source.path)
                )
        except Exception:  # noqa: BLE001 - every source failure must be sanitized
            with suppress(Exception):
                self.conn.close()
            source_error = QueryExecutionError(str(uuid4()), "source loading failed")
        if source_error is not None:
            raise source_error

    @staticmethod
    def _load_source(source_type: str, path: str) -> pl.DataFrame:
        if source_type == "parquet":
            return pl.read_parquet(path)
        if source_type == "csv":
            return pl.read_csv(path)
        raise ValueError(f"Unsupported data source type: {source_type}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def plan(
        self,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> QueryPlan:
        """Normalize caller inputs and resolve them into an immutable plan."""
        request = QueryRequest(
            metrics=tuple(metrics),
            dimensions=tuple(dimensions or ()),
            filters=filters,  # type: ignore[arg-type]
        )
        return plan_query(self.semantic_layer, request)

    def query(
        self,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> pl.DataFrame:
        """Execute a semantic query and return its result as Polars."""
        plan = self.plan(metrics, dimensions, filters)
        compiled = compile_duckdb(plan)
        query_id = str(uuid4())
        execution_error: QueryExecutionError | None = None
        result: pl.DataFrame | None = None
        try:
            result = self.conn.execute(compiled.sql, compiled.parameters).pl()
        except duckdb.Error as error:
            if compiled.parameters:
                diagnostic = _redact_parameter_values(str(error), compiled.parameters)
                message = f"query execution failed: {diagnostic}"
            else:
                # With no caller-provided values, the generated SQL and the
                # DuckDB diagnostic are useful debugging information.
                message = f"query execution failed: {error}; SQL: {compiled.sql}"
            execution_error = QueryExecutionError(query_id, message)
        if execution_error is not None:
            raise execution_error
        assert result is not None
        return result


__all__ = ["QueryEngine"]
