"""Strict YAML loader for the schema-version-1 grain-aware catalog.

``SemanticLayer.load`` parses a catalog file into the immutable
:class:`~selayer.model.SemanticLayer` model. It either returns a fully
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
8. validate metric grains and fact-expression reachability;
9. sort by ``(path, message)``;
10. raise one error when any issue exists;
11. return only immutable objects otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypedDict, get_args

import yaml

from selayer.expressions import ExpressionSyntaxError, parse_expression
from selayer.expressions.ast import Expression
from selayer.expressions.validation import (
    references,
    validate_metric_expression,
    validate_row_expression,
)
from selayer.model import (
    Aggregation,
    Cardinality,
    DataSource,
    Dimension,
    Fact,
    Measure,
    Metric,
    Relationship,
    SemanticLayer,
    SemanticObject,
    SemanticStatus,
)
from selayer.sources.catalog import (
    ParsedSource,
    parse_source_declarations,
    validate_source_declarations,
)
from selayer.sources.schema import (
    DecimalType,
    FieldSchema,
    LogicalType,
    ScalarType,
    TableSchema,
    TimestampType,
)

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_AGGREGATIONS: frozenset[str] = frozenset(
    get_args(getattr(Aggregation, "__value__", Aggregation))
)
_CARDINALITIES: frozenset[str] = frozenset(
    get_args(getattr(Cardinality, "__value__", Cardinality))
)
# Canonical string values accepted for the ``status`` metadata field. Built
# from :class:`~selayer.model.SemanticStatus` so the catalog parser and the
# model cannot drift on the accepted status set.
_SEMANTIC_STATUS_VALUES: frozenset[str] = frozenset(
    member.value for member in SemanticStatus
)

# Semantic data_type -> set of compatible logical type "kinds".  A dimension
# or fact declares a coarse semantic ``data_type`` (e.g. ``decimal``); this
# table decides whether a column's declared logical type satisfies it.  The
# comparison is by membership only — values are never coerced or cast.
_INT_SCALARS = frozenset(
    {"int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"}
)
_FLOAT_SCALARS = frozenset({"float16", "float32", "float64"})
_DATA_TYPE_COMPAT: Mapping[str, frozenset[str]] = {
    "string": frozenset({"utf8", "large_utf8"}),
    "integer": _INT_SCALARS,
    "decimal": frozenset({"decimal"}) | _FLOAT_SCALARS,
    "float": _FLOAT_SCALARS,
    "double": frozenset({"float64"}),
    "boolean": frozenset({"boolean"}),
    "timestamp": frozenset({"timestamp"}),
    "date": frozenset({"date32", "date64"}),
}


def _logical_type_kind(logical: LogicalType) -> str:
    """Return the kind discriminator a logical type satisfies."""

    if isinstance(logical, ScalarType):
        return logical.name
    if isinstance(logical, DecimalType):
        return "decimal"
    if isinstance(logical, TimestampType):
        return "timestamp"
    return type(logical).__name__


def _data_type_compatible(data_type: str, logical: LogicalType) -> bool:
    """Return whether a semantic data_type accepts a column's logical type."""

    compatible = _DATA_TYPE_COMPAT.get(data_type)
    if compatible is None:
        return False
    return _logical_type_kind(logical) in compatible


_NUMERIC_DATA_TYPES = frozenset({"integer", "decimal", "float", "double"})
_ORDERABLE_DATA_TYPES = frozenset(
    {"string", "integer", "decimal", "float", "double", "boolean", "timestamp", "date"}
)

# Logical scalar names that share the integer join-equivalence family.
_INTEGER_KINDS = frozenset(
    {
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
    }
)
# Logical scalar names that share the floating-point join-equivalence family
# (``decimal`` carries scale/precision but is join-equivalent to floats).
_FLOAT_KINDS = frozenset({"float16", "float32", "float64", "decimal"})


def _logical_kind_join_group(kind: str) -> str:
    """Return the coarse join-equivalence group for a logical type kind.

    Integer and floating-point families collapse to a single group so a join
    between, e.g., ``int32`` and ``int64`` columns is accepted, while a join
    between unrelated kinds (``utf8`` and ``int64``) is rejected. No coercion
    is performed: the comparison is by group membership only.
    """
    if kind in _INTEGER_KINDS:
        return "integer"
    if kind in _FLOAT_KINDS:
        return "float"
    return kind


def _join_type_compatible(source_type: LogicalType, target_type: LogicalType) -> bool:
    """Return whether two relationship join columns have equivalent types."""
    return _logical_kind_join_group(
        _logical_type_kind(source_type)
    ) == _logical_kind_join_group(_logical_type_kind(target_type))


def _validate_grain_columns(
    source_name: str,
    grain: tuple[str, ...],
    schema: TableSchema,
    collector: _Collector,
) -> None:
    """Validate grain uniqueness, column existence, and non-nullability.

    Shared by the raw loader (over a parsed source schema) and the typed
    model validator so both report identical grain codes.
    """
    path = f"data_sources.{source_name}.grain"
    if len(grain) != len(set(grain)):
        collector.add(
            path, "grain columns must be unique", "catalog.grain.duplicate_column"
        )
    schema_columns = {field.name for field in schema.fields}
    for column in grain:
        if column not in schema_columns:
            collector.add(
                path,
                f"grain column {column!r} is not declared in the source schema",
            )
            continue
        if schema.field(column).nullable:
            collector.add(
                path,
                f"grain column {column!r} must be non-nullable",
                "catalog.grain.nullable_column",
            )


