"""Shopfloor-specific knowledge coverage and query-safety policy.

Validates a composed :class:`~selayer.okf.OkfBundle` against curated-section
coverage, required selected-concept overlays, and declarative query-example
planning.  Catalog YAML remains execution authority; this policy is advisory
and never executes overlay content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from selayer import QueryPlanningError, SemanticLayer
from selayer.okf import OkfBundle
from selayer.okf.model import OkfConcept
from selayer.planning.planner import plan_query
from selayer.planning.types import QueryRequest

__all__ = [
    "ShopfloorKnowledgeError",
    "ShopfloorKnowledgeIssue",
    "validate_shopfloor_knowledge",
]


class ShopfloorKnowledgeError(Exception):
    """A shopfloor knowledge document is structurally malformed."""


@dataclass(frozen=True, slots=True, order=True)
class ShopfloorKnowledgeIssue:
    code: str
    path: str
    message: str


_REQUIRED_SECTIONS: dict[str, frozenset[str]] = {
    "metric": frozenset({"Usage Guidance", "Examples", "Caveats", "Related Concepts"}),
    "source": frozenset({"Usage Guidance", "Caveats"}),
    "relationship": frozenset({"Usage Guidance", "Caveats", "Related Concepts"}),
}

_REQUIRED_SELECTED = frozenset(
    {
        "dimension.drive_serial_number",
        "dimension.operation_line_id",
        "dimension.operation_machine_id",
        "dimension.requested_ship_date",
        "dimension.telemetry_line_id",
        "dimension.telemetry_machine_id",
        "dimension.telemetry_recorded_at",
        "fact.alarm_event_machine_id",
        "fact.first_attempt_serial",
        "fact.first_pass_serial",
        "fact.telemetry_event_machine_id",
        "measure.alarm_event_count_measure",
        "measure.component_count_measure",
        "measure.first_attempt_unit_count",
        "measure.first_pass_unit_count",
        "measure.telemetry_event_count",
    }
)

#: Maps a semantic kind prefix to its composed-bundle directory.
_KIND_DIRECTORIES: dict[str, str] = {
    "dimension": "dimensions",
    "fact": "facts",
    "measure": "measures",
    "metric": "metrics",
    "source": "sources",
    "relationship": "relationships",
}

_QUERY_FENCE = re.compile(
    r"```json selayer-query\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_ALLOWED_QUERY_KEYS = frozenset({"metrics", "dimensions", "filters"})

#: Maximum byte length of a parsed query-request body.  A well-formed request
#: is well under 1 KiB; anything larger is almost certainly an attack.
_MAX_QUERY_BODY_BYTES = 4096


def _concept_path(semantic_id: str) -> str:
    """Return the composed-bundle concept path for ``semantic_id``."""
    kind, name = semantic_id.split(".", 1)
    return f"{_KIND_DIRECTORIES[kind]}/{name}"


def _section_text(concept: OkfConcept, title: str) -> str:
    """Return the stripped text of ``concept``'s section ``title``."""
    for section in concept.sections:
        if section.title == title:
            return section.content.strip()
    return ""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Raise on duplicate JSON object keys so ``json.loads`` never merges them."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key '{key}'")
        seen.add(key)
    return dict(pairs)


def _reject_nonstandard_constant(value: str) -> object:
    """Reject NaN/Infinity JSON constants (not part of the strict JSON spec)."""
    raise ValueError(f"non-standard JSON constant '{value}'")


def _parse_query_body(body: str) -> object:
    """Safely parse ``body`` as JSON, converting all failures to ``ValueError``.

    Bounds the body length, rejects duplicate object keys, rejects
    non-standard constants (``NaN``, ``Infinity``), and catches every
    parse/recursion/encoding failure as a deterministic ``ValueError`` so the
    caller can convert it to a safe :class:`ShopfloorKnowledgeError`.
    """
    if len(body) > _MAX_QUERY_BODY_BYTES:
        raise ValueError("query request exceeds maximum size")
    try:
        return json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"not valid JSON: {error.msg}") from error
    except RecursionError as error:
        raise ValueError("query request nesting exceeds recursion limit") from error
    except UnicodeDecodeError as error:
        raise ValueError("query request has invalid encoding") from error


def _query_requests(concept: OkfConcept) -> tuple[QueryRequest, ...]:
    """Parse exactly one safe query request from a metric concept's Examples.

    Raises :class:`ShopfloorKnowledgeError` on any structural defect.
    Never executes Python, SQL, shell, Markdown links, or code blocks.
    """
    examples = _section_text(concept, "Examples")
    matches = tuple(_QUERY_FENCE.finditer(examples))
    if len(matches) != 1:
        raise ShopfloorKnowledgeError("metric example must contain one query request")
    body = matches[0].group("body")
    try:
        payload = _parse_query_body(body)
    except ValueError as error:
        raise ShopfloorKnowledgeError(
            f"metric query request is not valid JSON: {error}"
        ) from error
    if type(payload) is not dict or set(payload) - _ALLOWED_QUERY_KEYS:
        raise ShopfloorKnowledgeError("metric query request has invalid fields")
    metrics = payload.get("metrics")
    dimensions = payload.get("dimensions", [])
    filters = payload.get("filters", {})
    if (
        type(metrics) is not list
        or not metrics
        or any(type(item) is not str for item in metrics)
        or type(dimensions) is not list
        or any(type(item) is not str for item in dimensions)
        or type(filters) is not dict
        or any(type(key) is not str for key in filters)
    ):
        raise ShopfloorKnowledgeError("metric query request has invalid values")
    return (QueryRequest(metrics, dimensions, filters),)


