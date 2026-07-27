from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from typing import Any

import duckdb
import polars as pl

from selayer.catalog import SemanticLayer
from selayer.model import Measure, Relationship

_CONTEXT_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _qualified(table: str, column: str) -> str:
    return f"{_quote_identifier(table)}.{_quote_identifier(column)}"


class QueryEngine:
    """Compile semantic metric queries and execute them with DuckDB."""

    def __init__(self, semantic_layer: SemanticLayer, engine_type: str = "duckdb"):
        if engine_type != "duckdb":
            raise ValueError(f"Unsupported engine type: {engine_type}")
        self.semantic_layer = semantic_layer
        self.engine_type = engine_type
        self.conn = duckdb.connect(":memory:")
        for name, data_source in semantic_layer.data_sources.items():
            self.conn.register(name, data_source.get_data())

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def evaluate_expression(self, expression: str, context: dict[str, Any]) -> Any:
        """Evaluate a trusted catalog expression after binding context values."""
        parameters: list[Any] = []

        def bind_placeholder(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise ValueError(f"missing expression context value: {key}")
            parameters.append(context[key])
            return "?"

        bound_expression = _CONTEXT_PLACEHOLDER.sub(bind_placeholder, expression)
        return self.conn.sql(bound_expression, params=parameters).fetchall()

    def _find_join_path(
        self, source_tables: Iterable[str], target_table: str
    ) -> list[tuple[str, str, bool]] | None:
        sources = sorted(set(source_tables))
        if target_table in sources:
            return []

        visited = set(sources)
        queue = deque((table, []) for table in sources)
        relationships = sorted(
            self.semantic_layer.relationships.values(), key=lambda item: item.name
        )

        while queue:
            current_table, path = queue.popleft()
            for relationship in relationships:
                neighbor = self._neighbor(relationship, current_table)
                if neighbor is None or neighbor in visited:
                    continue
                next_path = path + [
                    (
                        neighbor,
                        self._join_condition(relationship),
                        self._expands_rows(relationship, current_table),
                    )
                ]
                if neighbor == target_table:
                    return next_path
                visited.add(neighbor)
                queue.append((neighbor, next_path))
        return None

    @staticmethod
    def _neighbor(relationship: Relationship, table: str) -> str | None:
        if relationship.source == table:
            return relationship.target
        if relationship.target == table:
            return relationship.source
        return None

    @staticmethod
    def _join_condition(relationship: Relationship) -> str:
        return (
            f"{_qualified(relationship.source, relationship.source_column)} = "
            f"{_qualified(relationship.target, relationship.target_column)}"
        )

    @staticmethod
    def _expands_rows(relationship: Relationship, current_table: str) -> bool:
        if relationship.type == "one_to_one":
            return False
        if relationship.type == "one_to_many":
            return current_table == relationship.source
        if relationship.type == "many_to_one":
            return current_table == relationship.target
        return True

    def _compile_measure(self, measure: Measure) -> tuple[str, str]:
        try:
            fact = self.semantic_layer.facts[measure.fact]
        except KeyError as exc:
            raise ValueError(
                f"measure '{measure.name}' references unknown fact '{measure.fact}'"
            ) from exc
        sql = measure.to_sql()
        sql = sql.replace("{fact_source}", _quote_identifier(fact.source))
        sql = sql.replace("{fact_column}", _quote_identifier(fact.column))
        return sql, fact.source

    def _compile_metric(self, metric_name: str) -> tuple[str, set[str]]:
        metric = self.semantic_layer.metrics[metric_name]
        expression = metric.expression
        tables: set[str] = set()
        for measure_name in metric.measures:
            try:
                measure = self.semantic_layer.measures[measure_name]
            except KeyError as exc:
                raise ValueError(
                    f"metric '{metric_name}' references unknown measure "
                    f"'{measure_name}'"
                ) from exc
            measure_sql, table = self._compile_measure(measure)
            expression = expression.replace(f"{{{{{measure_name}}}}}", measure_sql)
            tables.add(table)
        return expression, tables

    def _compile_filters(
        self, filters: dict[str, Any]
    ) -> tuple[list[str], list[Any], set[str]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        tables: set[str] = set()
        for filter_name, value in filters.items():
            dimension = self.semantic_layer.dimensions.get(filter_name)
            if dimension is None:
                raise ValueError(f"unknown filter dimension: {filter_name}")
            column = _qualified(dimension.source, dimension.column)
            tables.add(dimension.source)
            if isinstance(value, tuple) and len(value) == 2:
                clauses.append(f"{column} BETWEEN ? AND ?")
                parameters.extend(value)
            elif isinstance(value, list):
                if not value:
                    clauses.append("FALSE")
                else:
                    placeholders = ", ".join("?" for _ in value)
                    clauses.append(f"{column} IN ({placeholders})")
                    parameters.extend(value)
            else:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        return clauses, parameters, tables

    def query(
        self,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> pl.DataFrame:
        """Execute a semantic metric query."""
        dimensions = dimensions or []
        filters = filters or {}
        if not metrics:
            raise ValueError("query requires at least one metric")

        unknown_metrics = sorted(set(metrics) - self.semantic_layer.metrics.keys())
        if unknown_metrics:
            raise ValueError(f"unknown metric: {', '.join(unknown_metrics)}")
        unknown_dimensions = sorted(
            set(dimensions) - self.semantic_layer.dimensions.keys()
        )
        if unknown_dimensions:
            raise ValueError(f"unknown dimension: {', '.join(unknown_dimensions)}")

        select_clauses: list[str] = []
        required_tables: set[str] = set()
        fact_tables: set[str] = set()

        for dimension_name in dimensions:
            dimension = self.semantic_layer.dimensions[dimension_name]
            required_tables.add(dimension.source)
            select_clauses.append(
                f"{_qualified(dimension.source, dimension.column)} AS "
                f"{_quote_identifier(dimension_name)}"
            )

        for metric_name in metrics:
            expression, tables = self._compile_metric(metric_name)
            if len(tables) > 1:
                raise ValueError(
                    f"metric '{metric_name}' spans multiple fact sources: "
                    f"{', '.join(sorted(tables))}"
                )
            required_tables.update(tables)
            fact_tables.update(tables)
            select_clauses.append(f"({expression}) AS {_quote_identifier(metric_name)}")

        if len(fact_tables) > 1:
            raise ValueError(
                f"query spans multiple fact sources: {', '.join(sorted(fact_tables))}"
            )

        where_clauses, parameters, filter_tables = self._compile_filters(filters)
        required_tables.update(filter_tables)
        if not required_tables:
            raise ValueError("query does not reference a data source")

        from_table = min(fact_tables or required_tables)
        joined_tables = {from_table}
        join_clauses: list[str] = []
        for target_table in sorted(required_tables - joined_tables):
            path = self._find_join_path(joined_tables, target_table)
            if path is None:
                raise ValueError(
                    f"no relationship path from {sorted(joined_tables)} "
                    f"to '{target_table}'"
                )
            for next_table, condition, expands_rows in path:
                if expands_rows:
                    raise ValueError(
                        f"unsafe fan-out relationship path to '{target_table}'"
                    )
                if next_table not in joined_tables:
                    join_clauses.append(
                        f"JOIN {_quote_identifier(next_table)} ON {condition}"
                    )
                    joined_tables.add(next_table)

        sql = f"SELECT {', '.join(select_clauses)} FROM {_quote_identifier(from_table)}"
        if join_clauses:
            sql += " " + " ".join(join_clauses)
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        if dimensions:
            positions = ", ".join(str(index) for index in range(1, len(dimensions) + 1))
            sql += f" GROUP BY {positions}"

        return self.conn.sql(sql, params=parameters).pl()
