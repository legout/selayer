from __future__ import annotations

from collections import deque

from selayer.expressions.validation import references
from selayer.model import Relationship, SemanticLayer
from selayer.planning.types import (
    JoinStep,
    PlannedDimension,
    PlannedFilter,
    PlannedMeasure,
    PlannedMetric,
    QueryPlan,
    QueryPlanningError,
    QueryRequest,
)


def expands_rows(relationship: Relationship, current_source: str) -> bool:
    if relationship.type == "one_to_one":
        return False
    if relationship.type == "one_to_many":
        return current_source == relationship.source
    if relationship.type == "many_to_one":
        return current_source == relationship.target
    return True


def _shortest_path(
    layer: SemanticLayer, start: str, goal: str, *, safe_only: bool = False
) -> tuple[tuple[str, ...] | None, bool, bool]:
    """Find one shortest path and whether shortest paths are ambiguous.

    Distances and capped path multiplicities are computed over the shortest-path
    DAG.  This deliberately never materializes path combinations: the caller
    only needs one path when it is unique, or an ambiguity bit otherwise.
    """
    if start == goal:
        return (), False, True
    edges: dict[str, list[tuple[str, str]]] = {}
    for rid, rel in sorted(layer.relationships.items()):
        if safe_only:
            if not expands_rows(rel, rel.source):
                edges.setdefault(rel.source, []).append((rid, rel.target))
            if not expands_rows(rel, rel.target):
                edges.setdefault(rel.target, []).append((rid, rel.source))
        else:
            edges.setdefault(rel.source, []).append((rid, rel.target))
            edges.setdefault(rel.target, []).append((rid, rel.source))
    distances = {start: 0}
    order = [start]
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for _, nxt in edges.get(node, ()):
            if nxt not in distances:
                distances[nxt] = distances[node] + 1
                order.append(nxt)
                queue.append(nxt)
    if goal not in distances:
        return None, False, False

    shortest_length = distances[goal]
    # BFS discovery order is already layer ordered.
    layers = order
    multiplicity: dict[str, int] = {start: 1}
    for node in layers:
        if distances[node] >= shortest_length:
            continue
        count = multiplicity.get(node, 0)
        if not count:
            continue
        for _, nxt in edges.get(node, ()):
            if distances.get(nxt) != distances[node] + 1:
                continue
            multiplicity[nxt] = min(2, multiplicity.get(nxt, 0) + count)
    if multiplicity.get(goal, 0) > 1:
        return None, True, True

    # With exactly one path, walk the shortest-path DAG in relationship-ID
    # order.  Reverse counts identify which edges can reach the goal without
    # constructing any alternative path.
    suffix: dict[str, int] = {goal: 1}
    for node in reversed(layers):
        if node == goal or distances[node] >= shortest_length:
            continue
        for _, nxt in edges.get(node, ()):
            if distances.get(nxt) != distances[node] + 1:
                continue
            suffix[node] = min(2, suffix.get(node, 0) + suffix.get(nxt, 0))
    path: list[str] = []
    node = start
    while node != goal:
        for rid, nxt in edges.get(node, ()):
            if distances.get(nxt) == distances[node] + 1 and suffix.get(nxt, 0):
                path.append(rid)
                node = nxt
                break
        else:  # pragma: no cover - guarded by the unique-path count
            return None, False, True
    return tuple(path), False, True


def _safe_path(
    layer: SemanticLayer, start: str, goal: str
) -> tuple[tuple[str, ...] | None, bool, bool]:
    return _shortest_path(layer, start, goal, safe_only=True)