def _validate_measure_aggregation(
    measure_name: str,
    aggregation: str,
    fact_data_type: str | None,
    collector: _Collector,
) -> None:
    """Flag sum/avg over non-numeric facts and min/max over non-orderable ones."""
    if fact_data_type is None:
        return
    if aggregation in {"sum", "avg"} and fact_data_type not in _NUMERIC_DATA_TYPES:
        collector.add(
            f"measures.{measure_name}.aggregation",
            "sum and avg require a numeric fact",
            "catalog.measure.invalid_aggregation_type",
        )
    if aggregation in {"min", "max"} and fact_data_type not in _ORDERABLE_DATA_TYPES:
        collector.add(
            f"measures.{measure_name}.aggregation",
            "min and max require an orderable fact",
            "catalog.measure.invalid_aggregation_type",
        )


def _validate_relationship_join(
    name: str,
    source: str,
    target: str,
    source_column: str,
    target_column: str,
    source_schemas: Mapping[str, TableSchema],
    collector: _Collector,
) -> None:
    """Flag relationship joins whose endpoint columns have incompatible types."""
    source_field = _schema_field(source_schemas, source, source_column)
    target_field = _schema_field(source_schemas, target, target_column)
    if source_field is None or target_field is None:
        return
    if not _join_type_compatible(source_field.type, target_field.type):
        collector.add(
            f"relationships.{name}",
            "relationship join columns must have compatible types",
            "catalog.relationship.join_type_mismatch",
        )


@dataclass(frozen=True, slots=True)
class CatalogIssue:
    """A single catalog validation problem located at a field path.

    ``code`` is a stable, machine-readable identifier for the rule that fired
    (e.g. ``catalog.version.unsupported``); it defaults to ``catalog.invalid``
    so the historical two-argument ``(path, message)`` construction keeps
    working unchanged.
    """

    path: str
    message: str
    code: str = "catalog.invalid"


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


class _Collector:
    def __init__(self) -> None:
        self.issues: list[CatalogIssue] = []

    def add(
        self,
        path: str,
        message: str,
        code: str = "catalog.invalid",
    ) -> None:
        self.issues.append(CatalogIssue(path, message, code))

    def raise_if_any(self) -> None:
        if not self.issues:
            return
        # Sort by ``(path, message)`` only: the ``code`` is intentionally
        # excluded so adding codes does not change the existing deterministic
        # message order. Two issues sharing a path+message but differing codes
        # keep their relative insertion order, which is stable for a single
        # load pass because each rule emits at most one issue per path.
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


def _semantic_metadata(
    raw: Mapping[str, Any], base: str
) -> tuple[SemanticStatus, str | None, list[CatalogIssue]]:
    """Validate and parse ``(status, replaced_by)`` for one semantic object.

    A single shared helper used by every section validator (to collect issues)
    and the model builder (to construct typed values) so the deprecation rule
    has exactly one definition. ``status`` defaults to
    :data:`~selayer.model.SemanticStatus.ACTIVE` only when the field is absent
    (``"status" not in raw``); an explicitly-supplied null or other non-string
    is a malformed value and is rejected rather than ignored. It accepts only
    the values of :data:`_SEMANTIC_STATUS_VALUES`. ``replaced_by`` defaults to
    ``None`` only when absent; when present it must be a string, and is rejected
    on a validly-active object (a replacement target is only meaningful once the
    object is deprecated).

    Field presence is checked with ``in`` rather than truthiness on
    ``raw.get(...)`` so that an explicitly-supplied YAML ``null`` (which parses
    to ``None``) is treated as a present, malformed value instead of being
    silently coerced to the default.

    Returns the resolved ``(status, replaced_by)`` plus any issues rooted at
    ``base``; malformed fields always yield an issue and never silently fall
    through. When the ``status`` field itself is malformed the replacement
    check is skipped because the object's lifecycle state is undecided and the
    status issue already reported is sufficient.
    """
    issues: list[CatalogIssue] = []

    status: SemanticStatus = SemanticStatus.ACTIVE
    status_known = True
    if "status" in raw:
        status_raw = raw["status"]
        if not isinstance(status_raw, str):
            issues.append(CatalogIssue(f"{base}.status", "status must be a string"))
            status_known = False
        elif status_raw not in _SEMANTIC_STATUS_VALUES:
            issues.append(
                CatalogIssue(f"{base}.status", f"unsupported status {status_raw!r}")
            )
            status_known = False
        else:
            status = SemanticStatus(status_raw)

    replaced_by: str | None = None
    if "replaced_by" in raw:
        replaced_by_raw = raw["replaced_by"]
        if not isinstance(replaced_by_raw, str):
            issues.append(
                CatalogIssue(f"{base}.replaced_by", "replaced_by must be a string")
            )
        else:
            replaced_by = replaced_by_raw

    if status_known and status is SemanticStatus.ACTIVE and replaced_by is not None:
        issues.append(
            CatalogIssue(
                f"{base}.replaced_by",
                "replaced_by is only allowed on deprecated objects",
            )
        )

    return status, replaced_by, issues


