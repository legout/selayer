from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


type FilterValue = ScalarFilter | ListFilter | RangeFilter
type FilterInput = FilterValue | FilterScalar | Sequence[FilterScalar]


@dataclass(frozen=True, slots=True, init=False)
class QueryRequest:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: Mapping[str, FilterValue]

    def __init__(
        self,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Mapping[str, FilterInput] | None = None,
    ) -> None:
        normalized: dict[str, FilterValue] = {}
        for key, value in (filters or {}).items():
            if isinstance(value, (ScalarFilter, ListFilter, RangeFilter)):
                normalized[key] = value
            elif isinstance(value, list):
                normalized[key] = ListFilter(tuple(value))
            elif isinstance(value, tuple):
                normalized[key] = (
                    RangeFilter(value[0], value[1])
                    if len(value) == 2
                    else ListFilter(tuple(value))
                )
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                normalized[key] = ListFilter(tuple(value))
            else:
                normalized[key] = ScalarFilter(value)  # type: ignore[arg-type]
        object.__setattr__(self, "metrics", tuple(metrics))
        object.__setattr__(self, "dimensions", tuple(dimensions))
        object.__setattr__(self, "filters", MappingProxyType(normalized))


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
    "FilterInput",
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
