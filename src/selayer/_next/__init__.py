"""Staged immutable grain-aware catalog (schema version 1).

This is a **temporary, unexported** staging namespace. It holds the breaking
immutable model that will replace the mutable ``selayer.model``/``selayer.catalog``
runtime in Task 5. Nothing here is re-exported from the top-level ``selayer``
package, so the existing runtime and its tests remain green while the new model
matures.

Public surface:

- frozen model types ``DataSource``, ``Dimension``, ``Fact``, ``Measure``,
  ``Metric``, ``Relationship``, ``SemanticLayer`` (with ``Aggregation`` and
  ``Cardinality`` literals);
- ``CatalogIssue`` and ``CatalogValidationError``;
- ``SemanticLayer.load`` strict YAML loader.
"""

from selayer._next.catalog import CatalogIssue, CatalogValidationError
from selayer._next.model import (
    Aggregation,
    Cardinality,
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
)

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
    "Relationship",
    "SemanticLayer",
]
