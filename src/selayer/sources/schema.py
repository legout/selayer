"""Recursive Arrow-compatible logical schema model.

This module owns the immutable, recursive logical-type model that every data
source adapter resolves against.  It parses schema documents, validates them
into deterministic ``SchemaIssue`` aggregates, converts to and from
``pyarrow.Schema``, compares declared and observed schemas, and produces stable
SHA-256 fingerprints.

The scalar-name set is closed.  Recursive nodes (decimal, timestamp, time,
duration, interval, fixed-size binary, list, large list, fixed-size list,
struct, map, dictionary) use one discriminated mapping per type.  Every public
operation sanitizes its inputs: malformed documents surface as
``SchemaDefinitionError`` issues rather than leaked ``KeyError``/
``TypeError``/``yaml.YAMLError``/PyArrow errors.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeGuard

import pyarrow as pa
import pyarrow.types as patypes

__all__ = [
    "SCALAR_NAMES",
    "DecimalType",
    "DictionaryType",
    "DurationType",
    "FieldSchema",
    "FixedSizeBinaryType",
    "FixedSizeListType",
    "IntervalType",
    "LargeListType",
    "ListType",
    "LogicalType",
    "MapType",
    "ScalarType",
    "SchemaDefinitionError",
    "SchemaIssue",
    "SchemaMismatch",
    "StructType",
    "TableSchema",
    "TimeType",
    "TimestampType",
    "compare_schemas",
    "parse_schema_document",
    "schema_fingerprint",
    "table_schema_from_arrow",
    "table_schema_to_arrow",
    "validate_schema_document",
]

# ---------------------------------------------------------------------------
# Closed scalar-name set and logical-form constants
# ---------------------------------------------------------------------------

SCALAR_NAMES = frozenset(
    {
        "null",
        "boolean",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float16",
        "float32",
        "float64",
        "utf8",
        "large_utf8",
        "binary",
        "large_binary",
        "date32",
        "date64",
    }
)

_SIGNED_INT_SCALARS = frozenset({"int8", "int16", "int32", "int64"})
_TIMESTAMP_UNITS = frozenset({"s", "ms", "us", "ns"})
_TIME_UNITS_BY_BIT_WIDTH: Mapping[int, frozenset[str]] = {
    32: frozenset({"s", "ms"}),
    64: frozenset({"us", "ns"}),
}
_DURATION_UNITS = frozenset({"s", "ms", "us", "ns"})
_INTERVAL_VARIANTS = frozenset({"year_month", "day_time", "month_day_nano"})
_INTERVAL_VARIANTS_WITH_ARROW_SUPPORT = frozenset({"month_day_nano"})

# ``field_id`` is a connector-native integer carried on a field.  It is folded
# into the immutable ``FieldSchema.metadata`` mapping under this reserved key so
# the public dataclass signature stays a four-field mapping while comparison
# and fingerprinting still observe it.
_FIELD_ID_METADATA_KEY = "selayer.field_id"

# Maximum decimal precision across every Arrow decimal width (decimal256).
_MAX_DECIMAL_PRECISION = 76


def _is_int(value: object) -> TypeGuard[int]:
    """Return ``True`` for genuine integers, rejecting Python booleans."""

    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _issue(code: str, path: str, message: str) -> SchemaIssue:
    return SchemaIssue(code, path, message)


def _get(mapping: Mapping[str, object], key: str) -> tuple[object, bool]:
    """Return ``(value, present)`` for a mapping key without leaking KeyError."""

    if key in mapping:
        return mapping[key], True
    return None, False


def _sorted_unknown_keys(
    raw: Mapping[str, object], allowed: Container[str]
) -> list[str]:
    """Return unknown mapping keys deterministically ordered by ``str``.

    Schema documents are untrusted mappings and may carry heterogeneous key
    types (e.g. ``{str, int}``).  ``sorted()`` over such a set raises
    ``TypeError`` because ``str`` and ``int`` are not mutually orderable, which
    would leak out of validation.  We never compare raw keys directly: each key
    is projected through ``str()`` first, then ordered and de-duplicated, so the
    result is total and deterministic regardless of the input key types.
    """

    seen: set[str] = set()
    unknown: list[str] = []
    for key in raw:
        if key in allowed:
            continue
        text = str(key)
        if text not in seen:
            seen.add(text)
            unknown.append(text)
    unknown.sort()
    return unknown


# ---------------------------------------------------------------------------
# Logical type model (frozen, slotted, structural equality)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScalarType:
    """A leaf logical type identified by a closed scalar name."""

    name: str


@dataclass(frozen=True, slots=True)
class DecimalType:
    precision: int
    scale: int


@dataclass(frozen=True, slots=True)
class TimestampType:
    unit: str
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class TimeType:
    """A logical time type carrying an Arrow-valid ``(unit, bit_width)`` pair.

    ``bit_width`` 32 admits units ``{s, ms}``; ``bit_width`` 64 admits
    ``{us, ns}``.  The pair is validated so the model never describes a time
    type that Arrow cannot represent.
    """

    unit: str
    bit_width: int


@dataclass(frozen=True, slots=True)
class DurationType:
    unit: str


@dataclass(frozen=True, slots=True)
class IntervalType:
    """A logical interval type.

    The Arrow columnar spec defines three interval variants; only
    ``month_day_nano`` is representable by the installed PyArrow build, so the
    other two are accepted by the document model and rejected at Arrow
    conversion with a deterministic ``SchemaDefinitionError``.
    """

    variant: str


@dataclass(frozen=True, slots=True)
class FixedSizeBinaryType:
    byte_width: int


@dataclass(frozen=True, slots=True)
class ListType:
    element: FieldSchema


@dataclass(frozen=True, slots=True)
class LargeListType:
    element: FieldSchema


@dataclass(frozen=True, slots=True)
class FixedSizeListType:
    element: FieldSchema
    size: int


@dataclass(frozen=True, slots=True)
class StructType:
    fields: tuple[FieldSchema, ...]

    def __post_init__(self) -> None:
        # Coerce any iterable (e.g. a mutable list handed by an untyped
        # caller) to an immutable tuple so the frozen schema cannot be mutated
        # by later changes to the source collection.  Mirrors the defensive
        # ``FieldSchema.metadata`` coercion.
        if not isinstance(self.fields, tuple):
            object.__setattr__(self, "fields", tuple(self.fields))


@dataclass(frozen=True, slots=True)
class MapType:
    key: FieldSchema
    value: FieldSchema


@dataclass(frozen=True, slots=True)
class DictionaryType:
    index: ScalarType
    value: ScalarType
    ordered: bool = False


# Forward-reference-friendly union of every logical type variant.
LogicalType = (
    ScalarType
    | DecimalType
    | TimestampType
    | TimeType
    | DurationType
    | IntervalType
    | FixedSizeBinaryType
    | ListType
    | LargeListType
    | FixedSizeListType
    | StructType
    | MapType
    | DictionaryType
)


@dataclass(frozen=True, slots=True)
class FieldSchema:
    name: str
    type: LogicalType
    nullable: bool
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An Arrow ``null`` field can only hold nulls, so it is always nullable;
        # PyArrow rejects a non-nullable null field.  Enforce the invariant at
        # the model boundary so parsed and hand-constructed fields agree and the
        # Arrow round trip is always defined.
        if isinstance(self.type, ScalarType) and self.type.name == "null":
            object.__setattr__(self, "nullable", True)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def field_id(self) -> str | None:
        """Connector-native field identifier preserved as metadata, if declared."""

        return self.metadata.get(_FIELD_ID_METADATA_KEY)


@dataclass(frozen=True, slots=True)
class TableSchema:
    fields: tuple[FieldSchema, ...]

    def __post_init__(self) -> None:
        # Coerce any iterable (e.g. a mutable list handed by an untyped
        # caller) to an immutable tuple so the frozen schema cannot be mutated
        # by later changes to the source collection.
        if not isinstance(self.fields, tuple):
            object.__setattr__(self, "fields", tuple(self.fields))

    def field(self, name: str) -> FieldSchema:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(name)


# ---------------------------------------------------------------------------
# Issue, mismatch, and error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class SchemaMismatch:
    code: str
    path: str
    message: str


class SchemaDefinitionError(ValueError):
    """Raised when a schema document cannot be resolved into a TableSchema."""

    def __init__(self, issues: tuple[SchemaIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        )


# ---------------------------------------------------------------------------
# Document validation and parsing
# ---------------------------------------------------------------------------

# Allowed keys for the top-level table document.
_TABLE_KEYS = frozenset({"fields"})
# Allowed keys for a named field mapping.
_FIELD_KEYS = frozenset({"name", "type", "nullable", "metadata", "field_id"})
# Allowed keys for an unnamed entry (list element / map key / map value).
_ENTRY_KEYS = frozenset({"type", "nullable", "metadata", "field_id"})


def validate_schema_document(
    raw: object, path: str = "schema"
) -> tuple[SchemaIssue, ...]:
    """Validate a schema document, returning deterministic sorted issues."""

    issues = _validate_table(raw, path)
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


def parse_schema_document(raw: object) -> TableSchema:
    """Parse and validate a schema document into a ``TableSchema``.

    Raises ``SchemaDefinitionError`` with sorted issues for any malformed
    document.  Never leaks ``KeyError``, ``TypeError``, ``yaml.YAMLError``, or
    PyArrow conversion errors.
    """

    issues = validate_schema_document(raw, "schema")
    if issues:
        raise SchemaDefinitionError(issues)
    return _build_table(raw, "schema")


def _validate_table(raw: object, path: str) -> list[SchemaIssue]:
    if not isinstance(raw, Mapping):
        return [_issue("schema_not_mapping", path, "schema must be a mapping")]
    issues: list[SchemaIssue] = []
    unknown = _sorted_unknown_keys(raw, _TABLE_KEYS)
    if unknown:
        issues.append(
            _issue(
                "unknown_key",
                f"{path}.{unknown[0]}",
                f"unknown key {unknown[0]!r}",
            )
        )
    fields_path = f"{path}.fields"
    fields_raw, present = _get(raw, "fields")
    if not present:
        issues.append(_issue("missing_fields", fields_path, "fields is required"))
        return issues
    if not isinstance(fields_raw, list):
        issues.append(
            _issue("fields_not_list", fields_path, "fields must be a list of mappings")
        )
        return issues
    if not fields_raw:
        issues.append(_issue("empty_fields", fields_path, "fields must be non-empty"))
        return issues
    seen: set[str] = set()
    for index, field_raw in enumerate(fields_raw):
        field_path = f"{fields_path}[{index}]"
        issues.extend(_validate_field(field_raw, field_path))
        if isinstance(field_raw, Mapping):
            name = field_raw.get("name")
            if _is_str(name) and name:
                if name in seen:
                    issues.append(
                        _issue(
                            "duplicate_field",
                            f"{field_path}.name",
                            f"field name {name!r} is duplicated",
                        )
                    )
                else:
                    seen.add(name)
    return issues


def _validate_field(raw: object, path: str) -> list[SchemaIssue]:
    if not isinstance(raw, Mapping):
        return [_issue("field_not_mapping", path, "field must be a mapping")]
    return _validate_keys_and_entry(raw, path, _FIELD_KEYS, require_name=True)


def _validate_entry(raw: object, path: str) -> list[SchemaIssue]:
    """Validate an unnamed nested entry (list element / map key / map value)."""

    if not isinstance(raw, Mapping):
        return [_issue("entry_not_mapping", path, "entry must be a mapping")]
    return _validate_keys_and_entry(raw, path, _ENTRY_KEYS, require_name=False)


def _validate_keys_and_entry(
    raw: Mapping[str, object],
    path: str,
    allowed: frozenset[str],
    *,
    require_name: bool,
) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    unknown = _sorted_unknown_keys(raw, allowed)
    if unknown:
        issues.append(
            _issue(
                "unknown_key",
                f"{path}.{unknown[0]}",
                f"unknown key {unknown[0]!r}",
            )
        )

    name_raw, name_present = _get(raw, "name")
    name_path = f"{path}.name"
    if require_name:
        if not name_present:
            issues.append(_issue("name_missing", name_path, "name is required"))
        elif not _is_str(name_raw):
            issues.append(_issue("name_invalid", name_path, "name must be a string"))
        elif not name_raw:
            issues.append(_issue("name_invalid", name_path, "name must be non-empty"))

    type_raw, type_present = _get(raw, "type")
    type_path = f"{path}.type"
    if not type_present:
        issues.append(_issue("type_missing", type_path, "type is required"))
    else:
        issues.extend(_validate_type(type_raw, type_path))

    nullable_raw, nullable_present = _get(raw, "nullable")
    nullable_path = f"{path}.nullable"
    if not nullable_present:
        issues.append(_issue("nullable_missing", nullable_path, "nullable is required"))
    elif not isinstance(nullable_raw, bool):
        issues.append(
            _issue("nullable_invalid", nullable_path, "nullable must be a boolean")
        )

    metadata_raw, metadata_present = _get(raw, "metadata")
    if metadata_present:
        issues.extend(_validate_metadata(metadata_raw, f"{path}.metadata"))

    field_id_raw, field_id_present = _get(raw, "field_id")
    if field_id_present and not _is_int(field_id_raw):
        issues.append(
            _issue(
                "field_id_invalid", f"{path}.field_id", "field_id must be an integer"
            )
        )
    return issues


def _validate_metadata(raw: object, path: str) -> list[SchemaIssue]:
    if not isinstance(raw, Mapping):
        return [_issue("metadata_invalid", path, "metadata must be a mapping")]
    issues: list[SchemaIssue] = []
    for key, value in raw.items():
        if not _is_str(key):
            issues.append(
                _issue("metadata_key_invalid", path, "metadata keys must be strings")
            )
            break
        if not _is_str(value):
            issues.append(
                _issue(
                    "metadata_value_invalid",
                    f"{path}.{key}",
                    f"metadata value for {key!r} must be a string",
                )
            )
    return issues


def _validate_type(raw: object, path: str) -> list[SchemaIssue]:
    if _is_str(raw):
        if raw not in SCALAR_NAMES:
            return [_issue("unknown_scalar", path, f"unknown scalar type {raw!r}")]
        return []
    if not isinstance(raw, Mapping):
        return [_issue("type_invalid", path, "type must be a scalar name or mapping")]
    if len(raw) != 1:
        return [
            _issue(
                "type_invalid",
                path,
                "type mapping must have exactly one discriminator key",
            )
        ]
    (discriminator, body) = next(iter(raw.items()))
    if not _is_str(discriminator):
        return [_issue("type_invalid", path, "type discriminator must be a string")]
    body_path = f"{path}.{discriminator}"
    handler = _TYPE_VALIDATORS.get(discriminator)
    if handler is None:
        return [
            _issue(
                "unknown_type",
                path,
                f"unknown type discriminator {discriminator!r}",
            )
        ]
    return handler(body, body_path)


def _validate_mapping_body(
    body: object, body_path: str, discriminator: str
) -> list[SchemaIssue]:
    if not isinstance(body, Mapping):
        return [
            _issue(
                f"{discriminator}_invalid",
                body_path,
                f"{discriminator} must be a mapping",
            )
        ]
    return []


def _reject_unknown_keys(
    body: Mapping[str, object],
    body_path: str,
    discriminator: str,
    allowed: Container[str],
) -> list[SchemaIssue]:
    unknown = _sorted_unknown_keys(body, allowed)
    if unknown:
        return [
            _issue(
                f"{discriminator}_unknown_key",
                f"{body_path}.{unknown[0]}",
                f"unknown {discriminator} key {unknown[0]!r}",
            )
        ]
    return []


def _validate_decimal(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "decimal")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(
        _reject_unknown_keys(body, body_path, "decimal", {"precision", "scale"})
    )
    precision_raw, precision_present = _get(body, "precision")
    precision_path = f"{body_path}.precision"
    if not precision_present:
        issues.append(
            _issue("decimal_precision_missing", precision_path, "precision is required")
        )
    elif not _is_int(precision_raw):
        issues.append(
            _issue(
                "decimal_precision_invalid",
                precision_path,
                "precision must be an integer",
            )
        )
    elif not (1 <= precision_raw <= _MAX_DECIMAL_PRECISION):
        issues.append(
            _issue(
                "decimal_precision_invalid",
                precision_path,
                f"precision must be between 1 and {_MAX_DECIMAL_PRECISION}",
            )
        )

    scale_raw, scale_present = _get(body, "scale")
    scale_path = f"{body_path}.scale"
    if not scale_present:
        issues.append(_issue("decimal_scale_missing", scale_path, "scale is required"))
    elif not _is_int(scale_raw):
        issues.append(
            _issue("decimal_scale_invalid", scale_path, "scale must be an integer")
        )
    elif _is_int(precision_raw) and not (0 <= scale_raw <= precision_raw):
        issues.append(
            _issue(
                "decimal_scale_invalid",
                scale_path,
                "scale must be between 0 and precision",
            )
        )
    return issues


def _validate_timestamp(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "timestamp")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(
        _reject_unknown_keys(body, body_path, "timestamp", {"unit", "timezone"})
    )
    unit_raw, unit_present = _get(body, "unit")
    unit_path = f"{body_path}.unit"
    if not unit_present:
        issues.append(_issue("timestamp_unit_missing", unit_path, "unit is required"))
    elif not _is_str(unit_raw):
        issues.append(
            _issue("timestamp_unit_invalid", unit_path, "unit must be a string")
        )
    elif unit_raw not in _TIMESTAMP_UNITS:
        issues.append(
            _issue(
                "timestamp_unit_invalid",
                unit_path,
                f"unit must be one of {sorted(_TIMESTAMP_UNITS)}",
            )
        )
    timezone_raw, timezone_present = _get(body, "timezone")
    if timezone_present and timezone_raw is not None:
        timezone_path = f"{body_path}.timezone"
        if not _is_str(timezone_raw):
            issues.append(
                _issue(
                    "timestamp_timezone_invalid",
                    timezone_path,
                    "timezone must be a string or null",
                )
            )
    return issues


def _validate_time(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "time")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(_reject_unknown_keys(body, body_path, "time", {"unit", "bit_width"}))
    unit_raw, unit_present = _get(body, "unit")
    unit_path = f"{body_path}.unit"
    if not unit_present:
        issues.append(_issue("time_unit_missing", unit_path, "unit is required"))
    elif not _is_str(unit_raw):
        issues.append(_issue("time_unit_invalid", unit_path, "unit must be a string"))

    bit_width_raw, bit_width_present = _get(body, "bit_width")
    bit_width_path = f"{body_path}.bit_width"
    if not bit_width_present:
        issues.append(
            _issue("time_bit_width_missing", bit_width_path, "bit_width is required")
        )
    elif not _is_int(bit_width_raw):
        issues.append(
            _issue(
                "time_bit_width_invalid", bit_width_path, "bit_width must be an integer"
            )
        )
    elif bit_width_raw not in _TIME_UNITS_BY_BIT_WIDTH:
        issues.append(
            _issue(
                "time_bit_width_invalid",
                bit_width_path,
                f"bit_width must be one of {sorted(_TIME_UNITS_BY_BIT_WIDTH)}",
            )
        )

    if (
        _is_str(unit_raw)
        and _is_int(bit_width_raw)
        and bit_width_raw in _TIME_UNITS_BY_BIT_WIDTH
        and unit_raw not in _TIME_UNITS_BY_BIT_WIDTH[bit_width_raw]
    ):
        issues.append(
            _issue(
                "time_unit_width_mismatch",
                unit_path,
                f"unit {unit_raw!r} is invalid for bit_width {bit_width_raw}",
            )
        )
    return issues


def _validate_duration(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "duration")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(_reject_unknown_keys(body, body_path, "duration", {"unit"}))
    unit_raw, unit_present = _get(body, "unit")
    unit_path = f"{body_path}.unit"
    if not unit_present:
        issues.append(_issue("duration_unit_missing", unit_path, "unit is required"))
    elif not _is_str(unit_raw):
        issues.append(
            _issue("duration_unit_invalid", unit_path, "unit must be a string")
        )
    elif unit_raw not in _DURATION_UNITS:
        issues.append(
            _issue(
                "duration_unit_invalid",
                unit_path,
                f"unit must be one of {sorted(_DURATION_UNITS)}",
            )
        )
    return issues


def _validate_interval(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "interval")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(_reject_unknown_keys(body, body_path, "interval", {"variant"}))
    variant_raw, variant_present = _get(body, "variant")
    variant_path = f"{body_path}.variant"
    if not variant_present:
        issues.append(
            _issue("interval_variant_missing", variant_path, "variant is required")
        )
    elif not _is_str(variant_raw):
        issues.append(
            _issue("interval_variant_invalid", variant_path, "variant must be a string")
        )
    elif variant_raw not in _INTERVAL_VARIANTS:
        issues.append(
            _issue(
                "interval_variant_invalid",
                variant_path,
                f"variant must be one of {sorted(_INTERVAL_VARIANTS)}",
            )
        )
    return issues


def _validate_fixed_size_binary(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "fixed_size_binary")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(
        _reject_unknown_keys(body, body_path, "fixed_size_binary", {"byte_width"})
    )
    byte_width_raw, byte_width_present = _get(body, "byte_width")
    byte_width_path = f"{body_path}.byte_width"
    if not byte_width_present:
        issues.append(
            _issue(
                "fixed_size_binary_byte_width_missing",
                byte_width_path,
                "byte_width is required",
            )
        )
    elif not _is_int(byte_width_raw):
        issues.append(
            _issue(
                "fixed_size_binary_byte_width_invalid",
                byte_width_path,
                "byte_width must be an integer",
            )
        )
    elif byte_width_raw < 1:
        issues.append(
            _issue(
                "fixed_size_binary_byte_width_invalid",
                byte_width_path,
                "byte_width must be at least 1",
            )
        )
    return issues


def _validate_list_like(
    body: object, body_path: str, discriminator: str
) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, discriminator)
    if issues:
        return issues
    assert isinstance(body, Mapping)
    allowed = {"size", "element"} if discriminator == "fixed_size_list" else {"element"}
    issues.extend(_reject_unknown_keys(body, body_path, discriminator, allowed))
    if discriminator == "fixed_size_list":
        size_raw, size_present = _get(body, "size")
        size_path = f"{body_path}.size"
        if not size_present:
            issues.append(_issue("size_missing", size_path, "size is required"))
        elif not _is_int(size_raw):
            issues.append(_issue("size_invalid", size_path, "size must be an integer"))
        elif size_raw < 0:
            issues.append(_issue("size_invalid", size_path, "size must be at least 0"))
    element_raw, element_present = _get(body, "element")
    element_path = f"{body_path}.element"
    if not element_present:
        issues.append(_issue("element_missing", element_path, "element is required"))
    else:
        issues.extend(_validate_entry(element_raw, element_path))
    return issues


def _validate_struct(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "struct")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(_reject_unknown_keys(body, body_path, "struct", {"fields"}))
    fields_raw, fields_present = _get(body, "fields")
    fields_path = f"{body_path}.fields"
    if not fields_present:
        return [_issue("struct_fields_missing", fields_path, "fields is required")]
    if not isinstance(fields_raw, list):
        return [
            _issue(
                "struct_fields_invalid",
                fields_path,
                "fields must be a list of mappings",
            )
        ]
    if not fields_raw:
        return [_issue("struct_fields_empty", fields_path, "fields must be non-empty")]
    seen: set[str] = set()
    for index, field_raw in enumerate(fields_raw):
        field_path = f"{fields_path}[{index}]"
        issues.extend(_validate_field(field_raw, field_path))
        if isinstance(field_raw, Mapping):
            name = field_raw.get("name")
            if _is_str(name) and name:
                if name in seen:
                    issues.append(
                        _issue(
                            "duplicate_field",
                            f"{field_path}.name",
                            f"field name {name!r} is duplicated",
                        )
                    )
                else:
                    seen.add(name)
    return issues


def _validate_map(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "map")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(_reject_unknown_keys(body, body_path, "map", {"key", "value"}))
    key_raw, key_present = _get(body, "key")
    key_path = f"{body_path}.key"
    if not key_present:
        issues.append(_issue("map_key_missing", key_path, "key is required"))
    else:
        issues.extend(_validate_entry(key_raw, key_path))
        if isinstance(key_raw, Mapping):
            key_nullable = key_raw.get("nullable")
            if isinstance(key_nullable, bool) and key_nullable:
                issues.append(
                    _issue(
                        "map_key_nullable",
                        f"{key_path}.nullable",
                        "map key must be nullable: false",
                    )
                )
    value_raw, value_present = _get(body, "value")
    value_path = f"{body_path}.value"
    if not value_present:
        issues.append(_issue("map_value_missing", value_path, "value is required"))
    else:
        issues.extend(_validate_entry(value_raw, value_path))
    return issues


def _validate_dictionary(body: object, body_path: str) -> list[SchemaIssue]:
    issues = _validate_mapping_body(body, body_path, "dictionary")
    if issues:
        return issues
    assert isinstance(body, Mapping)
    issues.extend(
        _reject_unknown_keys(
            body, body_path, "dictionary", {"index", "value", "ordered"}
        )
    )
    index_raw, index_present = _get(body, "index")
    index_path = f"{body_path}.index"
    if not index_present:
        issues.append(
            _issue("dictionary_index_missing", index_path, "index is required")
        )
    elif not (_is_str(index_raw) and index_raw in _SIGNED_INT_SCALARS):
        issues.append(
            _issue(
                "dictionary_index_invalid",
                index_path,
                "index must be a signed integer scalar name",
            )
        )
    value_raw, value_present = _get(body, "value")
    value_path = f"{body_path}.value"
    if not value_present:
        issues.append(
            _issue("dictionary_value_missing", value_path, "value is required")
        )
    elif not (_is_str(value_raw) and value_raw in SCALAR_NAMES):
        issues.append(
            _issue(
                "dictionary_value_invalid",
                value_path,
                "value must be a scalar type name",
            )
        )
    ordered_raw, ordered_present = _get(body, "ordered")
    if ordered_present and not isinstance(ordered_raw, bool):
        issues.append(
            _issue(
                "dictionary_ordered_invalid",
                f"{body_path}.ordered",
                "ordered must be a boolean",
            )
        )
    return issues


_TYPE_VALIDATORS: Mapping[str, Callable[[object, str], list[SchemaIssue]]] = {
    "decimal": _validate_decimal,
    "timestamp": _validate_timestamp,
    "time": _validate_time,
    "duration": _validate_duration,
    "interval": _validate_interval,
    "fixed_size_binary": _validate_fixed_size_binary,
    "list": lambda body, body_path: _validate_list_like(body, body_path, "list"),
    "large_list": lambda body, body_path: _validate_list_like(
        body, body_path, "large_list"
    ),
    "fixed_size_list": lambda body, body_path: _validate_list_like(
        body, body_path, "fixed_size_list"
    ),
    "struct": _validate_struct,
    "map": _validate_map,
    "dictionary": _validate_dictionary,
}


# ---------------------------------------------------------------------------
# Construction pass (only runs after validation reports zero issues)
# ---------------------------------------------------------------------------


def _build_table(raw: object, path: str) -> TableSchema:
    assert isinstance(raw, Mapping)  # pragma: no cover - validated upstream
    fields_raw = raw["fields"]
    assert isinstance(fields_raw, list) and fields_raw  # pragma: no cover
    fields = tuple(
        _build_field(field_raw, f"{path}.fields[{index}]")
        for index, field_raw in enumerate(fields_raw)
    )
    return TableSchema(fields)


def _collect_metadata(raw: Mapping[str, object]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    metadata_raw, metadata_present = _get(raw, "metadata")
    if metadata_present and isinstance(metadata_raw, Mapping):
        for key, value in metadata_raw.items():
            if _is_str(key) and _is_str(value):
                metadata[key] = value
    field_id_raw, field_id_present = _get(raw, "field_id")
    if field_id_present and _is_int(field_id_raw):
        metadata[_FIELD_ID_METADATA_KEY] = str(field_id_raw)
    return metadata


def _build_field(raw: object, path: str) -> FieldSchema:
    assert isinstance(raw, Mapping)  # pragma: no cover
    name = raw["name"]
    assert _is_str(name) and name  # pragma: no cover
    logical = _build_type(raw["type"], f"{path}.type")
    nullable = raw["nullable"]
    assert isinstance(nullable, bool)  # pragma: no cover
    return FieldSchema(name, logical, nullable, _collect_metadata(raw))


def _build_entry(raw: object, path: str, implied_name: str) -> FieldSchema:
    assert isinstance(raw, Mapping)  # pragma: no cover
    logical = _build_type(raw["type"], f"{path}.type")
    nullable = raw["nullable"]
    assert isinstance(nullable, bool)  # pragma: no cover
    return FieldSchema(implied_name, logical, nullable, _collect_metadata(raw))


def _build_type(raw: object, path: str) -> LogicalType:
    if _is_str(raw):
        return ScalarType(raw)
    assert isinstance(raw, Mapping) and len(raw) == 1  # pragma: no cover
    (discriminator, body) = next(iter(raw.items()))
    body_path = f"{path}.{discriminator}"
    assert isinstance(body, Mapping)  # pragma: no cover
    if discriminator == "decimal":
        return DecimalType(int(body["precision"]), int(body["scale"]))
    if discriminator == "timestamp":
        timezone = body.get("timezone")
        return TimestampType(
            str(body["unit"]), None if timezone is None else str(timezone)
        )
    if discriminator == "time":
        return TimeType(str(body["unit"]), int(body["bit_width"]))
    if discriminator == "duration":
        return DurationType(str(body["unit"]))
    if discriminator == "interval":
        return IntervalType(str(body["variant"]))
    if discriminator == "fixed_size_binary":
        return FixedSizeBinaryType(int(body["byte_width"]))
    if discriminator == "list":
        return ListType(
            _build_entry(body["element"], f"{body_path}.element", "element")
        )
    if discriminator == "large_list":
        return LargeListType(
            _build_entry(body["element"], f"{body_path}.element", "element")
        )
    if discriminator == "fixed_size_list":
        return FixedSizeListType(
            _build_entry(body["element"], f"{body_path}.element", "element"),
            int(body["size"]),
        )
    if discriminator == "struct":
        fields = tuple(
            _build_field(field_raw, f"{body_path}.fields[{index}]")
            for index, field_raw in enumerate(body["fields"])
        )
        return StructType(fields)
    if discriminator == "map":
        return MapType(
            _build_entry(body["key"], f"{body_path}.key", "key"),
            _build_entry(body["value"], f"{body_path}.value", "value"),
        )
    if discriminator == "dictionary":
        ordered = body.get("ordered", False)
        return DictionaryType(
            ScalarType(str(body["index"])),
            ScalarType(str(body["value"])),
            bool(ordered),
        )
    raise AssertionError(  # pragma: no cover - exhaustive
        f"unhandled type discriminator {discriminator!r}"
    )


# ---------------------------------------------------------------------------
# Canonical fingerprint
# ---------------------------------------------------------------------------


def _canonical_logical(logical: LogicalType) -> object:
    if isinstance(logical, ScalarType):
        return {"scalar": logical.name}
    if isinstance(logical, DecimalType):
        return {"decimal": {"precision": logical.precision, "scale": logical.scale}}
    if isinstance(logical, TimestampType):
        return {"timestamp": {"unit": logical.unit, "timezone": logical.timezone}}
    if isinstance(logical, TimeType):
        return {"time": {"unit": logical.unit, "bit_width": logical.bit_width}}
    if isinstance(logical, DurationType):
        return {"duration": {"unit": logical.unit}}
    if isinstance(logical, IntervalType):
        return {"interval": {"variant": logical.variant}}
    if isinstance(logical, FixedSizeBinaryType):
        return {"fixed_size_binary": {"byte_width": logical.byte_width}}
    if isinstance(logical, ListType):
        return {"list": {"element": _canonical_field(logical.element)}}
    if isinstance(logical, LargeListType):
        return {"large_list": {"element": _canonical_field(logical.element)}}
    if isinstance(logical, FixedSizeListType):
        return {
            "fixed_size_list": {
                "size": logical.size,
                "element": _canonical_field(logical.element),
            }
        }
    if isinstance(logical, StructType):
        return {
            "struct": {"fields": [_canonical_field(item) for item in logical.fields]}
        }
    if isinstance(logical, MapType):
        return {
            "map": {
                "key": _canonical_field(logical.key),
                "value": _canonical_field(logical.value),
            }
        }
    if isinstance(logical, DictionaryType):
        return {
            "dictionary": {
                "index": logical.index.name,
                "value": logical.value.name,
                "ordered": logical.ordered,
            }
        }
    raise AssertionError(f"unhandled logical type {logical!r}")  # pragma: no cover


def _canonical_field(field: FieldSchema) -> object:
    return {
        "name": field.name,
        "type": _canonical_logical(field.type),
        "nullable": field.nullable,
        "metadata": dict(field.metadata),
    }


def schema_fingerprint(schema: TableSchema) -> str:
    """Return a deterministic SHA-256 fingerprint of a schema.

    The canonical payload preserves field order while sorting keys, uses compact
    JSON separators, forbids NaN/Infinity, and encodes as UTF-8.
    """

    canonical = {"fields": [_canonical_field(field) for field in schema.fields]}
    payload = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Arrow conversion
# ---------------------------------------------------------------------------

_SCALAR_ARROW: Mapping[str, pa.DataType] = {
    "null": pa.null(),
    "boolean": pa.bool_(),
    "int8": pa.int8(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "uint8": pa.uint8(),
    "uint16": pa.uint16(),
    "uint32": pa.uint32(),
    "uint64": pa.uint64(),
    "float16": pa.float16(),
    "float32": pa.float32(),
    "float64": pa.float64(),
    "utf8": pa.utf8(),
    "large_utf8": pa.large_utf8(),
    "binary": pa.binary(),
    "large_binary": pa.large_binary(),
    "date32": pa.date32(),
    "date64": pa.date64(),
}

_ARROW_SCALAR_TO_NAME: Mapping[pa.DataType, str] = {
    arrow_type: name for name, arrow_type in _SCALAR_ARROW.items()
}

# Logical types whose document model is valid but which the installed PyArrow
# build cannot represent (currently: ``year_month`` and ``day_time`` intervals).
_INTERVAL_VARIANT_TO_ARROW: Mapping[str, pa.DataType | None] = {
    "month_day_nano": pa.month_day_nano_interval(),
    "year_month": None,
    "day_time": None,
}

_CONVERT_ERRORS = (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError)


def _field_metadata_to_arrow(
    metadata: Mapping[str, str],
) -> dict[bytes, bytes] | None:
    if not metadata:
        return None
    return {
        key.encode("utf-8"): value.encode("utf-8") for key, value in metadata.items()
    }


def _decode_arrow_metadata_entry(raw: object, path: str) -> str:
    """Decode an Arrow metadata key/value, surfacing invalid UTF-8 safely.

    Arrow field metadata is a ``dict[bytes, bytes]``.  A non-UTF-8 entry would
    otherwise leak as ``UnicodeDecodeError``; instead it is reported as a
    deterministic ``SchemaDefinitionError`` with code ``arrow_metadata_invalid``
    and the supplied ``path`` (which points at ``…metadata`` for a key failure
    or ``…metadata.<key>`` for a value failure).
    """

    try:
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)
    except UnicodeDecodeError:
        raise SchemaDefinitionError(
            (
                _issue(
                    "arrow_metadata_invalid",
                    path,
                    "Arrow field metadata is not valid UTF-8",
                ),
            )
        ) from None


def _arrow_metadata_to_field(metadata: object, path: str) -> Mapping[str, str]:
    if not metadata:
        return MappingProxyType({})
    assert isinstance(metadata, Mapping)
    metadata_path = f"{path}.metadata"
    decoded: dict[str, str] = {}
    for key, value in metadata.items():
        key_text = _decode_arrow_metadata_entry(key, metadata_path)
        value_text = _decode_arrow_metadata_entry(value, f"{metadata_path}.{key_text}")
        decoded[key_text] = value_text
    return MappingProxyType(decoded)


def _sanitize(error: BaseException) -> str:
    """Return a stable, non-leaking label for an Arrow conversion error."""

    return type(error).__name__


def table_schema_to_arrow(schema: TableSchema) -> pa.Schema:
    """Convert a logical schema to ``pyarrow.Schema``.

    Unsupported logical nodes (e.g. interval variants the installed PyArrow
    build cannot represent) raise ``SchemaDefinitionError``.  PyArrow conversion
    errors are never leaked to callers.
    """

    fields: list[pa.Field] = []
    issues: list[SchemaIssue] = []
    for index, schema_field in enumerate(schema.fields):
        field_path = f"fields[{index}]"
        arrow_type, type_issues = _logical_to_arrow(
            schema_field.type, f"{field_path}.type"
        )
        issues.extend(
            SchemaIssue(issue.code, field_path, issue.message) for issue in type_issues
        )
        if arrow_type is None:
            continue
        try:
            fields.append(
                pa.field(
                    schema_field.name,
                    arrow_type,
                    nullable=schema_field.nullable,
                    metadata=_field_metadata_to_arrow(schema_field.metadata),
                )
            )
        except _CONVERT_ERRORS as error:
            issues.append(
                _issue(
                    "arrow_field_invalid",
                    field_path,
                    f"field cannot be converted to Arrow: {_sanitize(error)}",
                )
            )
    if issues:
        raise SchemaDefinitionError(
            tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
        )
    try:
        return pa.schema(fields)
    except _CONVERT_ERRORS as error:
        raise SchemaDefinitionError(
            (
                _issue(
                    "arrow_schema_invalid",
                    "schema",
                    f"schema cannot be converted to Arrow: {_sanitize(error)}",
                ),
            )
        ) from None


def _logical_to_arrow(
    logical: LogicalType, path: str
) -> tuple[pa.DataType | None, list[SchemaIssue]]:
    if isinstance(logical, ScalarType):
        return _SCALAR_ARROW[logical.name], []
    if isinstance(logical, DecimalType):
        if logical.precision <= 38:
            return pa.decimal128(logical.precision, logical.scale), []
        return pa.decimal256(logical.precision, logical.scale), []
    if isinstance(logical, TimestampType):
        return pa.timestamp(logical.unit, tz=logical.timezone), []
    if isinstance(logical, TimeType):
        if logical.bit_width == 32:
            return pa.time32(logical.unit), []
        return pa.time64(logical.unit), []
    if isinstance(logical, DurationType):
        return pa.duration(logical.unit), []
    if isinstance(logical, IntervalType):
        arrow = _INTERVAL_VARIANT_TO_ARROW.get(logical.variant)
        if arrow is not None:
            return arrow, []
        return None, [
            _issue(
                "interval_unsupported_arrow",
                path,
                f"interval variant {logical.variant!r} is not representable by "
                "the installed PyArrow",
            )
        ]
    if isinstance(logical, FixedSizeBinaryType):
        return pa.binary(logical.byte_width), []
    if isinstance(logical, ListType):
        return pa.list_(_field_to_arrow(logical.element)), []
    if isinstance(logical, LargeListType):
        return pa.large_list(_field_to_arrow(logical.element)), []
    if isinstance(logical, FixedSizeListType):
        return pa.list_(_field_to_arrow(logical.element), logical.size), []
    if isinstance(logical, StructType):
        return pa.struct([_field_to_arrow(item) for item in logical.fields]), []
    if isinstance(logical, MapType):
        key_arrow = _field_to_arrow(logical.key)
        value_arrow = _field_to_arrow(logical.value)
        return pa.map_(key_arrow, value_arrow), []
    if isinstance(logical, DictionaryType):
        return (
            pa.dictionary(
                _SCALAR_ARROW[logical.index.name],
                _SCALAR_ARROW[logical.value.name],
                ordered=logical.ordered,
            ),
            [],
        )
    raise AssertionError(f"unhandled logical type {logical!r}")  # pragma: no cover


def _field_to_arrow(field: FieldSchema) -> pa.Field:
    arrow_type, issues = _logical_to_arrow(field.type, "<inline>")
    if arrow_type is None or issues:
        raise SchemaDefinitionError(tuple(issues))
    return pa.field(
        field.name,
        arrow_type,
        nullable=field.nullable,
        metadata=_field_metadata_to_arrow(field.metadata),
    )


def table_schema_from_arrow(schema: pa.Schema) -> TableSchema:
    """Convert a ``pyarrow.Schema`` to a logical ``TableSchema``.

    Unsupported Arrow types (extensions, unions, run-end encoded, list views)
    raise ``SchemaDefinitionError``.  PyArrow inspection errors are never leaked.
    """

    fields: list[FieldSchema] = []
    issues: list[SchemaIssue] = []
    try:
        arrow_fields = list(schema)
    except _CONVERT_ERRORS as error:
        raise SchemaDefinitionError(
            (
                _issue(
                    "arrow_schema_invalid",
                    "schema",
                    f"Arrow schema is not introspectable: {_sanitize(error)}",
                ),
            )
        ) from None
    for index, arrow_field in enumerate(arrow_fields):
        field_path = f"fields[{index}]"
        logical, type_issues = _arrow_to_logical(arrow_field.type, f"{field_path}.type")
        issues.extend(type_issues)
        if logical is None:
            continue
        try:
            fields.append(
                FieldSchema(
                    arrow_field.name,
                    logical,
                    bool(arrow_field.nullable),
                    _arrow_metadata_to_field(arrow_field.metadata, field_path),
                )
            )
        except SchemaDefinitionError:
            raise
        except (TypeError, ValueError) as error:  # pragma: no cover - defensive
            issues.append(
                _issue(
                    "arrow_field_invalid",
                    field_path,
                    f"field cannot be read from Arrow: {_sanitize(error)}",
                )
            )
    if issues:
        raise SchemaDefinitionError(
            tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))
        )
    return TableSchema(tuple(fields))


def _arrow_to_logical(
    arrow_type: pa.DataType, path: str
) -> tuple[LogicalType | None, list[SchemaIssue]]:
    unsupported = _unsupported_arrow_issue(arrow_type, path)
    if unsupported is not None:
        return None, [unsupported]
    if arrow_type in _ARROW_SCALAR_TO_NAME:
        return ScalarType(_ARROW_SCALAR_TO_NAME[arrow_type]), []
    if patypes.is_decimal(arrow_type):
        return DecimalType(int(arrow_type.precision), int(arrow_type.scale)), []
    if patypes.is_timestamp(arrow_type):
        return TimestampType(str(arrow_type.unit), arrow_type.tz), []
    if patypes.is_time(arrow_type):
        return TimeType(str(arrow_type.unit), int(arrow_type.bit_width)), []
    if patypes.is_duration(arrow_type):
        return DurationType(str(arrow_type.unit)), []
    if patypes.is_interval(arrow_type):
        return IntervalType("month_day_nano"), []
    if patypes.is_fixed_size_binary(arrow_type):
        return FixedSizeBinaryType(int(arrow_type.byte_width)), []
    if patypes.is_list(arrow_type):
        return (
            ListType(_arrow_field_to_field(arrow_type.value_field, f"{path}.element")),
            [],
        )
    if patypes.is_large_list(arrow_type):
        return (
            LargeListType(
                _arrow_field_to_field(arrow_type.value_field, f"{path}.element")
            ),
            [],
        )
    if patypes.is_fixed_size_list(arrow_type):
        return (
            FixedSizeListType(
                _arrow_field_to_field(arrow_type.value_field, f"{path}.element"),
                int(arrow_type.list_size),
            ),
            [],
        )
    if patypes.is_struct(arrow_type):
        fields = tuple(
            _arrow_field_to_field(item, f"{path}.fields[{index}]")
            for index, item in enumerate(arrow_type)
        )
        return StructType(fields), []
    if patypes.is_map(arrow_type):
        return (
            MapType(
                _arrow_field_to_field(arrow_type.key_field, f"{path}.key"),
                _arrow_field_to_field(arrow_type.item_field, f"{path}.value"),
            ),
            [],
        )
    if patypes.is_dictionary(arrow_type):
        index_logical, index_issues = _arrow_to_logical(
            arrow_type.index_type, f"{path}.index"
        )
        value_logical, value_issues = _arrow_to_logical(
            arrow_type.value_type, f"{path}.value"
        )
        issues = [*index_issues, *value_issues]
        if issues:
            return None, issues
        assert isinstance(index_logical, ScalarType)  # pragma: no cover
        assert isinstance(value_logical, ScalarType)  # pragma: no cover
        return (
            DictionaryType(index_logical, value_logical, bool(arrow_type.ordered)),
            [],
        )
    return None, [
        _issue(
            "unsupported_arrow_type",
            path,
            f"Arrow type {arrow_type!r} is not supported",
        )
    ]


def _unsupported_arrow_issue(arrow_type: pa.DataType, path: str) -> SchemaIssue | None:
    if isinstance(arrow_type, pa.ExtensionType):
        return _issue(
            "unsupported_arrow_type",
            path,
            f"Arrow extension type {arrow_type!r} is not supported",
        )
    if patypes.is_union(arrow_type) or patypes.is_run_end_encoded(arrow_type):
        return _issue(
            "unsupported_arrow_type",
            path,
            f"Arrow type {arrow_type!r} is not supported",
        )
    if patypes.is_list_view(arrow_type) or patypes.is_large_list_view(arrow_type):
        return _issue(
            "unsupported_arrow_type",
            path,
            f"Arrow list-view type {arrow_type!r} is not supported",
        )
    return None


def _arrow_field_to_field(arrow_field: pa.Field, path: str) -> FieldSchema:
    logical, issues = _arrow_to_logical(arrow_field.type, f"{path}.type")
    if logical is None or issues:
        raise SchemaDefinitionError(tuple(issues))
    return FieldSchema(
        arrow_field.name,
        logical,
        bool(arrow_field.nullable),
        _arrow_metadata_to_field(arrow_field.metadata, path),
    )


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------


def compare_schemas(
    declared: TableSchema, observed: TableSchema
) -> tuple[SchemaMismatch, ...]:
    """Compare declared and observed schemas, returning sorted mismatches.

    Comparison is exact after normalization except that an observed
    ``nullable=False`` field satisfies a declared ``nullable=True`` field.  The
    comparison covers ordered field names, logical types, nullability, and any
    declared connector-native ``field_id`` metadata.
    """

    mismatches: list[SchemaMismatch] = []
    declared_fields = declared.fields
    observed_fields = observed.fields
    max_index = max(len(declared_fields), len(observed_fields))
    for index in range(max_index):
        field_path = f"fields[{index}]"
        if index >= len(declared_fields):
            mismatches.append(
                SchemaMismatch(
                    "extra_observed_field",
                    field_path,
                    "observed schema declares an extra field",
                )
            )
            continue
        if index >= len(observed_fields):
            mismatches.append(
                SchemaMismatch(
                    "missing_observed_field",
                    field_path,
                    "observed schema is missing a declared field",
                )
            )
            continue
        declared_field = declared_fields[index]
        observed_field = observed_fields[index]
        if declared_field.name != observed_field.name:
            mismatches.append(
                SchemaMismatch(
                    "field_name",
                    f"{field_path}.name",
                    f"declared field {declared_field.name!r} observed as "
                    f"{observed_field.name!r}",
                )
            )
        if declared_field.type != observed_field.type:
            mismatches.append(
                SchemaMismatch(
                    "field_type",
                    f"{field_path}.type",
                    "declared and observed field types differ",
                )
            )
        if observed_field.nullable and not declared_field.nullable:
            mismatches.append(
                SchemaMismatch(
                    "nullable_field",
                    field_path,
                    "observed field is nullable where the declaration is not",
                )
            )
        declared_field_id = declared_field.field_id
        if (
            declared_field_id is not None
            and observed_field.field_id != declared_field_id
        ):
            mismatches.append(
                SchemaMismatch(
                    "field_id",
                    f"{field_path}.field_id",
                    f"declared field_id {declared_field_id!r} observed as "
                    f"{observed_field.field_id!r}",
                )
            )
    return tuple(
        sorted(mismatches, key=lambda mismatch: (mismatch.path, mismatch.message))
    )
