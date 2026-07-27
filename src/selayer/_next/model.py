"""Immutable schema-version-1 catalog model types.

These frozen dataclasses are the staged breaking replacement for the mutable
``selayer.model`` types. They are intentionally **not** re-exported from the
top-level ``selayer`` package yet: they live under the private ``selayer._next``
staging namespace so the existing exported runtime stays green until Task 5.

Conventions enforced by the catalog loader (not by the dataclasses themselves):

- schema version is exactly ``1``;
- every collection mapping key is a stable lowercase identifier matching
  ``[a-z][a-z0-9_]*``;
- every data source declares a non-empty ``grain``;
- relationship ``type`` is mapped onto the :data:`Cardinality` literal and may
  take any of its four values (``many_to_many`` is valid here; planning it is
  deferred to the planner task).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from selayer.expressions.ast import Expression

type Aggregation = Literal["sum", "avg", "min", "max", "count", "count_distinct"]

type Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


@dataclass(frozen=True, slots=True)
class DataSource:
    """A named tabular source and the columns that identify one of its rows."""

    name: str
    type: str
    path: str
    grain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Dimension:
    """A named grouping/filtering field backed by a source column."""

    name: str
    source: str
    column: str
    data_type: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Fact:
    """A row-level value evaluated at its anchor source grain."""

    name: str
    source: str
    expression: Expression
    data_type: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Measure:
    """An aggregation of one fact; its grain is inherited from the fact."""

    name: str
    fact: str
    aggregation: Aggregation
    description: str = ""


@dataclass(frozen=True, slots=True)
class Metric:
    """An aggregate-level formula over declared measures."""

    name: str
    expression: Expression
    measures: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True, slots=True)
class Relationship:
    """A declared join between two data sources."""

    name: str
    source: str
    target: str
    cardinality: Cardinality
    source_column: str
    target_column: str


@dataclass(frozen=True, slots=True)
class SemanticLayer:
    """A validated, immutable collection of semantic model definitions.

    Each collection is a read-only :class:`~types.MappingProxyType` so a loaded
    layer cannot be mutated after construction. Lookup helpers raise a
    deterministic :class:`KeyError` for internal programmer mistakes;
    user-facing catalog and planning code converts lookup failures into domain
    errors.
    """

    version: int
    name: str
    label: str
    description: str
    data_sources: Mapping[str, DataSource]
    dimensions: Mapping[str, Dimension]
    facts: Mapping[str, Fact]
    measures: Mapping[str, Measure]
    metrics: Mapping[str, Metric]
    relationships: Mapping[str, Relationship]

    def source(self, name: str) -> DataSource:
        return self.data_sources[name]

    def dimension(self, name: str) -> Dimension:
        return self.dimensions[name]

    def fact(self, name: str) -> Fact:
        return self.facts[name]

    def measure(self, name: str) -> Measure:
        return self.measures[name]

    def metric(self, name: str) -> Metric:
        return self.metrics[name]

    def relationship(self, name: str) -> Relationship:
        return self.relationships[name]

    @classmethod
    def load(cls, path: str | Path) -> SemanticLayer:
        """Load and validate a schema-version-1 catalog from ``path``.

        Defined as a thin delegate so callers use ``SemanticLayer.load`` while
        the parsing and validation logic lives in
        :mod:`selayer._next.catalog`. The import is local to avoid a module-load
        cycle (``catalog`` imports this model module).
        """
        from selayer._next.catalog import load

        return load(path)


__all__ = [
    "Aggregation",
    "Cardinality",
    "DataSource",
    "Dimension",
    "Fact",
    "Measure",
    "Metric",
    "Relationship",
    "SemanticLayer",
]
