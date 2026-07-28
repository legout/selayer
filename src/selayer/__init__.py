"""Public interface for the selayer semantic-layer library."""

from selayer.catalog import CatalogIssue, CatalogValidationError, SemanticLayer
from selayer.errors import QueryExecutionError
from selayer.model import (
    Aggregation,
    Cardinality,
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
)
from selayer.planning import QueryPlan, QueryPlanningError
from selayer.query import QueryEngine

__all__ = [
    "Aggregation",
    "Cardinality",
    "CatalogIssue",
    "CatalogValidationError",
    "DataSource",
    "Dimension",
    "Fact",
    "Measure",
    "Metric",
    "QueryEngine",
    "QueryExecutionError",
    "QueryPlan",
    "QueryPlanningError",
    "Relationship",
    "SemanticLayer",
]
