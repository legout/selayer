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

from pathlib import Path

from selayer.model import SemanticLayer
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
        if item.check_id == "compatibility.declaration.metric.missing_metric"
    )
    assert unknown.status == "failed"
    assert unknown.scope == "declaration"
    assert unknown.diagnostics[0].code == "unknown_metric"
    assert not report.passed


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
        if item.check_id == "compatibility.declaration.dimension.missing_dim"
    )
    assert unknown.status == "failed"
    assert unknown.diagnostics[0].code == "unknown_dimension"
    assert not report.passed


def test_unknown_selector_is_reported_not_omitted(valid_layer: SemanticLayer) -> None:
    """An unknown metric is recorded as a failed declaration, not dropped."""
    report = verify(valid_layer, CompatibilityCheck(metrics=("gross_margin", "ghost")))
    check_ids = {item.check_id for item in report.outcomes}
    assert "compatibility.declaration.metric.ghost" in check_ids
    # The valid metric is still planned.
    assert "compatibility.metric.gross_margin" in check_ids
    # The unknown metric is not planned as a request.
    assert "compatibility.metric.ghost" not in check_ids


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
