"""Strict YAML loader for the schema-version-1 grain-aware catalog.

``SemanticLayer.load`` parses a catalog file into the immutable
:data:`~selayer._next.model.SemanticLayer` model. It either returns a fully
valid immutable layer or raises a single :class:`CatalogValidationError`
containing every independent issue, sorted by ``(path, message)``. It never
exposes a partially valid model.

Validation pipeline:

1. parse YAML safely (and detect duplicate mapping keys via the node tree);
2. validate required top-level and object fields;
3. validate identifier syntax with ``[a-z][a-z0-9_]*``;
4. parse every fact and metric expression;
5. resolve all references (sources, facts, measures);
6. validate relationship endpoints/cardinalities;
7. collect all independent issues;
8. sort by ``(path, message)``;
9. raise one error when any issue exists;
10. return only immutable objects otherwise.

Same-grain and relationship reachability are deferred to the planner task.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args

import yaml

from selayer._next.model import (
    Aggregation,
    Cardinality,
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
)
from selayer.expressions import ExpressionSyntaxError, parse_expression
from selayer.expressions.ast import Expression
from selayer.expressions.validation import (
    validate_metric_expression,
    validate_row_expression,
)

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_AGGREGATIONS: frozenset[str] = frozenset(
    get_args(getattr(Aggregation, "__value__", Aggregation))
)
_CARDINALITIES: frozenset[str] = frozenset(
    get_args(getattr(Cardinality, "__value__", Cardinality))
)


@dataclass(frozen=True, slots=True)
class CatalogIssue:
    """A single catalog validation problem located at a field path."""

    path: str
    message: str


class CatalogValidationError(ValueError):
    """Raised when a catalog has one or more validation issues.

    ``issues`` is a tuple sorted by ``(path, message)`` so error output is
    deterministic regardless of mapping iteration order.
    """

    def __init__(self, issues: tuple[CatalogIssue, ...]) -> None:
        self.issues = issues
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.issues:
            return "catalog validation failed"
        lines = [f"{len(self.issues)} catalog validation issue(s):"]
        lines.extend(f"  {issue.path}: {issue.message}" for issue in self.issues)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Collector:
    issues: list[CatalogIssue]

    def add(self, path: str, message: str) -> None:
        self.issues.append(CatalogIssue(path=path, message=message))

    def raise_if_any(self) -> None:
        if not self.issues:
            return
        issues = tuple(
            sorted(self.issues, key=lambda issue: (issue.path, issue.message))
        )
        raise CatalogValidationError(issues)


def _is_valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _require(
    raw: Mapping[str, Any], field: str, base_path: str, collector: _Collector
) -> None:
    if raw.get(field) is None:
        collector.add(f"{base_path}.{field}", f"{field} is required")


_COLLECTIONS = (
    "data_sources",
    "dimensions",
    "facts",
    "measures",
    "metrics",
    "relationships",
)


def _collection(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _check_string(value: object, path: str, field: str, collector: _Collector) -> None:
    if not isinstance(value, str):
        collector.add(path, f"{field} must be a string")


def _check_optional_string(
    raw: Mapping[str, Any], field: str, path: str, collector: _Collector
) -> None:
    if field in raw:
        field_path = f"{path}.{field}" if path else field
        _check_string(raw[field], field_path, field, collector)


def _validate_identifier_key(
    key: object, base_path: str, kind: str, collector: _Collector
) -> None:
    if not _is_valid_identifier(key):
        collector.add(base_path, f"{kind} key '{key}' must match [a-z][a-z0-9_]*")


# ---------------------------------------------------------------------------
# YAML parsing and duplicate-key detection
# ---------------------------------------------------------------------------


def _compose_and_construct(
    text: str,
) -> tuple[yaml.Node | None, object]:
    """Compose the YAML node tree and construct Python objects from it.

    Returning the node tree lets us detect duplicate mapping keys (which PyYAML
    silently collapses) while still using ``safe_load`` semantics for the data.
    """
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None, None
        data = loader.construct_document(node)
    finally:
        loader.dispose()
    return node, data


def _collect_duplicate_keys(node: yaml.Node, path: str, collector: _Collector) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            child_path = path
            if isinstance(key_node, yaml.ScalarNode) and isinstance(
                key_node.value, str
            ):
                key = key_node.value
                child_path = f"{path}.{key}" if path else key
                if key in seen:
                    collector.add(child_path, f"duplicate key '{key}'")
                else:
                    seen.add(key)
            _collect_duplicate_keys(value_node, child_path, collector)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _collect_duplicate_keys(item, f"{path}[{index}]", collector)


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


def _validate_top_level(data: Mapping[str, Any], collector: _Collector) -> None:
    version = data.get("version")
    if type(version) is not int or version != 1:
        collector.add("version", "expected schema version 1")
    name = data.get("name")
    if name is None:
        collector.add("name", "name is required")
    elif not _is_valid_identifier(name):
        collector.add("name", "name must match [a-z][a-z0-9_]*")
    data_sources = data.get("data_sources")
    if data_sources is None:
        collector.add("data_sources", "data_sources is required")
    elif not isinstance(data_sources, Mapping):
        collector.add("data_sources", "data_sources must be a mapping")
    for field in ("label", "description"):
        _check_optional_string(data, field, "", collector)
    for section in _COLLECTIONS[1:]:
        if section in data and not isinstance(data[section], Mapping):
            collector.add(section, f"{section} must be a mapping")


def _validate_data_sources(sources: Mapping[str, Any], collector: _Collector) -> None:
    for key, raw in sources.items():
        base = f"data_sources.{key}"
        _validate_identifier_key(key, base, "data source", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "data source must be a mapping")
            continue
        _require(raw, "type", base, collector)
        _require(raw, "path", base, collector)
        _check_optional_string(raw, "type", base, collector)
        _check_optional_string(raw, "path", base, collector)
        grain = raw.get("grain")
        grain_path = f"{base}.grain"
        if grain is None:
            collector.add(grain_path, "grain is required")
        elif not isinstance(grain, list):
            collector.add(grain_path, "grain must be a list of column names")
        elif not grain:
            collector.add(grain_path, "grain must be non-empty")
        elif not all(isinstance(column, str) for column in grain):
            collector.add(grain_path, "grain entries must be strings")


def _validate_dimensions(
    dimensions: Mapping[str, Any], known_sources: frozenset[str], collector: _Collector
) -> None:
    for key, raw in dimensions.items():
        base = f"dimensions.{key}"
        _validate_identifier_key(key, base, "dimension", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "dimension must be a mapping")
            continue
        _require(raw, "source", base, collector)
        _require(raw, "column", base, collector)
        _require(raw, "data_type", base, collector)
        for field in ("source", "column", "data_type"):
            _check_optional_string(raw, field, base, collector)
        _check_optional_string(raw, "description", base, collector)
        _check_known_source(
            raw.get("source"), known_sources, f"{base}.source", collector
        )


def _validate_facts(
    facts: Mapping[str, Any],
    known_sources: frozenset[str],
    collector: _Collector,
    parsed: dict[str, Expression],
) -> None:
    for key, raw in facts.items():
        base = f"facts.{key}"
        _validate_identifier_key(key, base, "fact", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "fact must be a mapping")
            continue
        _require(raw, "source", base, collector)
        _require(raw, "data_type", base, collector)
        _require(raw, "expression", base, collector)
        for field in ("source", "data_type", "expression"):
            _check_optional_string(raw, field, base, collector)
        _check_optional_string(raw, "description", base, collector)
        _check_known_source(
            raw.get("source"), known_sources, f"{base}.source", collector
        )
        _parse_and_validate_row(
            raw.get("expression"),
            known_sources,
            f"{base}.expression",
            collector,
            parsed,
            key,
        )


def _validate_measures(
    measures: Mapping[str, Any], known_facts: frozenset[str], collector: _Collector
) -> None:
    for key, raw in measures.items():
        base = f"measures.{key}"
        _validate_identifier_key(key, base, "measure", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "measure must be a mapping")
            continue
        _require(raw, "fact", base, collector)
        _require(raw, "aggregation", base, collector)
        _check_optional_string(raw, "fact", base, collector)
        _check_optional_string(raw, "aggregation", base, collector)
        _check_optional_string(raw, "description", base, collector)
        fact = raw.get("fact")
        if isinstance(fact, str) and fact not in known_facts:
            collector.add(f"{base}.fact", f"fact '{fact}' is not a known fact")
        aggregation = raw.get("aggregation")
        if isinstance(aggregation, str) and aggregation not in _AGGREGATIONS:
            collector.add(
                f"{base}.aggregation", f"unsupported aggregation '{aggregation}'"
            )


def _validate_metrics(
    metrics: Mapping[str, Any],
    known_measures: frozenset[str],
    collector: _Collector,
    parsed: dict[str, Expression],
) -> None:
    for key, raw in metrics.items():
        base = f"metrics.{key}"
        _validate_identifier_key(key, base, "metric", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "metric must be a mapping")
            continue
        _require(raw, "expression", base, collector)
        _require(raw, "measures", base, collector)
        _check_optional_string(raw, "expression", base, collector)
        _check_optional_string(raw, "description", base, collector)
        declared = raw.get("measures")
        declared_set = _declared_measures(declared)
        if not isinstance(declared, list):
            collector.add(f"{base}.measures", "measures must be a list of strings")
        elif not all(isinstance(measure, str) for measure in declared):
            collector.add(f"{base}.measures", "measures entries must be strings")
        else:
            for measure in declared:
                if measure not in known_measures:
                    collector.add(
                        f"{base}.measures",
                        f"measure '{measure}' is not a known measure",
                    )
        _parse_and_validate_metric(
            raw.get("expression"),
            declared_set,
            f"{base}.expression",
            collector,
            parsed,
            key,
        )


def _validate_relationships(
    relationships: Mapping[str, Any],
    known_sources: frozenset[str],
    collector: _Collector,
) -> None:
    for key, raw in relationships.items():
        base = f"relationships.{key}"
        _validate_identifier_key(key, base, "relationship", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "relationship must be a mapping")
            continue
        _require(raw, "source", base, collector)
        _require(raw, "target", base, collector)
        _require(raw, "type", base, collector)
        _require(raw, "source_column", base, collector)
        _require(raw, "target_column", base, collector)
        for field in ("source", "target", "type", "source_column", "target_column"):
            _check_optional_string(raw, field, base, collector)
        _check_known_source(
            raw.get("source"), known_sources, f"{base}.source", collector
        )
        _check_known_source(
            raw.get("target"), known_sources, f"{base}.target", collector
        )
        cardinality = raw.get("type")
        if isinstance(cardinality, str) and cardinality not in _CARDINALITIES:
            collector.add(f"{base}.type", f"unsupported cardinality '{cardinality}'")


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _check_known_source(
    source: object,
    known_sources: frozenset[str],
    path: str,
    collector: _Collector,
) -> None:
    if isinstance(source, str) and source not in known_sources:
        collector.add(path, f"source '{source}' is not a known data source")


def _declared_measures(declared: object) -> frozenset[str]:
    if isinstance(declared, list) and all(isinstance(m, str) for m in declared):
        return frozenset(declared)
    return frozenset()


def _parse_and_validate_row(
    expression_text: object,
    known_sources: frozenset[str],
    path: str,
    collector: _Collector,
    parsed: dict[str, Expression],
    key: str,
) -> None:
    if not isinstance(expression_text, str):
        if expression_text is not None:
            collector.add(path, "expression must be a string")
        return
    try:
        expression = parse_expression(expression_text)
    except ExpressionSyntaxError as error:
        collector.add(path, f"invalid expression: {error.message}")
        return
    parsed[key] = expression
    for message in validate_row_expression(expression, known_sources):
        collector.add(path, message)


def _parse_and_validate_metric(
    expression_text: object,
    declared_measures: frozenset[str],
    path: str,
    collector: _Collector,
    parsed: dict[str, Expression],
    key: str,
) -> None:
    if not isinstance(expression_text, str):
        if expression_text is not None:
            collector.add(path, "expression must be a string")
        return
    try:
        expression = parse_expression(expression_text)
    except ExpressionSyntaxError as error:
        collector.add(path, f"invalid expression: {error.message}")
        return
    parsed[key] = expression
    for message in validate_metric_expression(expression, declared_measures):
        collector.add(path, message)


# ---------------------------------------------------------------------------
# Model construction (only reached when there are no issues)
# ---------------------------------------------------------------------------


def _build_layer(
    data: Mapping[str, Any],
    fact_expressions: Mapping[str, Expression],
    metric_expressions: Mapping[str, Expression],
) -> SemanticLayer:
    sources = _collection(data, "data_sources")
    dimensions = _collection(data, "dimensions")
    facts = _collection(data, "facts")
    measures = _collection(data, "measures")
    metrics = _collection(data, "metrics")
    relationships = _collection(data, "relationships")

    return SemanticLayer(
        version=1,
        name=data["name"],
        label=str(data.get("label") or ""),
        description=str(data.get("description") or ""),
        data_sources=MappingProxyType(
            {
                key: DataSource(
                    name=key,
                    type=raw["type"],
                    path=raw["path"],
                    grain=tuple(raw["grain"]),
                )
                for key, raw in sources.items()
            }
        ),
        dimensions=MappingProxyType(
            {
                key: Dimension(
                    name=key,
                    source=raw["source"],
                    column=raw["column"],
                    data_type=raw["data_type"],
                    description=str(raw.get("description") or ""),
                )
                for key, raw in dimensions.items()
            }
        ),
        facts=MappingProxyType(
            {
                key: Fact(
                    name=key,
                    source=raw["source"],
                    expression=fact_expressions[key],
                    data_type=raw["data_type"],
                    description=str(raw.get("description") or ""),
                )
                for key, raw in facts.items()
            }
        ),
        measures=MappingProxyType(
            {
                key: Measure(
                    name=key,
                    fact=raw["fact"],
                    aggregation=raw["aggregation"],
                    description=str(raw.get("description") or ""),
                )
                for key, raw in measures.items()
            }
        ),
        metrics=MappingProxyType(
            {
                key: Metric(
                    name=key,
                    expression=metric_expressions[key],
                    measures=tuple(raw["measures"]),
                    description=str(raw.get("description") or ""),
                )
                for key, raw in metrics.items()
            }
        ),
        relationships=MappingProxyType(
            {
                key: Relationship(
                    name=key,
                    source=raw["source"],
                    target=raw["target"],
                    type=raw["type"],
                    source_column=raw["source_column"],
                    target_column=raw["target_column"],
                )
                for key, raw in relationships.items()
            }
        ),
    )


def load(path: str | Path) -> SemanticLayer:
    """Load and validate a schema-version-1 catalog from ``path``.

    Returns a fully valid immutable :class:`SemanticLayer`, or raises a single
    :class:`CatalogValidationError` whose ``issues`` are sorted by
    ``(path, message)``.
    """
    collector = _Collector(issues=[])
    try:
        text = Path(path).read_text(encoding="utf-8")
        node, data = _compose_and_construct(text)
    except yaml.YAMLError as error:
        collector.add("", f"invalid YAML: {error}")
        collector.raise_if_any()
        raise AssertionError("unreachable")
    except (TypeError, ValueError) as error:
        collector.add("", f"invalid catalog structure: {error}")
        collector.raise_if_any()
        raise AssertionError("unreachable")

    if node is not None:
        _collect_duplicate_keys(node, "", collector)

    if not isinstance(data, Mapping):
        collector.add("", "catalog root must be a mapping")
        collector.raise_if_any()

    catalog = data if isinstance(data, Mapping) else {}
    _validate_top_level(catalog, collector)

    sources = _collection(catalog, "data_sources")
    dimensions = _collection(catalog, "dimensions")
    facts = _collection(catalog, "facts")
    measures = _collection(catalog, "measures")
    metrics = _collection(catalog, "metrics")
    relationships = _collection(catalog, "relationships")

    known_sources = frozenset(sources)
    known_facts = frozenset(facts)
    known_measures = frozenset(measures)

    fact_expressions: dict[str, Expression] = {}
    metric_expressions: dict[str, Expression] = {}

    _validate_data_sources(sources, collector)
    _validate_dimensions(dimensions, known_sources, collector)
    _validate_facts(facts, known_sources, collector, fact_expressions)
    _validate_measures(measures, known_facts, collector)
    _validate_metrics(metrics, known_measures, collector, metric_expressions)
    _validate_relationships(relationships, known_sources, collector)

    collector.raise_if_any()
    return _build_layer(catalog, fact_expressions, metric_expressions)


__all__ = [
    "CatalogIssue",
    "CatalogValidationError",
    "SemanticLayer",
    "load",
]