# Semantic metadata keys owned by :func:`_semantic_metadata`. They are not
# connector fields and must be stripped from a raw source declaration before
# it reaches the closed connector-field validator, which would otherwise
# reject them as unknown fields.
_SOURCE_METADATA_KEYS: frozenset[str] = frozenset({"status", "replaced_by"})


class _MetadataKwargs(TypedDict):
    """Precisely-typed ``status``/``replaced_by`` constructor kwargs.

    A ``TypedDict`` (rather than a plain ``dict``) so ``**_metadata_kwargs(...)``
    splats keep each key's exact type at the call site instead of widening to
    a common value union.
    """

    status: SemanticStatus
    replaced_by: str | None


def _strip_source_metadata(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return ``raw`` without semantic metadata keys.

    The deprecation metadata is validated and parsed separately by
    :func:`_semantic_metadata`; it is never a connector option, so it is
    removed before the declaration is handed to the connector field validator
    (and parser) in :mod:`selayer.sources.catalog`. A new mapping is built only
    when a metadata key is actually present.
    """
    if not any(key in raw for key in _SOURCE_METADATA_KEYS):
        return raw
    return {
        key: value for key, value in raw.items() if key not in _SOURCE_METADATA_KEYS
    }


def _metadata_kwargs(raw: Mapping[str, Any], base: str) -> _MetadataKwargs:
    """Resolve ``status``/``replaced_by`` constructor kwargs for one object.

    Safe to call at build time: by the time :func:`_build_layer` runs,
    validation has already collected (and raised on) every malformed metadata
    field, so the returned values are guaranteed well-formed. Re-running the
    single shared helper keeps the deprecation rule in one place rather than
    re-deriving the defaults inline.
    """
    status, replaced_by, _ = _semantic_metadata(raw, base)
    return {"status": status, "replaced_by": replaced_by}


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
        collector.add(
            "version",
            "expected schema version 1",
            "catalog.version.unsupported",
        )
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


def _validate_data_sources(
    sources: Mapping[str, Any],
    catalog_path: Path,
    collector: _Collector,
) -> dict[str, ParsedSource]:
    """Validate data source declarations and return parsed sources.

    Connector and schema declaration validation is delegated to
    :func:`selayer.sources.catalog.validate_source_declarations`; its issues are
    converted into :class:`CatalogIssue` values.  When sources are clean they are
    parsed into :class:`ParsedSource` objects so downstream cross-validation can
    check grain columns, dimension columns, and references against the declared
    schema.  An empty mapping is returned when any source declaration is invalid
    so dependent checks are skipped deterministically.
    """

    # Semantic metadata (status/replaced_by) is validated here and stripped
    # before the declaration reaches the closed connector-field validator,
    # which would otherwise reject it as an unknown field.
    connector_decls: dict[str, Any] = {}
    for key, raw in sources.items():
        base = f"data_sources.{key}"
        _validate_identifier_key(key, base, "data source", collector)
        if isinstance(raw, Mapping):
            _, _, meta_issues = _semantic_metadata(raw, base)
            collector.issues.extend(meta_issues)
            connector_decls[key] = _strip_source_metadata(raw)
        else:
            connector_decls[key] = raw
    source_issues = validate_source_declarations(connector_decls, catalog_path)
    for issue in source_issues:
        collector.add(issue.path, issue.message)
    if source_issues:
        # Source declarations are invalid; dependent schema checks would
        # operate on an unparseable model, so return an empty mapping.
        return {}
    try:
        parsed = parse_source_declarations(connector_decls, catalog_path)
    except Exception:  # noqa: BLE001 - parse only runs after validation
        return {}
    for name, source in parsed.items():
        _validate_grain_columns(name, source.grain, source.schema, collector)
    return dict(parsed)


def _validate_dimensions(
    dimensions: Mapping[str, Any],
    known_sources: frozenset[str],
    source_schemas: Mapping[str, TableSchema],
    collector: _Collector,
) -> None:
    for key, raw in dimensions.items():
        base = f"dimensions.{key}"
        _validate_identifier_key(key, base, "dimension", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "dimension must be a mapping")
            continue
        _, _, meta_issues = _semantic_metadata(raw, base)
        collector.issues.extend(meta_issues)
        _require(raw, "source", base, collector)
        _require(raw, "column", base, collector)
        _require(raw, "data_type", base, collector)
        for field in ("source", "column", "data_type"):
            _check_optional_string(raw, field, base, collector)
        _check_optional_string(raw, "description", base, collector)
        source = raw.get("source")
        _check_known_source(source, known_sources, f"{base}.source", collector)
        if isinstance(source, str) and source in source_schemas:
            _check_column_and_type(
                source_schemas,
                source,
                str(raw.get("column")),
                str(raw.get("data_type")),
                base,
                collector,
            )


def _validate_facts(
    facts: Mapping[str, Any],
    known_sources: frozenset[str],
    source_schemas: Mapping[str, TableSchema],
    collector: _Collector,
    parsed: dict[str, Expression],
) -> None:
    for key, raw in facts.items():
        base = f"facts.{key}"
        _validate_identifier_key(key, base, "fact", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "fact must be a mapping")
            continue
        _, _, meta_issues = _semantic_metadata(raw, base)
        collector.issues.extend(meta_issues)
        _require(raw, "source", base, collector)
        _require(raw, "data_type", base, collector)
        _require(raw, "expression", base, collector)
        for field in ("source", "data_type", "expression"):
            _check_optional_string(raw, field, base, collector)
        _check_optional_string(raw, "description", base, collector)
        source = raw.get("source")
        _check_known_source(source, known_sources, f"{base}.source", collector)
        expression = _parse_and_validate_row(
            raw.get("expression"),
            known_sources,
            f"{base}.expression",
            collector,
            parsed,
            key,
        )
        # Every source.column reference must resolve to a declared column.
        if expression is not None:
            for reference in references(expression):
                if len(reference.parts) != 2:
                    continue
                ref_source, ref_column = reference.parts
                if ref_source in source_schemas:
                    _check_column_exists(
                        source_schemas,
                        ref_source,
                        ref_column,
                        f"{base}.expression",
                        collector,
                    )
            # For a simple single-column fact, the declared data_type must be
            # compatible with the referenced column's logical type.  Arithmetic
            # and function expressions have no single source column, so they are
            # not type-checked here.
            ref_columns = [r for r in references(expression) if len(r.parts) == 2]
            if len(ref_columns) == 1 and ref_columns[0].parts[0] in source_schemas:
                ref_source, ref_column = ref_columns[0].parts
                _check_column_type(
                    source_schemas,
                    ref_source,
                    ref_column,
                    str(raw.get("data_type")),
                    f"{base}.data_type",
                    collector,
                )


def _validate_measures(
    measures: Mapping[str, Any], facts: Mapping[str, Any], collector: _Collector
) -> None:
    known_facts = frozenset(facts)
    for key, raw in measures.items():
        base = f"measures.{key}"
        _validate_identifier_key(key, base, "measure", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "measure must be a mapping")
            continue
        _, _, meta_issues = _semantic_metadata(raw, base)
        collector.issues.extend(meta_issues)
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
        fact_data_type: str | None = None
        if isinstance(fact, str) and fact in known_facts:
            raw_fact = facts.get(fact)
            if isinstance(raw_fact, Mapping):
                data_type = raw_fact.get("data_type")
                if isinstance(data_type, str):
                    fact_data_type = data_type
        if isinstance(aggregation, str) and fact_data_type is not None:
            _validate_measure_aggregation(key, aggregation, fact_data_type, collector)


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
        _, _, meta_issues = _semantic_metadata(raw, base)
        collector.issues.extend(meta_issues)
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
    source_schemas: Mapping[str, TableSchema],
    collector: _Collector,
) -> None:
    for key, raw in relationships.items():
        base = f"relationships.{key}"
        _validate_identifier_key(key, base, "relationship", collector)
        if not isinstance(raw, Mapping):
            collector.add(base, "relationship must be a mapping")
            continue
        _, _, meta_issues = _semantic_metadata(raw, base)
        collector.issues.extend(meta_issues)
        _require(raw, "source", base, collector)
        _require(raw, "target", base, collector)
        _require(raw, "type", base, collector)
        _require(raw, "source_column", base, collector)
        _require(raw, "target_column", base, collector)
        for field in ("source", "target", "type", "source_column", "target_column"):
            _check_optional_string(raw, field, base, collector)
        source = raw.get("source")
        target = raw.get("target")
        _check_known_source(source, known_sources, f"{base}.source", collector)
        _check_known_source(target, known_sources, f"{base}.target", collector)
        cardinality = raw.get("type")
        if isinstance(cardinality, str) and cardinality not in _CARDINALITIES:
            collector.add(f"{base}.type", f"unsupported cardinality '{cardinality}'")
        if isinstance(source, str) and source in source_schemas:
            _check_column_exists(
                source_schemas,
                source,
                str(raw.get("source_column")),
                f"{base}.source_column",
                collector,
            )
        if isinstance(target, str) and target in source_schemas:
            _check_column_exists(
                source_schemas,
                target,
                str(raw.get("target_column")),
                f"{base}.target_column",
                collector,
            )
        _validate_relationship_join(
            key,
            source if isinstance(source, str) else "",
            target if isinstance(target, str) else "",
            str(raw.get("source_column")),
            str(raw.get("target_column")),
            source_schemas,
            collector,
        )


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


def _schema_field(
    source_schemas: Mapping[str, TableSchema], source_id: str, column: str
) -> FieldSchema | None:
    schema = source_schemas.get(source_id)
    if schema is None:
        return None
    for field in schema.fields:
        if field.name == column:
            return field
    return None


def _check_column_exists(
    source_schemas: Mapping[str, TableSchema],
    source_id: str,
    column: str,
    path: str,
    collector: _Collector,
) -> None:
    if _schema_field(source_schemas, source_id, column) is None:
        collector.add(
            path,
            f"column {column!r} is not declared in source {source_id!r} schema",
        )


def _check_column_type(
    source_schemas: Mapping[str, TableSchema],
    source_id: str,
    column: str,
    data_type: str,
    path: str,
    collector: _Collector,
) -> None:
    field = _schema_field(source_schemas, source_id, column)
    if field is None:
        return
    if not _data_type_compatible(data_type, field.type):
        collector.add(
            path,
            f"data_type {data_type!r} is not compatible with column {column!r} type",
        )


def _check_column_and_type(
    source_schemas: Mapping[str, TableSchema],
    source_id: str,
    column: str,
    data_type: str,
    base: str,
    collector: _Collector,
) -> None:
    """Check a dimension's column exists and its type matches the data_type."""

    _check_column_exists(source_schemas, source_id, column, f"{base}.column", collector)
    _check_column_type(
        source_schemas, source_id, column, data_type, f"{base}.data_type", collector
    )


def _parse_and_validate_row(
    expression_text: object,
    known_sources: frozenset[str],
    path: str,
    collector: _Collector,
    parsed: dict[str, Expression],
    key: str,
) -> Expression | None:
    if not isinstance(expression_text, str):
        if expression_text is not None:
            collector.add(path, "expression must be a string")
        return None
    try:
        expression = parse_expression(expression_text)
    except ExpressionSyntaxError as error:
        collector.add(path, f"invalid expression: {error.message}")
        return None
    parsed[key] = expression
    for message in validate_row_expression(expression, known_sources):
        collector.add(path, message)
    return expression


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
# Grain and reachability validation
# ---------------------------------------------------------------------------


def _safe_relationship_edges(
    edges: Iterable[tuple[object, object, object]],
    known_sources: frozenset[str],
) -> dict[str, tuple[str, ...]]:
    """Build directed grain-preserving source-traversal edges from triples.

    ``edges`` is an iterable of ``(source, target, cardinality)`` triples
    shared by the raw YAML loader and the typed model validators, so both
    paths use one implementation of the safe-traversal rule. Cardinality is
    interpreted from the relationship's declared source to target:
    one-to-many is safe only from target (many) to source (one), many-to-one
    only from source (many) to target (one); many-to-many contributes no
    edges.
    """
    graph: dict[str, list[str]] = {}
    for source, target, cardinality in edges:
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in known_sources
            or target not in known_sources
        ):
            continue
        if cardinality == "one_to_one":
            graph.setdefault(source, []).append(target)
            graph.setdefault(target, []).append(source)
        elif cardinality == "one_to_many":
            graph.setdefault(target, []).append(source)
        elif cardinality == "many_to_one":
            graph.setdefault(source, []).append(target)
    return {source: tuple(sorted(targets)) for source, targets in graph.items()}


