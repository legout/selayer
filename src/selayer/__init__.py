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
from selayer.sources.base import ReloadResult, SourceStatus
from selayer.sources.errors import (
    SourceConnectionError,
    SourceDependencyError,
    SourceError,
    SourceProfileError,
    SourceReloadError,
    SourceSchemaError,
)
from selayer.sources.schema import FieldSchema, TableSchema

from .okf import OkfBundle

__all__ = [
    "Aggregation",
    "Cardinality",
    "CatalogIssue",
    "CatalogValidationError",
    "DataSource",
    "Dimension",
    "Fact",
    "FieldSchema",
    "Measure",
    "Metric",
    "OkfBundle",
    "QueryEngine",
    "QueryExecutionError",
    "QueryPlan",
    "QueryPlanningError",
    "Relationship",
    "ReloadResult",
    "SemanticLayer",
    "SourceConnectionError",
    "SourceDependencyError",
    "SourceError",
    "SourceProfileError",
    "SourceReloadError",
    "SourceSchemaError",
    "SourceStatus",
    "TableSchema",
]
