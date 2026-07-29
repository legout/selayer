from __future__ import annotations

from typing import Self
from uuid import uuid4

import duckdb
import polars as pl

from selayer.catalog import SemanticLayer
from selayer.compilation import CompiledQuery, compile_duckdb
from selayer.errors import QueryExecutionError
from selayer.planning import QueryPlan, QueryRequest, plan_query
from selayer.sources.base import ReloadResult, SourceStatus
from selayer.sources.profiles import (
    ArrowProviderResolver,
    MappingArrowProviderResolver,
    MappingProfileResolver,
    RuntimeProfileResolver,
)
from selayer.sources.registry import SourceRegistry

_DUCKDB_DIAGNOSTIC_CATEGORIES = (
    "Conversion Error",
    "Binder Error",
    "Catalog Error",
    "Constraint Error",
    "Invalid Input Error",
)


def _duckdb_diagnostic_category(error: duckdb.Error) -> str:
    """Return only an anchored, allowlisted category from a driver error."""
    diagnostic = str(error)
    for category in _DUCKDB_DIAGNOSTIC_CATEGORIES:
        if diagnostic.startswith(category) and (
            len(diagnostic) == len(category)
            or diagnostic[len(category)] in (":", " ", "\n")
        ):
            return category
    return "DuckDB Error"


class QueryEngine:
    """Orchestrate catalog loading, planning, compilation, and execution.

    Sources are loaded through a :class:`~selayer.sources.registry.SourceRegistry`
    that owns the DuckDB connection; there is no eager Polars source reading.
    Queries obtain a plan, enter ``registry.bind(plan)``, compile, and execute
    under the registry lock so a reload can never swap a handle mid-query.

    The connection is private to registry-aware execution.  Source reload and
    status lifecycle is delegated to the registry while query and plan
    signatures are preserved.
    """

    def __init__(
        self,
        semantic_layer: SemanticLayer,
        *,
        profiles: RuntimeProfileResolver | None = None,
        arrow_providers: ArrowProviderResolver | None = None,
    ) -> None:
        self.semantic_layer = semantic_layer
        self._connection = duckdb.connect(":memory:")
        self._registry = SourceRegistry.create(
            semantic_layer,
            self._connection,
            profiles or MappingProfileResolver({}),
            arrow_providers or MappingArrowProviderResolver({}),
        )

    def close(self) -> None:
        self._registry.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- source lifecycle --------------------------------------------------

    def reload_source(self, source_id: str) -> ReloadResult:
        """Atomically reload one source and return the generation change."""
        return self._registry.reload_source(source_id)

    def reload_all(self) -> tuple[ReloadResult, ...]:
        """Atomically reload every source in sorted source-ID order."""
        return self._registry.reload_all()

    def source_status(self, source_id: str) -> SourceStatus:
        """Return the current health/generation snapshot for one source."""
        return self._registry.status(source_id)

    # -- planning and execution -------------------------------------------

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
        query_id = str(uuid4())
        execution_error: QueryExecutionError | None = None
        result: pl.DataFrame | None = None
        compiled: CompiledQuery | None = None
        try:
            with self._registry.bind(plan):
                # Compile inside the binding so the query-scoped sources are
                # registered before compilation and the registry lock is held
                # across both compile and execute.
                compiled = compile_duckdb(plan)
                result = self._registry.execute(compiled.sql, compiled.parameters).pl()
        except duckdb.Error as error:
            if compiled is None:
                message = f"query execution failed: {error}"
            elif compiled.parameters:
                category = _duckdb_diagnostic_category(error)
                message = (
                    f"query execution failed: parameterized query failed ({category})"
                )
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
