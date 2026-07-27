from __future__ import annotations

from collections import deque
from dataclasses import dataclass

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


@dataclass(slots=True)
class _PathIndex:
    """Shortest safe paths from one anchor, with bounded multiplicity."""

    distances: dict[str, int]
    multiplicities: dict[str, int]
    predecessors: dict[str, tuple[str, str]]

    def path_to(self, goal: str) -> tuple[tuple[str, ...] | None, bool, bool]:
        """Return (path, ambiguous, reachable) for a previously indexed goal."""
        distance = self.distances.get(goal)
        if distance is None:
            return None, False, False
        if self.multiplicities.get(goal, 0) > 1:
            return None, True, True
        if distance == 0:
            return (), False, True

        path: list[str] = []
        node = goal
        while distance:
            previous = self.predecessors.get(node)
            if previous is None:  # pragma: no cover - guarded by the index
                return None, False, True
            node, relationship_id = previous
            path.append(relationship_id)
            distance -= 1
        path.reverse()
        return tuple(path), False, True


def _build_adjacency(
    layer: SemanticLayer,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    """Build safe directed and full undirected adjacency in one sorted pass."""
    safe: dict[str, list[tuple[str, str]]] = {}
    full: dict[str, list[tuple[str, str]]] = {}
    # Relationship IDs are the stable tie-breaker for both traversal and paths.
    for relationship_id, relationship in sorted(
        layer.relationships.items(), key=lambda item: item[0]
    ):
        full.setdefault(relationship.source, []).append(
            (relationship_id, relationship.target)
        )
        full.setdefault(relationship.target, []).append(
            (relationship_id, relationship.source)
        )
        if not expands_rows(relationship, relationship.source):
            safe.setdefault(relationship.source, []).append(
                (relationship_id, relationship.target)
            )
        if not expands_rows(relationship, relationship.target):
            safe.setdefault(relationship.target, []).append(
                (relationship_id, relationship.source)
            )
    return safe, full


def _safe_path_index(
    anchor: str, adjacency: dict[str, list[tuple[str, str]]]
) -> _PathIndex:
    distances = {anchor: 0}
    order: list[str] = []
    queue: deque[str] = deque([anchor])
    while queue:
        node = queue.popleft()
        order.append(node)
        for _, next_node in adjacency.get(node, ()):
            if next_node not in distances:
                distances[next_node] = distances[node] + 1
                queue.append(next_node)

    # Every predecessor is one BFS layer closer to the anchor. Capping counts
    # at two is enough to distinguish unique paths from ambiguous paths.
    multiplicities: dict[str, int] = {anchor: 1}
    predecessors: dict[str, tuple[str, str]] = {}
    for node in order:
        count = multiplicities.get(node, 0)
        if not count:
            continue
        for relationship_id, next_node in adjacency.get(node, ()):
            if distances.get(next_node) != distances[node] + 1:
                continue
            prior = multiplicities.get(next_node, 0)
            if prior == 0:
                predecessors[next_node] = (node, relationship_id)
            multiplicities[next_node] = min(2, prior + count)
    return _PathIndex(distances, multiplicities, predecessors)


def _reachable(anchor: str, adjacency: dict[str, list[tuple[str, str]]]) -> set[str]:
    reachable = {anchor}
    queue: deque[str] = deque([anchor])
    while queue:
        node = queue.popleft()
        for _, next_node in adjacency.get(node, ()):
            if next_node not in reachable:
                reachable.add(next_node)
                queue.append(next_node)
    return reachable


def _shortest_path(
    layer: SemanticLayer, start: str, goal: str, *, safe_only: bool = False
) -> tuple[tuple[str, ...] | None, bool, bool]:
    """Find one shortest path and whether shortest paths are ambiguous.

    This compatibility helper is intentionally a single indexed traversal. The
    planner itself builds both indexes once and reuses them for every goal.
    """
    safe, full = _build_adjacency(layer)
    if safe_only:
        return _safe_path_index(start, safe).path_to(goal)
    return _safe_path_index(start, full).path_to(goal)


def _safe_path(
    layer: SemanticLayer, start: str, goal: str
) -> tuple[tuple[str, ...] | None, bool, bool]:
    return _safe_path_index(start, _build_adjacency(layer)[0]).path_to(goal)


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

    # Construct both graph views once, then index all safe paths from the
    # anchor. Full reachability distinguishes a blocked (row-expanding) path
    # from a genuinely disconnected source without rescanning per goal.
    safe_adjacency, full_adjacency = _build_adjacency(layer)
    safe_index = _safe_path_index(anchor, safe_adjacency)
    full_reachable = _reachable(anchor, full_adjacency)

    joins: list[JoinStep] = []
    joined: set[str] = set()
    for goal in required_sources:
        path, ambiguous, _ = safe_index.path_to(goal)
        if ambiguous:
            raise QueryPlanningError(
                "ambiguous_relationship_path",
                f"multiple shortest relationship paths from '{anchor}' to '{goal}'",
            )
        if path is None:
            if goal in full_reachable:
                raise QueryPlanningError(
                    "row_expanding_path", f"relationship path to '{goal}' expands rows"
                )
            raise QueryPlanningError(
                "no_relationship_path",
                f"no relationship path from '{anchor}' to '{goal}'",
            )
        node = anchor
        for relationship_id in path:
            relationship = layer.relationships[relationship_id]
            if expands_rows(relationship, node):
                raise QueryPlanningError(
                    "row_expanding_path", f"relationship path to '{goal}' expands rows"
                )
            if relationship_id not in joined:
                joins.append(
                    JoinStep(
                        relationship_id,
                        relationship,
                        relationship.source,
                        relationship.target,
                        relationship.source_column,
                        relationship.target_column,
                    )
                )
                joined.add(relationship_id)
            node = (
                relationship.target
                if node == relationship.source
                else relationship.source
            )

    return QueryPlan(
        anchor,
        tuple(joins),
        tuple(dimensions),
        tuple(measures),
        tuple(metrics),
        tuple(filters),
    )


__all__ = ["expands_rows", "plan_query"]
