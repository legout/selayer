from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from selayer._next.model import (
    Aggregation,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
)
from selayer.expressions.ast import Expression

type FilterScalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ScalarFilter:
    value: FilterScalar


@dataclass(frozen=True, slots=True)
class ListFilter:
    values: tuple[FilterScalar, ...]


@dataclass(frozen=True, slots=True)
class RangeFilter:
    start: FilterScalar
    end: FilterScalar


type FilterValue = (
    ScalarFilter
    | ListFilter
    | RangeFilter
    | FilterScalar
    | tuple[FilterScalar, ...]
    | list[FilterScalar]
)


@dataclass(frozen=True, slots=True)
class QueryRequest:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: Mapping[str, FilterValue] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class JoinStep:
    relationship_id: str
    relationship: Relationship
    source: str
    target: str
    source_column: str
    target_column: str


@dataclass(frozen=True, slots=True)
class PlannedDimension:
    id: str
    dimension: Dimension


@dataclass(frozen=True, slots=True)
class PlannedMeasure:
    id: str
    measure: Measure
    fact: Fact
    expression: Expression
    aggregation: Aggregation


@dataclass(frozen=True, slots=True)
class PlannedMetric:
    id: str
    metric: Metric
    expression: Expression


@dataclass(frozen=True, slots=True)
class PlannedFilter:
    id: str
    dimension: Dimension
    value: ScalarFilter | ListFilter | RangeFilter


@dataclass(frozen=True, slots=True)
class QueryPlan:
    anchor_source: str
    joins: tuple[JoinStep, ...]
    dimensions: tuple[PlannedDimension, ...]
    measures: tuple[PlannedMeasure, ...]
    metrics: tuple[PlannedMetric, ...]
    filters: tuple[PlannedFilter, ...]


__all__ = [
    "FilterValue",
    "JoinStep",
    "ListFilter",
    "PlannedDimension",
    "PlannedFilter",
    "PlannedMeasure",
    "PlannedMetric",
    "QueryPlan",
    "QueryPlanningError",
    "QueryRequest",
    "RangeFilter",
    "ScalarFilter",
]


class QueryPlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
