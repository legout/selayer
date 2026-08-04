"""Tests for planner-parity compatibility verification.

:func:`verify` with a :class:`CompatibilityCheck` adapts the connection-free
:func:`~selayer.planning.planner.plan_query` into the immutable verification
report model. Every generated request is planned exactly once: a compatible
request records the anchor source and required sources, while a documented
:class:`~selayer.planning.types.QueryPlanningError` records its stable
``code``. Both planner outcomes report ``status="passed"`` because the check
completed; ``status="failed"`` is reserved for invalid selectors.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from selayer import QueryEngine
from selayer.model import SemanticLayer, SemanticStatus
from selayer.planning.planner import plan_query
from selayer.planning.types import QueryRequest
from selayer.verification import CompatibilityCheck, verify
from selayer.verification.compatibility import compatibility_requests

_SHOPFLOOR = "examples/shopfloor/shopfloor_semantic_layer.yaml"


# ---------------------------------------------------------------------------
# Planner parity (brief Step 1)
# ---------------------------------------------------------------------------


def test_compatibility_matches_direct_planner(valid_layer: SemanticLayer) -> None:
    """A metric-dimension outcome mirrors a direct ``plan_query`` result."""
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category",),
        ),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id
        == "compatibility.metric_dimension.gross_margin.product_category"
    )
    plan = plan_query(
        valid_layer,
        QueryRequest(["gross_margin"], ["product_category"]),
    )
    assert outcome.status == "passed"
    assert outcome.evidence["anchor_source"] == plan.anchor_source
    # QueryPlan exposes the sources it touches via ``source_grains``; the
    # compatibility evidence records them comma-joined and sorted.
    assert outcome.evidence["required_sources"] == ",".join(sorted(plan.source_grains))


def test_compatibility_preserves_planner_failure_code(root: Path) -> None:
    """A documented ``mixed_grain`` failure keeps its stable code and passes."""
    layer = SemanticLayer.load(root / _SHOPFLOOR)
    report = verify(
        layer,
        CompatibilityCheck(
            metrics=("average_cycle_seconds", "eol_attempt_pass_rate"),
        ),
    )
    # The two metrics resolve to different anchor sources, so the metric-pair
    # request fails with ``mixed_grain``. Target that outcome directly because
    # the shopfloor layer also produces many unrelated metric-dimension
    # failures that sort earlier by ``check_id``.
    pair = next(
        item
        for item in report.outcomes
        if item.check_id
        == "compatibility.metric_pair.average_cycle_seconds.eol_attempt_pass_rate"
    )
    assert pair.status == "passed"
    assert pair.evidence["compatible"] is False
    assert pair.evidence["planner_code"] == "mixed_grain"
    # The check completed (no invalid selectors), so the report passes.
    assert report.passed


# ---------------------------------------------------------------------------
# Invalid selectors produce coded, declaration-level failed outcomes
# ---------------------------------------------------------------------------


def test_unknown_metric_selector_fails_declaration(valid_layer: SemanticLayer) -> None:
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin", "missing_metric"),
            dimensions=("product_category",),
        ),
    )
    unknown = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.declaration.metric.0000"
    )
    assert unknown.status == "failed"
    assert unknown.scope == "declaration"
    assert unknown.diagnostics[0].code == "unknown_metric"
    assert not report.passed
    # An untrusted top-level selector must never reach the report in any field:
    # check_id, diagnostic path/message, or evidence.
    blob = json.dumps(report.to_dict())
    assert "missing_metric" not in blob


def test_unknown_dimension_selector_fails_declaration(
    valid_layer: SemanticLayer,
) -> None:
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category", "missing_dim"),
        ),
    )
    unknown = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.declaration.dimension.0000"
    )
    assert unknown.status == "failed"
    assert unknown.diagnostics[0].code == "unknown_dimension"
    assert not report.passed
    blob = json.dumps(report.to_dict())
    assert "missing_dim" not in blob


def test_unknown_selector_is_reported_not_omitted(valid_layer: SemanticLayer) -> None:
    """An unknown metric is recorded as a failed declaration, not dropped."""
    report = verify(valid_layer, CompatibilityCheck(metrics=("gross_margin", "ghost")))
    check_ids = {item.check_id for item in report.outcomes}
    # The unknown metric is recorded under an indexed, selector-free check_id.
    assert "compatibility.declaration.metric.0000" in check_ids
    # The valid metric is still planned.
    assert "compatibility.metric.gross_margin" in check_ids
    # The unknown metric is not planned as a request.
    assert "compatibility.metric.ghost" not in check_ids
    # The untrusted selector name never reaches the report.
    assert "ghost" not in json.dumps(report.to_dict())


# ---------------------------------------------------------------------------
# Top-level unknown selectors are secret-safe (no raw name anywhere)
# ---------------------------------------------------------------------------


def test_multiple_unknown_selectors_indexed_and_sorted(
    valid_layer: SemanticLayer,
) -> None:
    """Several unknown selectors get distinct indexed, selector-free check_ids.

    Indices are assigned over the sorted unknown selectors, so the outcome
    set is deterministic (hash-seed independent) regardless of input order,
    and no untrusted selector name reaches any report field.
    """
    forward = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("zebra_unknown", "alpha_unknown", "gross_margin"),
            dimensions=("omega_unknown", "beta_unknown"),
        ),
    )
    reverse = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin", "alpha_unknown", "zebra_unknown"),
            dimensions=("beta_unknown", "omega_unknown"),
        ),
    )
    forward_ids = [item.check_id for item in forward.outcomes]
    reverse_ids = [item.check_id for item in reverse.outcomes]
    assert forward_ids == reverse_ids
    # Distinct unknown metrics/dimensions get distinct indexed check_ids.
    assert "compatibility.declaration.metric.0000" in forward_ids
    assert "compatibility.declaration.metric.0001" in forward_ids
    assert "compatibility.declaration.dimension.0000" in forward_ids
    assert "compatibility.declaration.dimension.0001" in forward_ids
    # The valid metric is still planned.
    assert "compatibility.metric.gross_margin" in forward_ids
    # Outcomes are sorted by (path, check_id); the indexed declarations sort
    # before the planned metric outcome.
    assert forward_ids == sorted(forward_ids)
    # None of the untrusted selector names reach any report field.
    blob = json.dumps(forward.to_dict())
    for name in ("zebra_unknown", "alpha_unknown", "omega_unknown", "beta_unknown"):
        assert name not in blob


def test_unknown_selector_outcome_has_no_raw_name_in_evidence(
    valid_layer: SemanticLayer,
) -> None:
    """The declaration outcome evidence, path, and message carry no raw name."""
    report = verify(
        valid_layer,
        CompatibilityCheck(metrics=("gross_margin", "secret_metric_xyz")),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.declaration.metric.0000"
    )
    assert outcome.status == "failed"
    # No raw selector is echoed in evidence, check_id, or diagnostics.
    assert "secret_metric_xyz" not in outcome.check_id
    assert "secret_metric_xyz" not in outcome.evidence
    assert all(
        "secret_metric_xyz" not in diag.path and "secret_metric_xyz" not in diag.message
        for diag in outcome.diagnostics
    )
    # The evidence still marks the declaration as an incompatible failure.
    assert outcome.evidence["compatible"] is False


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


def test_outcomes_are_sorted_and_reproducible(valid_layer: SemanticLayer) -> None:
    check = CompatibilityCheck(
        metrics=("gross_margin",),
        dimensions=("product_category",),
    )
    first = [item.check_id for item in verify(valid_layer, check).outcomes]
    second = [item.check_id for item in verify(valid_layer, check).outcomes]
    assert first == second
    # All outcomes share the ``compatibility`` path, so they sort by check_id.
    assert first == sorted(first)


# ---------------------------------------------------------------------------
# metric-alone and metric-dimension cases
# ---------------------------------------------------------------------------


def test_metric_alone_outcome_is_compatible(valid_layer: SemanticLayer) -> None:
    report = verify(
        valid_layer,
        CompatibilityCheck(metrics=("gross_margin",), dimensions=("product_category",)),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.metric.gross_margin"
    )
    assert outcome.status == "passed"
    assert outcome.evidence["compatible"] is True
    assert outcome.evidence["anchor_source"] == "order_items"
    # item_cost references products.cost, so products is always required.
    assert outcome.evidence["required_sources"] == "order_items,products"
    assert outcome.evidence["relationship_ids"] == "product_order_items"
    assert outcome.evidence["selected_dimensions"] == ""


def test_metric_dimension_outcome_records_dimension(
    valid_layer: SemanticLayer,
) -> None:
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category",),
        ),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id
        == "compatibility.metric_dimension.gross_margin.product_category"
    )
    assert outcome.evidence["compatible"] is True
    assert outcome.evidence["selected_dimensions"] == "product_category"


# ---------------------------------------------------------------------------
# Unordered metric-pair is deterministically generated
# ---------------------------------------------------------------------------


def test_unordered_metric_pair_request_is_stable(root: Path) -> None:
    layer = SemanticLayer.load(root / _SHOPFLOOR)
    forward = compatibility_requests(
        layer,
        CompatibilityCheck(metrics=("average_cycle_seconds", "eol_attempt_pass_rate")),
    )
    reverse = compatibility_requests(
        layer,
        CompatibilityCheck(metrics=("eol_attempt_pass_rate", "average_cycle_seconds")),
    )
    # Metrics are sorted before pair generation, so input order is irrelevant.
    assert forward == reverse
    pair_ids = [check_id for check_id, _ in forward if ".metric_pair." in check_id]
    assert pair_ids == [
        "compatibility.metric_pair.average_cycle_seconds.eol_attempt_pass_rate"
    ]


# ---------------------------------------------------------------------------
# Explicit multi-dimension QueryRequest cases
# ---------------------------------------------------------------------------


def test_explicit_query_cases_are_indexed_and_planned(
    valid_layer: SemanticLayer,
) -> None:
    cases = (
        QueryRequest(["gross_margin"], ["product_category"]),
        QueryRequest(["gross_margin"]),
    )
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category",),
            query_cases=cases,
        ),
    )
    by_id = {item.check_id: item for item in report.outcomes}
    assert "compatibility.explicit.0000" in by_id
    assert "compatibility.explicit.0001" in by_id
    assert by_id["compatibility.explicit.0000"].evidence["selected_dimensions"] == (
        "product_category"
    )
    assert by_id["compatibility.explicit.0001"].evidence["selected_dimensions"] == ""


def test_explicit_multi_dimension_case_records_planner_failure(
    valid_layer: SemanticLayer,
) -> None:
    # order_date lives on ``orders``, which has no relationship path from the
    # ``order_items`` anchor, so the multi-dimension request fails.
    cases = (QueryRequest(["gross_margin"], ["product_category", "order_date"]),)
    report = verify(
        valid_layer,
        CompatibilityCheck(
            metrics=("gross_margin",),
            dimensions=("product_category",),
            query_cases=cases,
        ),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "passed"
    assert outcome.evidence["compatible"] is False
    assert outcome.evidence["planner_code"] == "no_relationship_path"


# ---------------------------------------------------------------------------
# Review findings: explicit query-case errors never echo selector names
# ---------------------------------------------------------------------------


def test_explicit_case_planner_failure_does_not_echo_source_names(
    valid_layer: SemanticLayer,
) -> None:
    """A planner failure for an explicit case records a fixed safe message.

    ``no_relationship_path`` normally names the anchor and goal sources in its
    message; the adapted outcome must keep only the stable ``planner_code`` and
    a fixed message, never echoing the source names to the report.
    """
    # order_date lives on ``orders`` (no path from the order_items anchor).
    cases = (QueryRequest(["gross_margin"], ["order_date"]),)
    report = verify(
        valid_layer,
        CompatibilityCheck(metrics=("gross_margin",), dimensions=(), query_cases=cases),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "passed"
    assert outcome.evidence["compatible"] is False
    assert outcome.evidence["planner_code"] == "no_relationship_path"
    message = outcome.evidence.get("message", "")
    assert isinstance(message, str)
    # The planner's raw message names the anchor/goal sources; neither reaches
    # the adapted outcome's evidence or diagnostics.
    assert "order_items" not in message
    assert "orders" not in message
    assert all(
        "order_items" not in d.message and "orders" not in d.message
        for d in outcome.diagnostics
    )


def test_explicit_case_unknown_filter_dimension_not_echoed(
    valid_layer: SemanticLayer,
) -> None:
    """An unknown filter dimension is a planner failure whose name is scrubbed.

    The planner reports ``unknown_filter_dimension`` naming the dimension; the
    adapted outcome keeps the stable code but never echoes the dimension name
    (which is user-supplied) to the report.
    """
    cases = (QueryRequest(["gross_margin"], filters={"secret_filter_dim": "x"}),)
    report = verify(
        valid_layer,
        CompatibilityCheck(metrics=("gross_margin",), dimensions=(), query_cases=cases),
    )
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "passed"
    assert outcome.evidence["planner_code"] == "unknown_filter_dimension"
    blob = json.dumps(report.to_dict())
    assert "secret_filter_dim" not in blob


# ---------------------------------------------------------------------------
# Review findings: unknown selectors inside explicit query_cases are
# declaration failures, not passed planner failures
# ---------------------------------------------------------------------------


def test_explicit_case_unknown_metric_is_declaration_failure(
    valid_layer: SemanticLayer,
) -> None:
    cases = (QueryRequest(["gross_margin", "ghost_metric"]),)
    report = verify(valid_layer, CompatibilityCheck(query_cases=cases))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "failed"
    assert outcome.scope == "declaration"
    codes = {d.code for d in outcome.diagnostics}
    assert "unknown_metric" in codes
    # A declaration failure is never a passed planner failure.
    assert "planner_code" not in outcome.evidence
    assert not report.passed
    # The offending selector name must never reach the report.
    blob = json.dumps(report.to_dict())
    assert "ghost_metric" not in blob


def test_explicit_case_unknown_dimension_is_declaration_failure(
    valid_layer: SemanticLayer,
) -> None:
    cases = (QueryRequest(["gross_margin"], ["ghost_dimension"]),)
    report = verify(valid_layer, CompatibilityCheck(query_cases=cases))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "failed"
    assert outcome.scope == "declaration"
    codes = {d.code for d in outcome.diagnostics}
    assert "unknown_dimension" in codes
    assert "planner_code" not in outcome.evidence
    assert not report.passed
    blob = json.dumps(report.to_dict())
    assert "ghost_dimension" not in blob


def test_explicit_case_unknown_selector_outcome_uses_indexed_check_id(
    valid_layer: SemanticLayer,
) -> None:
    """The declaration-failure outcome is keyed by case index, not selector name."""
    cases = (
        QueryRequest(["gross_margin"], ["product_category"]),
        QueryRequest(["gross_margin", "ghost"]),
    )
    report = verify(valid_layer, CompatibilityCheck(query_cases=cases))
    by_id = {item.check_id: item for item in report.outcomes}
    # The valid case still plans; the unknown-selector case is a declaration failure.
    assert by_id["compatibility.explicit.0000"].status == "passed"
    assert by_id["compatibility.explicit.0001"].status == "failed"
    assert "ghost" not in json.dumps(report.to_dict())


def test_metric_alone_explicit_case_with_no_dimensions_is_compatible(
    valid_layer: SemanticLayer,
) -> None:
    """An explicit metric-alone case (no dimensions) plans successfully."""
    cases = (QueryRequest(["gross_margin"]),)
    report = verify(valid_layer, CompatibilityCheck(query_cases=cases))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.explicit.0000"
    )
    assert outcome.status == "passed"
    assert outcome.evidence["compatible"] is True
    assert outcome.evidence["selected_dimensions"] == ""


# ---------------------------------------------------------------------------
# Deprecation reporting (Task 3)
# ---------------------------------------------------------------------------


def _layer_with_deprecated_metric(
    valid_layer: SemanticLayer,
    *,
    deprecated_measure: bool = False,
) -> SemanticLayer:
    """Return ``valid_layer`` with ``gross_margin`` deprecated and a successor.

    ``gross_margin`` is marked deprecated with ``metric.gross_margin_v2`` as its
    replacement; ``gross_margin_v2`` is an active metric with the same measures.
    When ``deprecated_measure`` is set, ``total_item_revenue`` is additionally
    deprecated with an active successor so transitive usage is exercised.
    """
    base = valid_layer.metrics["gross_margin"]
    v2 = replace(base, name="gross_margin_v2")
    metrics = {
        **valid_layer.metrics,
        "gross_margin": replace(
            base,
            status=SemanticStatus.DEPRECATED,
            replaced_by="metric.gross_margin_v2",
        ),
        "gross_margin_v2": v2,
    }
    layer = replace(valid_layer, metrics=metrics)
    if not deprecated_measure:
        return layer
    measure = valid_layer.measures["total_item_revenue"]
    measures = {
        **valid_layer.measures,
        "total_item_revenue": replace(
            measure,
            status=SemanticStatus.DEPRECATED,
            replaced_by="measure.total_item_revenue_v2",
        ),
        "total_item_revenue_v2": replace(measure, name="total_item_revenue_v2"),
    }
    return replace(layer, measures=measures)


def test_deprecated_metric_still_plans_and_executes(
    valid_layer: SemanticLayer,
) -> None:
    """A deprecated metric remains plannable and executable (not removed)."""
    layer = _layer_with_deprecated_metric(valid_layer)

    plan = plan_query(layer, QueryRequest(["gross_margin"]))
    assert plan.anchor_source == "order_items"
    assert tuple(metric.id for metric in plan.metrics) == ("gross_margin",)

    # The deprecated metric still executes against the underlying data.
    with QueryEngine(layer) as engine:
        result = engine.query(["gross_margin"], ["product_category"])
    assert "gross_margin" in result.columns
    assert result.height > 0


def test_compatibility_reports_deprecated_metric_and_replacement(
    valid_layer: SemanticLayer,
) -> None:
    """A compatible outcome lists the deprecated metric and its replacement."""
    layer = _layer_with_deprecated_metric(valid_layer)
    report = verify(layer, CompatibilityCheck(metrics=("gross_margin",)))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.metric.gross_margin"
    )
    assert outcome.evidence["compatible"] is True
    assert outcome.evidence["deprecated_ids"] == "metric.gross_margin"
    assert outcome.evidence["replacements"] == "metric.gross_margin_v2"


def test_compatibility_reports_transitively_used_deprecated_measure(
    valid_layer: SemanticLayer,
) -> None:
    """A transitively used deprecated measure is reported alongside its metric."""
    layer = _layer_with_deprecated_metric(valid_layer, deprecated_measure=True)
    report = verify(layer, CompatibilityCheck(metrics=("gross_margin",)))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.metric.gross_margin"
    )
    deprecated_ids = outcome.evidence["deprecated_ids"]
    assert isinstance(deprecated_ids, str)
    assert "metric.gross_margin" in deprecated_ids
    assert "measure.total_item_revenue" in deprecated_ids
    replacements = outcome.evidence["replacements"]
    assert isinstance(replacements, str)
    assert "measure.total_item_revenue_v2" in replacements


def test_compatibility_outcome_has_no_deprecated_keys_when_clean(
    valid_layer: SemanticLayer,
) -> None:
    """A request touching no deprecated objects omits the deprecated evidence."""
    report = verify(valid_layer, CompatibilityCheck(metrics=("gross_margin",)))
    outcome = next(
        item
        for item in report.outcomes
        if item.check_id == "compatibility.metric.gross_margin"
    )
    assert "deprecated_ids" not in outcome.evidence
    assert "replacements" not in outcome.evidence
