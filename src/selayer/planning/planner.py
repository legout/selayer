"""Pure, deterministic planning for grain-aware semantic queries."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import cast

from selayer.expressions import references
from selayer.model import Relationship, SemanticLayer
from selayer.planning.types import (
    FilterValue,
    JoinStep,
    PlannedDimension,
    PlannedFilter,
    PlannedMeasure,
    PlannedMetric,
    QueryPlan,
    QueryPlanningError,
    QueryRequest,
)


@dataclass(frozen=True, slots=True)
class _Traversal:
    relationship_id: str
    relationship: Relationship
    current_source: str
    next_source: str


def expands_rows(relationship: Relationship, current_source: str) -> bool:
    """Return whether traversal from ``current_source`` can multiply its rows."""
    if relationship.type == "one_to_one":
        return False
    if relationship.type == "one_to_many":
        return current_source == relationship.source
    if relationship.type == "many_to_one":
        return current_source == relationship.target
    return True


def _neighbors(
    layer: SemanticLayer, source: str
) -> tuple[tuple[str, str, Relationship], ...]:
    result: list[tuple[str, str, Relationship]] = []
    for relationship_id, relationship in sorted(layer.relationships.items()):
        if relationship.source == source:
            result.append((relationship_id, relationship.target, relationship))
        elif relationship.target == source:
            result.append((relationship_id, relationship.source, relationship))
    return tuple(result)


def _shortest_path(
    layer: SemanticLayer, anchor: str, target: str
) -> tuple[_Traversal, ...]:
    if anchor == target:
        return ()

    queue: deque[tuple[str, tuple[_Traversal, ...], frozenset[str]]] = deque(
        [(anchor, (), frozenset({anchor}))]
    )
    shortest_length: int | None = None
    paths: list[tuple[_Traversal, ...]] = []

    while queue:
        current, path, visited = queue.popleft()
        if shortest_length is not None and len(path) >= shortest_length:
            continue
        for relationship_id, next_source, relationship in _neighbors(layer, current):
            if next_source in visited:
                continue
            candidate = path + (
                _Traversal(
                    relationship_id=relationship_id,
                    relationship=relationship,
                    current_source=current,
                    next_source=next_source,
                ),
            )
            if next_source == target:
                shortest_length = len(candidate)
                paths.append(candidate)
            else:
                queue.append((next_source, candidate, visited | {next_source}))

    if not paths:
        raise QueryPlanningError(
            "no_relationship_path",
            f"no relationship path from '{anchor}' to '{target}'",
        )
    if len(paths) > 1:
        rendered = sorted(
            " -> ".join(step.relationship_id for step in path) for path in paths
        )
        raise QueryPlanningError(
            "ambiguous_relationship_path",
            f"multiple shortest relationship paths from '{anchor}' to '{target}': "
            + "; ".join(rendered),
        )

    path = paths[0]
    expanding_step = next(
        (step for step in path if expands_rows(step.relationship, step.current_source)),
        None,
    )
    if expanding_step is not None:
        raise QueryPlanningError(
            "row_expanding_path",
            f"relationship path from '{anchor}' to '{target}' expands rows at "
            f"'{expanding_step.relationship_id}'",
        )
    return path


def _planned_dimension(layer: SemanticLayer, dimension_id: str) -> PlannedDimension:
    dimension = layer.dimensions[dimension_id]
    return PlannedDimension(
        id=dimension_id,
        source=dimension.source,
        column=dimension.column,
        data_type=dimension.data_type,
    )


def _validate_identifiers(layer: SemanticLayer, request: QueryRequest) -> None:
    if not request.metrics:
        raise QueryPlanningError("unknown_metric", "at least one metric is required")
    for metric_id in request.metrics:
        if metric_id not in layer.metrics:
            raise QueryPlanningError(
                "unknown_metric", f"metric '{metric_id}' is not known"
            )
    for dimension_id in request.dimensions:
        if dimension_id not in layer.dimensions:
            raise QueryPlanningError(
                "unknown_dimension", f"dimension '{dimension_id}' is not known"
            )
    for dimension_id in sorted(request.filters):
        if dimension_id not in layer.dimensions:
            raise QueryPlanningError(
                "unknown_filter_dimension",
                f"filter dimension '{dimension_id}' is not known",
            )
    duplicate_output_names = sorted(set(request.dimensions) & set(request.metrics))
    if duplicate_output_names:
        name = duplicate_output_names[0]
        raise QueryPlanningError(
            "duplicate_output_name",
            f"requested dimension and metric share output name '{name}'",
        )


def _measure_ids(layer: SemanticLayer, request: QueryRequest) -> tuple[str, ...]:
    result: list[str] = []
    for metric_id in request.metrics:
        for measure_id in layer.metrics[metric_id].measures:
            if measure_id not in result:
                result.append(measure_id)
    return tuple(result)


def _anchor_source(layer: SemanticLayer, measure_ids: tuple[str, ...]) -> str:
    anchors = {layer.facts[layer.measures[item].fact].source for item in measure_ids}
    if len(anchors) != 1:
        rendered = ", ".join(sorted(anchors)) or "none"
        raise QueryPlanningError(
            "mixed_grain", f"requested measures do not share one grain: {rendered}"
        )
    return next(iter(anchors))


def _required_sources(
    layer: SemanticLayer,
    measure_ids: tuple[str, ...],
    dimensions: tuple[PlannedDimension, ...],
    filters: tuple[PlannedFilter, ...],
) -> frozenset[str]:
    result: set[str] = set()
    for measure_id in measure_ids:
        fact = layer.facts[layer.measures[measure_id].fact]
        result.update(
            reference.parts[0]
            for reference in references(fact.expression)
            if len(reference.parts) == 2
        )
    result.update(dimension.source for dimension in dimensions)
    result.update(planned_filter.dimension.source for planned_filter in filters)
    return frozenset(result)


def _joins(
    layer: SemanticLayer, anchor: str, required_sources: frozenset[str]
) -> tuple[JoinStep, ...]:
    result: list[JoinStep] = []
    used_relationships: set[str] = set()
    for source in sorted(required_sources - {anchor}):
        for traversal in _shortest_path(layer, anchor, source):
            if traversal.relationship_id in used_relationships:
                continue
            relationship = traversal.relationship
            result.append(
                JoinStep(
                    relationship_id=traversal.relationship_id,
                    source=relationship.source,
                    target=relationship.target,
                    source_column=relationship.source_column,
                    target_column=relationship.target_column,
                )
            )
            used_relationships.add(traversal.relationship_id)
    return tuple(result)


def plan_query(layer: SemanticLayer, request: QueryRequest) -> QueryPlan:
    """Resolve a semantic request into an immutable, engine-neutral query plan."""
    _validate_identifiers(layer, request)
    measure_ids = _measure_ids(layer, request)
    anchor = _anchor_source(layer, measure_ids)

    dimensions = tuple(
        _planned_dimension(layer, dimension_id) for dimension_id in request.dimensions
    )
    filters = tuple(
        PlannedFilter(
            dimension=_planned_dimension(layer, dimension_id),
            value=cast(FilterValue, request.filters[dimension_id]),
        )
        for dimension_id in sorted(request.filters)
    )
    measures = tuple(
        PlannedMeasure(
            id=measure_id,
            expression=layer.facts[layer.measures[measure_id].fact].expression,
            aggregation=layer.measures[measure_id].aggregation,
        )
        for measure_id in measure_ids
    )
    metrics = tuple(
        PlannedMetric(id=metric_id, expression=layer.metrics[metric_id].expression)
        for metric_id in request.metrics
    )

    return QueryPlan(
        anchor_source=anchor,
        joins=_joins(
            layer,
            anchor,
            _required_sources(layer, measure_ids, dimensions, filters),
        ),
        dimensions=dimensions,
        measures=measures,
        metrics=metrics,
        filters=filters,
    )


__all__ = ["expands_rows", "plan_query"]