def _safe_relationship_graph(
    relationships: Mapping[str, Any], known_sources: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    """Build the safe-traversal graph from raw relationship mappings."""
    return _safe_relationship_edges(
        (
            (raw.get("source"), raw.get("target"), raw.get("type"))
            for raw in relationships.values()
            if isinstance(raw, Mapping)
        ),
        known_sources,
    )


def _has_safe_path(graph: Mapping[str, tuple[str, ...]], start: str, goal: str) -> bool:
    if start == goal:
        return True
    pending = [start]
    seen = {start}
    while pending:
        current = pending.pop(0)
        for target in graph.get(current, ()):
            if target == goal:
                return True
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return False


def _validate_fact_expression_reachability(
    facts: Mapping[str, Any],
    sources: Mapping[str, Any],
    relationships: Mapping[str, Any],
    parsed: Mapping[str, Expression],
    known_sources: frozenset[str],
    collector: _Collector,
) -> None:
    graph = _safe_relationship_graph(relationships, known_sources)
    entries: list[tuple[str, object, Expression]] = []
    for fact_name, expression in parsed.items():
        raw = facts.get(fact_name)
        if not isinstance(raw, Mapping):
            continue
        anchor = raw.get("source")
        if not isinstance(anchor, str) or anchor not in sources:
            continue
        entries.append((fact_name, anchor, expression))
    _fact_reachability_issues(entries, graph, known_sources, collector)


def _fact_reachability_issues(
    facts: Iterable[tuple[str, object, Expression]],
    graph: Mapping[str, tuple[str, ...]],
    known_sources: frozenset[str],
    collector: _Collector,
) -> None:
    """Flag facts whose expression references an unreachable source.

    ``facts`` is an iterable of ``(name, anchor_source, expression)`` triples
    shared by the raw and typed validators, so the reachability rule has one
    implementation.
    """
    reported: set[tuple[str, str]] = set()
    for fact_name, anchor, expression in facts:
        if not isinstance(anchor, str) or anchor not in known_sources:
            continue
        for reference in references(expression):
            if len(reference.parts) != 2:
                continue
            referenced_source = reference.parts[0]
            if referenced_source not in known_sources or referenced_source == anchor:
                continue
            if not _has_safe_path(graph, anchor, referenced_source):
                issue_key = (fact_name, referenced_source)
                if issue_key in reported:
                    continue
                reported.add(issue_key)
                collector.add(
                    f"facts.{fact_name}.expression",
                    f"source '{referenced_source}' is not reachable from anchor "
                    f"'{anchor}' through grain-preserving relationships",
                )


def _metric_grain_issues(
    metric_grains: Iterable[tuple[str, list[tuple[str, tuple[str, ...]]]]],
    collector: _Collector,
) -> None:
    """Flag metrics whose declared measures disagree on anchor or grain.

    ``metric_grains`` is an iterable of ``(name, resolved)`` pairs where
    ``resolved`` is a list of ``(anchor, grain)`` tuples, shared by the raw
    and typed validators, so the grain rule has one implementation.
    """
    for metric_name, resolved in metric_grains:
        if len(resolved) > 1 and any(item != resolved[0] for item in resolved[1:]):
            collector.add(
                f"metrics.{metric_name}.measures",
                "declared measures must share exactly the same anchor source and grain",
            )


def _validate_metric_grains(
    metrics: Mapping[str, Any],
    measures: Mapping[str, Any],
    facts: Mapping[str, Any],
    sources: Mapping[str, Any],
    collector: _Collector,
) -> None:
    """Validate metric grain consistency over raw mappings via the shared core."""

    def resolved_grains(
        raw_metric: Mapping[str, Any],
    ) -> list[tuple[str, tuple[str, ...]]]:
        declared = raw_metric.get("measures")
        if not isinstance(declared, list) or not all(
            isinstance(measure_id, str) for measure_id in declared
        ):
            return []
        resolved: list[tuple[str, tuple[str, ...]]] = []
        for measure_id in declared:
            raw_measure = measures.get(measure_id)
            if not isinstance(raw_measure, Mapping):
                continue
            fact_id = raw_measure.get("fact")
            raw_fact = facts.get(fact_id) if isinstance(fact_id, str) else None
            anchor = raw_fact.get("source") if isinstance(raw_fact, Mapping) else None
            raw_source = sources.get(anchor) if isinstance(anchor, str) else None
            grain = raw_source.get("grain") if isinstance(raw_source, Mapping) else None
            if (
                isinstance(anchor, str)
                and isinstance(grain, list)
                and all(isinstance(column, str) for column in grain)
            ):
                resolved.append((anchor, tuple(grain)))
        return resolved

    metric_grains = (
        (metric_name, resolved_grains(raw_metric))
        for metric_name, raw_metric in metrics.items()
        if isinstance(raw_metric, Mapping)
    )
    _metric_grain_issues(metric_grains, collector)


# ---------------------------------------------------------------------------
# Model-oriented rule helpers (operate on typed SemanticLayer values)
# ---------------------------------------------------------------------------


def _validate_layer_identity_model(layer: SemanticLayer, collector: _Collector) -> None:
    """Validate the layer's schema version and name identifier."""
    if type(layer.version) is not int or layer.version != 1:
        collector.add(
            "version", "expected schema version 1", "catalog.version.unsupported"
        )
    if not _is_valid_identifier(layer.name):
        collector.add("name", "name must match [a-z][a-z0-9_]*")


def _validate_named_models(
    section: str,
    mapping: Mapping[str, Any],
    model_type: type,
    collector: _Collector,
) -> None:
    """Validate that each collection key is an identifier of the right type."""
    singular = section.removesuffix("s")
    for key, value in mapping.items():
        if not _is_valid_identifier(key):
            collector.add(
                f"{section}.{key}",
                f"{singular} key '{key}' must match [a-z][a-z0-9_]*",
            )
        if not isinstance(value, model_type):
            collector.add(
                f"{section}.{key}",
                f"{singular} must be a {model_type.__name__}",
            )


def _typed_view(mapping: Mapping[str, Any], model_type: type) -> dict[str, Any]:
    """Return only entries whose values are instances of ``model_type``.

    Excludes malformed programmatic collection entries (already reported by
    :func:`_validate_named_models`) so the per-object and cross-collection
    validators never dereference an unexpected value type and raise
    ``AttributeError`` instead of a coded static diagnostic.
    """
    return {
        key: value for key, value in mapping.items() if isinstance(value, model_type)
    }


def _validate_source_model(source: DataSource, collector: _Collector) -> None:
    """Validate a typed data source's grain declarations."""
    _validate_grain_columns(source.name, source.grain, source.schema, collector)


def _validate_dimension_model(
    dimension: Dimension,
    data_sources: Mapping[str, DataSource],
    collector: _Collector,
) -> None:
    """Validate a typed dimension's source, column, and declared data_type."""
    base = f"dimensions.{dimension.name}"
    if dimension.source not in data_sources:
        collector.add(
            f"{base}.source",
            f"source '{dimension.source}' is not a known data source",
        )
        return
    source_schemas = {name: src.schema for name, src in data_sources.items()}
    field = _schema_field(source_schemas, dimension.source, dimension.column)
    if field is None:
        collector.add(
            f"{base}.column",
            f"column {dimension.column!r} is not declared in source "
            f"{dimension.source!r} schema",
        )
        return
    if not _data_type_compatible(dimension.data_type, field.type):
        collector.add(
            f"{base}.data_type",
            f"data_type {dimension.data_type!r} is not compatible with column "
            f"{dimension.column!r} type",
        )


def _validate_fact_model(
    fact: Fact,
    data_sources: Mapping[str, DataSource],
    collector: _Collector,
) -> None:
    """Validate a typed fact's source and expression references/types.

    Mirrors the raw loader's :func:`_validate_facts` so loaded and
    programmatic layers report identical diagnostics: the shared
    :func:`~selayer.expressions.validation.validate_row_expression` flags
    unknown source symbols and row-function arity mismatches, while the column
    and data_type checks reuse ``_check_column_exists``/``_check_column_type``
    exactly as the YAML path does.
    """
    base = f"facts.{fact.name}"
    if fact.source not in data_sources:
        collector.add(
            f"{base}.source",
            f"source '{fact.source}' is not a known data source",
        )
    source_schemas = {name: src.schema for name, src in data_sources.items()}
    known_sources = frozenset(data_sources)
    # Symbol-environment parity with the raw loader: the same shared helper
    # reports unknown sources and function arity mismatches in the expression.
    for message in validate_row_expression(fact.expression, known_sources):
        collector.add(f"{base}.expression", message)
    # Every resolvable source.column reference must point at a declared column.
    ref_columns = [r for r in references(fact.expression) if len(r.parts) == 2]
    for reference in ref_columns:
        ref_source, ref_column = reference.parts
        if ref_source in source_schemas:
            _check_column_exists(
                source_schemas,
                ref_source,
                ref_column,
                f"{base}.expression",
                collector,
            )
    # A simple single-column fact's declared data_type must be compatible with
    # the referenced column's logical type; arithmetic/function expressions
    # have no single source column and are not type-checked here.
    if len(ref_columns) == 1 and ref_columns[0].parts[0] in source_schemas:
        ref_source, ref_column = ref_columns[0].parts
        _check_column_type(
            source_schemas,
            ref_source,
            ref_column,
            fact.data_type,
            f"{base}.data_type",
            collector,
        )


def _validate_measure_model(
    measure: Measure,
    facts: Mapping[str, Fact],
    collector: _Collector,
) -> None:
    """Validate a typed measure's fact reference and aggregation type."""
    base = f"measures.{measure.name}"
    fact = facts.get(measure.fact)
    if measure.fact not in facts:
        collector.add(f"{base}.fact", f"fact '{measure.fact}' is not a known fact")
    if measure.aggregation not in _AGGREGATIONS:
        collector.add(
            f"{base}.aggregation",
            f"unsupported aggregation '{measure.aggregation}'",
        )
    if fact is not None:
        _validate_measure_aggregation(
            measure.name, measure.aggregation, fact.data_type, collector
        )


def _validate_metric_model(
    metric: Metric,
    measures: Mapping[str, Measure],
    collector: _Collector,
) -> None:
    """Validate a typed metric's declared measures and expression."""
    base = f"metrics.{metric.name}"
    for measure_id in metric.measures:
        if measure_id not in measures:
            collector.add(
                f"{base}.measures",
                f"measure '{measure_id}' is not a known measure",
            )
    declared = frozenset(metric.measures)
    for message in validate_metric_expression(metric.expression, declared):
        collector.add(f"{base}.expression", message)


def _validate_relationship_model(
    relationship: Relationship,
    data_sources: Mapping[str, DataSource],
    collector: _Collector,
) -> None:
    """Validate a typed relationship's endpoints, columns, and join types."""
    base = f"relationships.{relationship.name}"
    source_schemas = {name: src.schema for name, src in data_sources.items()}
    if relationship.source not in data_sources:
        collector.add(
            f"{base}.source",
            f"source '{relationship.source}' is not a known data source",
        )
    if relationship.target not in data_sources:
        collector.add(
            f"{base}.target",
            f"source '{relationship.target}' is not a known data source",
        )
    if relationship.type not in _CARDINALITIES:
        collector.add(f"{base}.type", f"unsupported cardinality '{relationship.type}'")
    if (
        relationship.source in data_sources
        and _schema_field(
            source_schemas, relationship.source, relationship.source_column
        )
        is None
    ):
        collector.add(
            f"{base}.source_column",
            f"column {relationship.source_column!r} is not declared in source "
            f"{relationship.source!r} schema",
        )
    if (
        relationship.target in data_sources
        and _schema_field(
            source_schemas, relationship.target, relationship.target_column
        )
        is None
    ):
        collector.add(
            f"{base}.target_column",
            f"column {relationship.target_column!r} is not declared in source "
            f"{relationship.target!r} schema",
        )
    _validate_relationship_join(
        relationship.name,
        relationship.source,
        relationship.target,
        relationship.source_column,
        relationship.target_column,
        source_schemas,
        collector,
    )


def _validate_fact_reachability_model(
    facts: Mapping[str, Fact],
    relationships: Mapping[str, Relationship],
    data_sources: Mapping[str, DataSource],
    collector: _Collector,
) -> None:
    """Validate typed fact-expression reachability over the safe graph."""
    known_sources = frozenset(data_sources)
    graph = _safe_relationship_edges(
        (
            (relationship.source, relationship.target, relationship.type)
            for relationship in relationships.values()
        ),
        known_sources,
    )
    fact_entries = (
        (fact.name, fact.source, fact.expression) for fact in facts.values()
    )
    _fact_reachability_issues(fact_entries, graph, known_sources, collector)


def _validate_metric_grains_model(
    metrics: Mapping[str, Metric],
    measures: Mapping[str, Measure],
    facts: Mapping[str, Fact],
    data_sources: Mapping[str, DataSource],
    collector: _Collector,
) -> None:
    """Validate typed metric grain consistency across declared measures."""

    def resolved_grains(metric: Metric) -> list[tuple[str, tuple[str, ...]]]:
        resolved: list[tuple[str, tuple[str, ...]]] = []
        for measure_id in metric.measures:
            measure = measures.get(measure_id)
            if measure is None:
                continue
            fact = facts.get(measure.fact)
            if fact is None:
                continue
            source = data_sources.get(fact.source)
            if source is None:
                continue
            resolved.append((fact.source, source.grain))
        return resolved

    metric_grains = (
        (metric.name, resolved_grains(metric)) for metric in metrics.values()
    )
    _metric_grain_issues(metric_grains, collector)


def collect_model_issues(layer: SemanticLayer) -> tuple[CatalogIssue, ...]:
    """Return every declaration-rule issue for a typed ``SemanticLayer``.

    Mirrors the raw loader's validation on typed model values so both
    programmatic layers (used by :func:`verify_static`) and loaded catalogs
    report the same codes. Issues are sorted by ``(path, message)`` and the
    ``code`` is intentionally excluded from the sort key to match the loader.
    """
    collector = _Collector()
    _validate_layer_identity_model(layer, collector)
    _validate_named_models("data_sources", layer.data_sources, DataSource, collector)
    _validate_named_models("dimensions", layer.dimensions, Dimension, collector)
    _validate_named_models("facts", layer.facts, Fact, collector)
    _validate_named_models("measures", layer.measures, Measure, collector)
    _validate_named_models("metrics", layer.metrics, Metric, collector)
    _validate_named_models(
        "relationships", layer.relationships, Relationship, collector
    )
    # Well-typed views exclude malformed programmatic collection entries
    # (already reported by ``_validate_named_models``) so the per-object and
    # cross-collection validators below never dereference an unexpected value
    # type and raise ``AttributeError`` instead of a coded static diagnostic.
    sources = _typed_view(layer.data_sources, DataSource)
    dimensions = _typed_view(layer.dimensions, Dimension)
    facts = _typed_view(layer.facts, Fact)
    measures = _typed_view(layer.measures, Measure)
    metrics = _typed_view(layer.metrics, Metric)
    relationships = _typed_view(layer.relationships, Relationship)
    for source in sources.values():
        _validate_source_model(source, collector)
    for dimension in dimensions.values():
        _validate_dimension_model(dimension, sources, collector)
    for fact in facts.values():
        _validate_fact_model(fact, sources, collector)
    for measure in measures.values():
        _validate_measure_model(measure, facts, collector)
    for metric in metrics.values():
        _validate_metric_model(metric, measures, collector)
    for relationship in relationships.values():
        _validate_relationship_model(relationship, sources, collector)
    _validate_fact_reachability_model(facts, relationships, sources, collector)
    _validate_metric_grains_model(metrics, measures, facts, sources, collector)
    return tuple(
        sorted(collector.issues, key=lambda issue: (issue.path, issue.message))
    )


# ---------------------------------------------------------------------------
# Model construction (only reached when there are no issues)
# ---------------------------------------------------------------------------


def _build_layer(
    data: Mapping[str, Any],
    parsed_sources: Mapping[str, ParsedSource],
    fact_expressions: Mapping[str, Expression],
    metric_expressions: Mapping[str, Expression],
) -> SemanticLayer:
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
                    name=parsed.name,
                    connector=parsed.connector,
                    schema=parsed.schema,
                    grain=parsed.grain,
                    **_metadata_kwargs(
                        raw if isinstance(raw, Mapping) else {},
                        f"data_sources.{key}",
                    ),
                )
                for key, parsed in parsed_sources.items()
                for raw in (_collection(data, "data_sources").get(key),)
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
                    **_metadata_kwargs(raw, f"dimensions.{key}"),
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
                    **_metadata_kwargs(raw, f"facts.{key}"),
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
                    **_metadata_kwargs(raw, f"measures.{key}"),
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
                    **_metadata_kwargs(raw, f"metrics.{key}"),
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
                    **_metadata_kwargs(raw, f"relationships.{key}"),
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
    collector = _Collector()
    try:
        text = Path(path).read_text(encoding="utf-8")
        node, data = _compose_and_construct(text)
    except yaml.YAMLError:
        # PyYAML's diagnostic echoes the offending source line(s) verbatim, so
        # ``str(error)`` can carry credentials, authenticated locations, or
        # other secrets from the file into a ``CatalogIssue``/verification
        # report. Report a fixed, secret-safe domain message instead and never
        # interpolate the parser's text. The code stays on the default
        # ``catalog.invalid`` so catalog error behavior is unchanged.
        collector.add("", "catalog file is not valid YAML")
        collector.raise_if_any()
        raise AssertionError("unreachable")
    except (TypeError, ValueError):
        # Structural construction failures are reported with a fixed
        # secret-safe message; the exception text is never interpolated
        # because it could carry source values from the document.
        collector.add("", "catalog file has an invalid structure")
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
    known_measures = frozenset(measures)

    fact_expressions: dict[str, Expression] = {}
    metric_expressions: dict[str, Expression] = {}

    parsed_sources = _validate_data_sources(sources, Path(path), collector)
    source_schemas: dict[str, TableSchema] = {
        name: parsed.schema for name, parsed in parsed_sources.items()
    }

    _validate_dimensions(dimensions, known_sources, source_schemas, collector)
    _validate_facts(facts, known_sources, source_schemas, collector, fact_expressions)
    _validate_measures(measures, facts, collector)
    _validate_metrics(metrics, known_measures, collector, metric_expressions)
    _validate_relationships(relationships, known_sources, source_schemas, collector)
    _validate_metric_grains(metrics, measures, facts, sources, collector)
    _validate_fact_expression_reachability(
        facts,
        sources,
        relationships,
        fact_expressions,
        known_sources,
        collector,
    )

    collector.raise_if_any()
    return _build_layer(catalog, parsed_sources, fact_expressions, metric_expressions)


__all__ = [
    "CatalogIssue",
    "CatalogValidationError",
    "SemanticLayer",
    "SemanticObject",
    "load",
]