def plan_query(layer: SemanticLayer, request: QueryRequest) -> QueryPlan:
    if not request.metrics:
        raise QueryPlanningError("unknown_metric", "at least one metric is required")
    for metric_id in sorted(set(request.metrics)):
        if metric_id not in layer.metrics:
            raise QueryPlanningError(
                "unknown_metric", f"metric '{metric_id}' is not known"
            )
    metrics = [
        PlannedMetric(
            metric_id, layer.metrics[metric_id], layer.metrics[metric_id].expression
        )
        for metric_id in request.metrics
    ]

    measure_ids: list[str] = []
    for metric in metrics:
        for measure_id in metric.metric.measures:
            if measure_id not in measure_ids:
                measure_ids.append(measure_id)
    measures: list[PlannedMeasure] = []
    for measure_id in measure_ids:
        measure = layer.measures.get(measure_id)
        if measure is None:
            raise QueryPlanningError(
                "unknown_metric", f"metric measure '{measure_id}' is not known"
            )
        fact = layer.facts[measure.fact]
        measures.append(
            PlannedMeasure(
                measure_id, measure, fact, fact.expression, measure.aggregation
            )
        )
    if not measures:
        raise QueryPlanningError("mixed_grain", "requested metrics contain no measures")
    anchor = measures[0].fact.source
    sources = {item.fact.source for item in measures}
    if len(sources) != 1:
        raise QueryPlanningError(
            "mixed_grain", "requested measures do not share one anchor source"
        )
    grain = layer.data_sources[anchor].grain
    if any(layer.data_sources[item.fact.source].grain != grain for item in measures):
        raise QueryPlanningError(
            "mixed_grain", "requested measures do not share one source grain"
        )

    for dimension_id in sorted(set(request.dimensions)):
        if dimension_id not in layer.dimensions:
            raise QueryPlanningError(
                "unknown_dimension", f"dimension '{dimension_id}' is not known"
            )
    dimensions: list[PlannedDimension] = []
    required_sources: list[str] = []
    for dimension_id in request.dimensions:
        dimension = layer.dimensions[dimension_id]
        dimensions.append(PlannedDimension(dimension_id, dimension))
        if dimension.source not in required_sources:
            required_sources.append(dimension.source)
    filters: list[PlannedFilter] = []
    for dimension_id in sorted(request.filters):
        raw_value = request.filters[dimension_id]
        dimension = layer.dimensions.get(dimension_id)
        if dimension is None:
            raise QueryPlanningError(
                "unknown_filter_dimension",
                f"filter dimension '{dimension_id}' is not known",
            )
        filters.append(PlannedFilter(dimension_id, dimension, raw_value))
        if dimension.source not in required_sources:
            required_sources.append(dimension.source)
    for planned in measures:
        for reference in references(planned.fact.expression):
            source = reference.parts[0]
            if source != anchor and source not in required_sources:
                required_sources.append(source)

    joins: list[JoinStep] = []
    joined: set[str] = set()
    for goal in required_sources:
        path, ambiguous, _ = _safe_path(layer, anchor, goal)
        if ambiguous:
            raise QueryPlanningError(
                "ambiguous_relationship_path",
                f"multiple shortest relationship paths from '{anchor}' to '{goal}'",
            )
        if path is None:
            _, _, fallback_reachable = _shortest_path(layer, anchor, goal)
            if fallback_reachable:
                raise QueryPlanningError(
                    "row_expanding_path", f"relationship path to '{goal}' expands rows"
                )
            raise QueryPlanningError(
                "no_relationship_path",
                f"no relationship path from '{anchor}' to '{goal}'",
            )
        node = anchor
        for rid in path:
            rel = layer.relationships[rid]
            if expands_rows(rel, node):
                raise QueryPlanningError(
                    "row_expanding_path", f"relationship path to '{goal}' expands rows"
                )
            if rid not in joined:
                joins.append(
                    JoinStep(
                        rid,
                        rel,
                        rel.source,
                        rel.target,
                        rel.source_column,
                        rel.target_column,
                    )
                )
                joined.add(rid)
            node = rel.target if node == rel.source else rel.source

    return QueryPlan(
        anchor,
        tuple(joins),
        tuple(dimensions),
        tuple(measures),
        tuple(metrics),
        tuple(filters),
    )


__all__ = ["expands_rows", "plan_query"]
