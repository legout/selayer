"""Immutable request and fully resolved query-plan types."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias, Union

from selayer._next.model import Aggregation
from selayer.expressions import Expression, Scalar


@dataclass(frozen=True, slots=True)
class ScalarFilter:
    """A normalized scalar equality filter."""

    value: Scalar


@dataclass(frozen=True, slots=True, init=False)
class ListFilter:
    """A normalized membership filter, including the empty-list case."""

    values: tuple[Scalar, ...]

    def __init__(self, values: Iterable[Scalar]) -> None:
        object.__setattr__(self, "values", tuple(values))


@dataclass(frozen=True, slots=True)
class RangeFilter:
    """A normalized inclusive two-bound range filter."""

    lower: Scalar
    upper: Scalar


FilterValue: TypeAlias = Union[  # noqa: UP007, UP040
    ScalarFilter, ListFilter, RangeFilter
]
FilterInput: TypeAlias = Union[  # noqa: UP007, UP040
    Scalar, list[Scalar], tuple[Scalar, ...], FilterValue
]


def _normalize_filter(value: FilterInput) -> FilterValue:
    if isinstance(value, (ScalarFilter, ListFilter, RangeFilter)):
        return value
    if isinstance(value, list):
        return ListFilter(tuple(value))
    if isinstance(value, tuple):
        if len(value) == 2:
            return RangeFilter(value[0], value[1])
        return ListFilter(value)
    return ScalarFilter(value)


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """An immutable semantic query request with normalized filter values."""

    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: Mapping[str, FilterInput] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "dimensions", tuple(self.dimensions))
        object.__setattr__(
            self,
            "filters",
            MappingProxyType(
                {name: _normalize_filter(value) for name, value in self.filters.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class JoinStep:
    relationship_id: str
    source: str
    target: str
    source_column: str
    target_column: str


@dataclass(frozen=True, slots=True)
class PlannedDimension:
    id: str
    source: str
    column: str
    data_type: str


@dataclass(frozen=True, slots=True)
class PlannedMeasure:
    id: str
    expression: Expression
    aggregation: Aggregation


@dataclass(frozen=True, slots=True)
class PlannedMetric:
    id: str
    expression: Expression


@dataclass(frozen=True, slots=True)
class PlannedFilter:
    dimension: PlannedDimension
    value: FilterValue


@dataclass(frozen=True, slots=True)
class QueryPlan:
    anchor_source: str
    joins: tuple[JoinStep, ...]
    dimensions: tuple[PlannedDimension, ...]
    measures: tuple[PlannedMeasure, ...]
    metrics: tuple[PlannedMetric, ...]
    filters: tuple[PlannedFilter, ...]


class QueryPlanningError(ValueError):
    """A stable, machine-classifiable planning failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
