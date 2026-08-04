"""Planner-parity compatibility verification.

Adapts the connection-free :func:`~selayer.planning.planner.plan_query` into
the immutable verification report model. For every generated
:class:`~selayer.planning.types.QueryRequest`, the planner is invoked exactly
once: a compatible request records the anchor source, required sources,
relationship ids, and selected dimensions; a documented
:class:`~selayer.planning.types.QueryPlanningError` records its stable
``code`` and a safe message. Both planner outcomes report ``status="passed"``
because the verification check completed; ``status="failed"`` is reserved for
invalid selectors caught before request generation.

The planner is purely declarative and credential-free, so compatibility
verification never initialises an execution engine, an adapter, or DuckDB, and
never touches a secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from selayer.model import SemanticLayer, SemanticStatus
from selayer.planning.planner import plan_query
from selayer.planning.types import QueryPlan, QueryPlanningError, QueryRequest
from selayer.verification.model import (
    CompatibilityCheck,
    VerificationDiagnostic,
    VerificationOutcome,
    VerificationReport,
)

__all__ = ["compatibility_requests", "verify_compatibility"]

#: Evidence ``path`` shared by every compatibility outcome. All outcomes share
#: this path so the report orders them deterministically by ``check_id``.
_PATH = "compatibility"

#: Fixed, secret-safe messages keyed by the planner's stable error ``code``.
#: The planner's own messages name requested selectors, sources, and filter
#: dimensions; an explicit query case is user-supplied, so those names (and any
#: filter values) must never be echoed in a report. Each documented planner
#: failure is therefore adapted to the fixed message below while the stable
#: ``code`` is preserved as ``planner_code`` evidence.
_PLANNER_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "unknown_metric": "request references an unknown metric",
        "unknown_dimension": "request references an unknown dimension",
        "unknown_filter_dimension": "request filters an unknown dimension",
        "duplicate_output_name": (
            "request reuses an output name across metrics and dimensions"
        ),
        "mixed_grain": "request metrics span incompatible grains",
        "no_relationship_path": "required sources have no relationship path",
        "row_expanding_path": "a required relationship path would expand rows",
        "ambiguous_relationship_path": (
            "required sources have multiple relationship paths"
        ),
        "invalid_filter_type": ("a filter value does not match its dimension type"),
    }
)

#: Fallback for any future planner code: fixed, identifier-free, secret-safe.
_PLANNER_FALLBACK_MESSAGE = "request is not compatible with the semantic layer"


def _build_requests(
    metrics: tuple[str, ...],
    dimensions: tuple[str, ...],
    query_cases: tuple[QueryRequest, ...],
) -> tuple[tuple[str, QueryRequest], ...]:
    """Deterministically build ``(check_id, QueryRequest)`` pairs.

    Metric-alone, metric-dimension, and unordered metric-pair requests are
    generated from already-sorted selector tuples, then explicit query cases
    are appended with a zero-padded index. The selector tuples are sorted by
    the caller, so the generated set is independent of the input order.
    """
    requests: list[tuple[str, QueryRequest]] = []
    for metric in metrics:
        requests.append((f"compatibility.metric.{metric}", QueryRequest([metric])))
        for dimension in dimensions:
            requests.append(
                (
                    f"compatibility.metric_dimension.{metric}.{dimension}",
                    QueryRequest([metric], [dimension]),
                )
            )
    for index, left in enumerate(metrics):
        for right in metrics[index + 1 :]:
            requests.append(
                (
                    f"compatibility.metric_pair.{left}.{right}",
                    QueryRequest([left, right]),
                )
            )
    for index, request in enumerate(query_cases):
        requests.append((f"compatibility.explicit.{index:04d}", request))
    return tuple(requests)


def compatibility_requests(
    layer: SemanticLayer,
    check: CompatibilityCheck,
) -> tuple[tuple[str, QueryRequest], ...]:
    """Return the deterministic request set for ``check`` against ``layer``.

    When ``check.metrics``/``check.dimensions`` are unset (or empty) every
    declared metric/dimension is used; otherwise only the named selectors are
    used. Selectors are sorted before generation, so the request set is
    reproducible regardless of the supplied order.
    """
    metrics = tuple(sorted(check.metrics or layer.metrics))
    dimensions = tuple(sorted(check.dimensions or layer.dimensions))
    return _build_requests(metrics, dimensions, check.query_cases)


def _unknown_selector_outcome(kind: str, index: int) -> VerificationOutcome:
    """A coded, declaration-level failed outcome for an unknown selector.

    Top-level ``--metric``/``--dimension`` values are user-supplied and
    therefore untrusted; an unknown selector's name must never reach the
    report. The outcome is keyed by a zero-padded index over the *sorted*
    unknown selectors (deterministic and selector-free), the diagnostic uses a
    fixed path and a fixed message that names only the selector ``kind``
    (``"metric"``/``"dimension"`` — a fixed value, not user-controlled), and
    the evidence carries no raw name. This mirrors the secret-safe handling
    already applied to unknown selectors inside explicit query cases.
    """
    code = "unknown_metric" if kind == "metric" else "unknown_dimension"
    check_id = f"compatibility.declaration.{kind}.{index:04d}"
    diagnostic = VerificationDiagnostic(
        code,
        "error",
        check_id,
        f"an unknown {kind} was requested",
    )
    return VerificationOutcome(
        check_id=check_id,
        status="failed",
        scope="declaration",
        path=_PATH,
        evidence={"compatible": False},
        diagnostics=(diagnostic,),
    )


def _deprecated_evidence(layer: SemanticLayer, plan: QueryPlan) -> dict[str, str]:
    """Return deprecation evidence for a compatible plan, or an empty dict.

    Collects every deprecated semantic object touched by the plan — directly
    requested deprecated metrics and dimensions, plus transitively used
    deprecated measures, facts, sources, and relationships — and returns
    comma-joined sorted ``deprecated_ids`` and ``replacements``. Returns an
    empty dict when no deprecated object is touched so clean requests keep
    their existing evidence shape unchanged.
    """
    deprecated: dict[str, str] = {}  # semantic_id -> replaced_by
    for planned_metric in plan.metrics:
        metric = planned_metric.metric
        if metric.status == SemanticStatus.DEPRECATED:
            deprecated[f"metric.{planned_metric.id}"] = metric.replaced_by or ""
    for planned_dimension in plan.dimensions:
        dimension = planned_dimension.dimension
        if dimension.status == SemanticStatus.DEPRECATED:
            deprecated[f"dimension.{planned_dimension.id}"] = (
                dimension.replaced_by or ""
            )
    for planned_measure in plan.measures:
        measure = planned_measure.measure
        if measure.status == SemanticStatus.DEPRECATED:
            deprecated[f"measure.{planned_measure.id}"] = measure.replaced_by or ""
        fact = planned_measure.fact
        if fact.status == SemanticStatus.DEPRECATED:
            deprecated[f"fact.{fact.name}"] = fact.replaced_by or ""
    for source_name in plan.source_grains:
        source = layer.data_sources.get(source_name)
        if source is not None and source.status == SemanticStatus.DEPRECATED:
            deprecated[f"source.{source_name}"] = source.replaced_by or ""
    for join in plan.joins:
        relationship = layer.relationships.get(join.relationship_id)
        if (
            relationship is not None
            and relationship.status == SemanticStatus.DEPRECATED
        ):
            deprecated[f"relationship.{join.relationship_id}"] = (
                relationship.replaced_by or ""
            )
    if not deprecated:
        return {}
    replacements = ",".join(sorted(value for value in deprecated.values() if value))
    return {
        "deprecated_ids": ",".join(sorted(deprecated)),
        "replacements": replacements,
    }


def _plan_outcome(
    layer: SemanticLayer, check_id: str, request: QueryRequest
) -> VerificationOutcome:
    """Plan one request and adapt the result into an immutable outcome.

    A documented :class:`QueryPlanningError` is not a verifier failure: the
    check completed and recorded a stable incompatibility code, so the outcome
    is ``passed`` with ``compatible`` set to ``False``. The planner's own
    message names requested selectors, sources, and filter dimensions, so it is
    never recorded verbatim: only the fixed, secret-safe message mapped from
    the stable ``code`` is, while ``planner_code`` preserves the evidence.
    """
    try:
        plan = plan_query(layer, request)
    except QueryPlanningError as error:
        return VerificationOutcome(
            check_id=check_id,
            status="passed",
            scope="planner",
            path=_PATH,
            evidence={
                "compatible": False,
                "planner_code": error.code,
                "message": _PLANNER_MESSAGES.get(error.code, _PLANNER_FALLBACK_MESSAGE),
            },
            diagnostics=(),
        )
    return VerificationOutcome(
        check_id=check_id,
        status="passed",
        scope="planner",
        path=_PATH,
        evidence={
            "compatible": True,
            "anchor_source": plan.anchor_source,
            # ``QueryPlan`` exposes the set of sources it touches via
            # ``source_grains`` (anchor plus every join endpoint); the evidence
            # records them comma-joined and sorted for deterministic output.
            "required_sources": ",".join(sorted(plan.source_grains)),
            "relationship_ids": ",".join(step.relationship_id for step in plan.joins),
            "selected_dimensions": ",".join(
                sorted(dimension.id for dimension in plan.dimensions)
            ),
            **_deprecated_evidence(layer, plan),
        },
        diagnostics=(),
    )


def _explicit_case_outcome(
    layer: SemanticLayer, check_id: str, request: QueryRequest
) -> VerificationOutcome:
    """Validate then plan one explicit (user-supplied) query case.

    An unknown metric or dimension inside a query case is a declaration-level
    failure, not a passed planner failure: the case is rejected with a coded
    ``failed`` outcome whose diagnostic message is fixed and never echoes the
    offending selector (which may be user-supplied). Only cases whose metric
    and dimension selectors are all known reach the planner, and any planner
    failure is adapted to a fixed secret-safe message (see
    ``_PLANNER_MESSAGES``). The indexed ``check_id`` carries no selector name.
    """
    kinds: list[str] = []
    if any(metric not in layer.metrics for metric in request.metrics):
        kinds.append("metric")
    if any(dimension not in layer.dimensions for dimension in request.dimensions):
        kinds.append("dimension")
    if kinds:
        diagnostics = tuple(
            VerificationDiagnostic(
                "unknown_metric" if kind == "metric" else "unknown_dimension",
                "error",
                check_id,
                f"explicit query case references an unknown {kind}",
            )
            for kind in kinds
        )
        return VerificationOutcome(
            check_id=check_id,
            status="failed",
            scope="declaration",
            path=_PATH,
            evidence={"compatible": False},
            diagnostics=diagnostics,
        )
    return _plan_outcome(layer, check_id, request)


def verify_compatibility(
    layer: SemanticLayer, check: CompatibilityCheck
) -> VerificationReport:
    """Run planner-parity compatibility verification and return its report.

    Selectors are validated before request generation: an unknown requested
    metric or dimension produces a coded, declaration-level ``failed``
    outcome rather than being silently omitted. Top-level selector values are
    user-supplied and untrusted, so the outcome is keyed by a deterministic
    index over the sorted unknown selectors and never echoes the raw name in
    its ``check_id``, diagnostic path/message, or evidence. Valid selectors
    drive the deterministic request set, each planned once. Each explicit
    ``query_case`` is validated the same way before planning: an unknown
    metric or dimension inside a case produces a coded, declaration-level
    ``failed`` outcome (never echoing the offending selector) rather than a
    passed planner failure.
    """
    requested_metrics = (
        sorted(check.metrics) if check.metrics else sorted(layer.metrics)
    )
    requested_dimensions = (
        sorted(check.dimensions) if check.dimensions else sorted(layer.dimensions)
    )

    outcomes: list[VerificationOutcome] = []
    diagnostics: list[VerificationDiagnostic] = []

    valid_metrics: list[str] = []
    unknown_metric_index = 0
    for metric in requested_metrics:
        if metric in layer.metrics:
            valid_metrics.append(metric)
            continue
        # ``requested_metrics`` is sorted, so the index assigned over the
        # unknown selectors is deterministic (hash-seed independent) without
        # ever echoing the untrusted selector name.
        outcome = _unknown_selector_outcome("metric", unknown_metric_index)
        unknown_metric_index += 1
        outcomes.append(outcome)
        diagnostics.extend(outcome.diagnostics)

    valid_dimensions: list[str] = []
    unknown_dimension_index = 0
    for dimension in requested_dimensions:
        if dimension in layer.dimensions:
            valid_dimensions.append(dimension)
            continue
        outcome = _unknown_selector_outcome("dimension", unknown_dimension_index)
        unknown_dimension_index += 1
        outcomes.append(outcome)
        diagnostics.extend(outcome.diagnostics)

    requests = _build_requests(tuple(valid_metrics), tuple(valid_dimensions), ())
    for check_id, request in requests:
        outcomes.append(_plan_outcome(layer, check_id, request))
    for index, request in enumerate(check.query_cases):
        check_id = f"compatibility.explicit.{index:04d}"
        outcome = _explicit_case_outcome(layer, check_id, request)
        outcomes.append(outcome)
        diagnostics.extend(outcome.diagnostics)

    return VerificationReport(
        1,
        layer.name,
        "compatibility",
        True,
        tuple(outcomes),
        tuple(diagnostics),
    )