def _check_required_kind_sections(
    bundle: OkfBundle,
    layer: SemanticLayer,
    issues: list[ShopfloorKnowledgeIssue],
) -> None:
    """Verify required curated sections are present and non-empty per kind."""
    for concept_id in sorted(bundle.concepts):
        concept = bundle.concepts[concept_id]
        selayer_id = concept.frontmatter.get("selayer_id")
        if not isinstance(selayer_id, str):
            continue
        kind = selayer_id.split(".", 1)[0]
        required = _REQUIRED_SECTIONS.get(kind)
        if required is None:
            continue
        present = {section.title for section in concept.sections}
        for section_title in sorted(required):
            if section_title not in present or not _section_text(
                concept, section_title
            ):
                issues.append(
                    ShopfloorKnowledgeIssue(
                        code="shopfloor.section.missing",
                        path=concept_id,
                        message=f"required section '{section_title}' is missing or empty",
                    )
                )


def _check_required_selected_concepts(
    bundle: OkfBundle,
    issues: list[ShopfloorKnowledgeIssue],
) -> None:
    """Verify every required selected concept has a non-empty overlay."""
    for semantic_id in sorted(_REQUIRED_SELECTED):
        path = _concept_path(semantic_id)
        concept = bundle.concepts.get(path)
        if concept is None:
            issues.append(
                ShopfloorKnowledgeIssue(
                    code="shopfloor.overlay.missing",
                    path=path,
                    message=f"required concept '{semantic_id}' is absent from the bundle",
                )
            )
            continue
        has_overlay = any(
            section.title != "Catalog Definition" and section.content.strip()
            for section in concept.sections
        )
        if not has_overlay:
            issues.append(
                ShopfloorKnowledgeIssue(
                    code="shopfloor.overlay.missing",
                    path=path,
                    message=f"required concept '{semantic_id}' has no curated overlay",
                )
            )


def _check_metric_queries(
    bundle: OkfBundle,
    layer: SemanticLayer,
    issues: list[ShopfloorKnowledgeIssue],
) -> None:
    """Verify every catalog metric has exactly one plannable query request.

    Iterates ``layer.metrics`` (catalog authority), not ``bundle.concepts``,
    so a missing generated/overlay metric concept produces a deterministic
    issue rather than being silently skipped.
    """
    for metric_name in sorted(layer.metrics):
        concept_id = f"metrics/{metric_name}"
        concept = bundle.concepts.get(concept_id)
        if concept is None:
            issues.append(
                ShopfloorKnowledgeIssue(
                    code="shopfloor.metric.missing",
                    path=concept_id,
                    message=f"metric '{metric_name}' has no composed concept",
                )
            )
            continue
        try:
            requests = _query_requests(concept)
        except ShopfloorKnowledgeError as error:
            issues.append(
                ShopfloorKnowledgeIssue(
                    code="shopfloor.query.invalid",
                    path=concept_id,
                    message=str(error),
                )
            )
            continue
        for request in requests:
            if request.metrics != (metric_name,):
                issues.append(
                    ShopfloorKnowledgeIssue(
                        code="shopfloor.query.invalid",
                        path=concept_id,
                        message=(
                            f"metric query request must name only '{metric_name}'"
                        ),
                    )
                )
                continue
            if request.filters != {}:
                issues.append(
                    ShopfloorKnowledgeIssue(
                        code="shopfloor.query.invalid",
                        path=concept_id,
                        message="metric query request must have empty filters",
                    )
                )
                continue
            try:
                plan_query(layer, request)
            except QueryPlanningError as error:
                issues.append(
                    ShopfloorKnowledgeIssue(
                        code="shopfloor.example.unplannable",
                        path=concept_id,
                        message=(f"metric query request does not plan: {error.code}"),
                    )
                )


def validate_shopfloor_knowledge(
    bundle: OkfBundle,
    layer: SemanticLayer,
) -> tuple[ShopfloorKnowledgeIssue, ...]:
    """Validate a composed shopfloor OKF bundle against coverage policy.

    Returns a deterministic, sorted tuple of issues.  An empty tuple means the
    bundle satisfies all shopfloor knowledge requirements.
    """
    issues: list[ShopfloorKnowledgeIssue] = []
    _check_required_kind_sections(bundle, layer, issues)
    _check_required_selected_concepts(bundle, issues)
    _check_metric_queries(bundle, layer, issues)
    return tuple(sorted(issues))
