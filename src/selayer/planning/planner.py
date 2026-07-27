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


def _paths(
    layer: SemanticLayer, start: str, goal: str, *, safe_only: bool = False
) -> list[tuple[str, ...]]:
    """Return all deterministic shortest paths in the requested graph.

    The fallback graph is undirected so callers can distinguish a genuine
    disconnect from a path that would expand the anchor grain.  Safe paths,
    in contrast, are directed by cardinality and only retain grain-preserving
    traversals.
    """
    if start == goal:
        return [()]
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
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for _, nxt in edges.get(node, ()):
            if nxt not in distances:
                distances[nxt] = distances[node] + 1
                queue.append(nxt)
    if goal not in distances:
        return []
    length = distances[goal]
    result: list[tuple[str, ...]] = []

    def walk(node: str, path: tuple[str, ...], seen: frozenset[str]) -> None:
        if len(path) == length:
            if node == goal:
                result.append(path)
            return
        for rid, nxt in edges.get(node, ()):
            if rid in seen or distances.get(nxt) != distances[node] + 1:
                continue
            walk(nxt, path + (rid,), seen | {rid})

    walk(start, (), frozenset())
    return result


def _safe_paths(layer: SemanticLayer, start: str, goal: str) -> list[tuple[str, ...]]:
    return _paths(layer, start, goal, safe_only=True)


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
        paths = _safe_paths(layer, anchor, goal)
        if not paths:
            if _paths(layer, anchor, goal):
                raise QueryPlanningError(
                    "row_expanding_path", f"relationship path to '{goal}' expands rows"
                )
            raise QueryPlanningError(
                "no_relationship_path",
                f"no relationship path from '{anchor}' to '{goal}'",
            )
        if len(paths) > 1:
            raise QueryPlanningError(
                "ambiguous_relationship_path",
                f"multiple shortest relationship paths from '{anchor}' to '{goal}'",
            )
        node = anchor
        for rid in paths[0]:
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
