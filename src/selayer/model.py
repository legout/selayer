from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import duckdb
import polars as pl

if TYPE_CHECKING:
    from selayer.query import QueryEngine


@dataclass
class DataSource:
    """A named tabular source used by a semantic layer."""

    name: str
    type: str
    path: str
    schema: dict[str, str] | None = None
    connection_params: dict[str, Any] | None = None

    def get_data(self) -> pl.DataFrame:
        """Load the source into a Polars DataFrame."""
        if self.type == "parquet":
            return pl.read_parquet(self.path)
        if self.type == "csv":
            return pl.read_csv(self.path)
        if self.type in {"postgres", "sqlite"}:
            return self._get_database_data()
        raise ValueError(f"Unsupported data source type: {self.type}")

    def _get_database_data(self) -> pl.DataFrame:
        params = self.connection_params or {}
        table = params.get("table")
        if not isinstance(table, str) or not table:
            raise ValueError(
                f"Data source '{self.name}' requires connection_params.table"
            )

        connection = duckdb.connect(":memory:")
        try:
            if self.type == "postgres":
                connection_string = params.get("connection_string")
                if not isinstance(connection_string, str) or not connection_string:
                    raise ValueError(
                        f"Data source '{self.name}' requires "
                        "connection_params.connection_string"
                    )
                connection.execute("INSTALL postgres_scanner; LOAD postgres_scanner;")
                relation = connection.sql(
                    "SELECT * FROM postgres_scan($connection, 'public', $table)",
                    params={"connection": connection_string, "table": table},
                )
            else:
                connection.execute("INSTALL sqlite; LOAD sqlite;")
                relation = connection.sql(
                    "SELECT * FROM sqlite_scan($path, $table)",
                    params={"path": self.path, "table": table},
                )
            return relation.pl()
        finally:
            connection.close()


@dataclass
class Fact:
    """An atomic value in a fact table."""

    name: str
    description: str
    data_type: str
    source: str
    column: str
    is_additive: bool = True


@dataclass
class Measure:
    """An aggregation of a fact."""

    name: str
    description: str
    fact: str
    aggregation: Literal["sum", "avg", "min", "max", "count", "count_distinct"] = "sum"
    filter_expression: str | None = None

    def to_sql(self) -> str:
        """Compile the measure to SQL with fact placeholders."""
        aggregate_functions = {
            "sum": "SUM",
            "avg": "AVG",
            "min": "MIN",
            "max": "MAX",
            "count": "COUNT",
        }
        fact_reference = "{fact_source}.{fact_column}"
        expression = fact_reference
        if self.filter_expression:
            expression = (
                f"CASE WHEN {self.filter_expression} THEN {fact_reference} "
                "ELSE NULL END"
            )
        if self.aggregation == "count_distinct":
            return f"COUNT(DISTINCT {expression})"
        return f"{aggregate_functions[self.aggregation]}({expression})"


@dataclass
class Dimension:
    name: str
    description: str
    data_type: str
    source: str
    column: str
    hierarchies: list[str] = field(default_factory=list)


@dataclass
class Hierarchy:
    name: str
    description: str
    levels: list[str]


@dataclass
class Metric:
    name: str
    description: str
    expression: str
    measures: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def evaluate(self, context: dict[str, Any], engine: QueryEngine) -> Any:
        """Evaluate this metric expression with a query engine."""
        return engine.evaluate_expression(self.expression, context)


@dataclass
class Relationship:
    name: str
    source: str
    target: str
    type: str = "one_to_many"
    source_column: str = ""
    target_column: str = ""
