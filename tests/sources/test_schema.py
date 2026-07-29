"""Tests for the recursive Arrow-compatible schema model."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pyarrow as pa
import pytest

from selayer.sources.schema import (
    SCALAR_NAMES,
    DecimalType,
    DictionaryType,
    DurationType,
    FieldSchema,
    FixedSizeBinaryType,
    FixedSizeListType,
    IntervalType,
    LargeListType,
    ListType,
    LogicalType,
    MapType,
    ScalarType,
    SchemaDefinitionError,
    SchemaIssue,
    SchemaMismatch,
    StructType,
    TableSchema,
    TimestampType,
    TimeType,
    compare_schemas,
    parse_schema_document,
    schema_fingerprint,
    table_schema_from_arrow,
    table_schema_to_arrow,
    validate_schema_document,
)

# ---------------------------------------------------------------------------
# Shared case tables (verbatim from the Task 1 brief)
# ---------------------------------------------------------------------------

SCALAR_CASES = (
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
)

RECURSIVE_CASES = (
    {"decimal": {"precision": 18, "scale": 2}},
    {"timestamp": {"unit": "us", "timezone": "UTC"}},
    {"list": {"element": {"type": "int64", "nullable": False}}},
    {"large_list": {"element": {"type": "utf8", "nullable": True}}},
    {"fixed_size_list": {"size": 3, "element": {"type": "int32", "nullable": False}}},
    {"struct": {"fields": [{"name": "x", "type": "int64", "nullable": False}]}},
    {
        "map": {
            "key": {"type": "utf8", "nullable": False},
            "value": {"type": "int64", "nullable": True},
        }
    },
    {"dictionary": {"index": "int32", "value": "utf8", "ordered": False}},
)

# Pre-flight resolution types governed by the spec but absent from the brief's
# parametrized round-trip table: fixed-size binary, time (with valid
# unit/width pairings), duration, and every interval variant.
PREFLIGHT_ROUND_TRIP_CASES = (
    {"fixed_size_binary": {"byte_width": 16}},
    {"duration": {"unit": "us"}},
    {"time": {"unit": "s", "bit_width": 32}},
    {"time": {"unit": "ms", "bit_width": 32}},
    {"time": {"unit": "us", "bit_width": 64}},
    {"time": {"unit": "ns", "bit_width": 64}},
    {"interval": {"variant": "month_day_nano"}},
)


def _field_schema(logical_type: object) -> dict[str, object]:
    return {"name": "value", "type": logical_type, "nullable": False}


def _wrap(logical_type: object) -> dict[str, object]:
    return {"fields": [_field_schema(logical_type)]}


def invalid_integer_metadata_schema(field: str, value: object) -> dict[str, object]:
    """Build a schema that supplies a non-integer where ``field`` expects one.

    One explicit mapping branch per listed field; no fallthrough.
    """

    if field == "precision":
        return _wrap({"decimal": {"precision": value, "scale": 0}})
    if field == "scale":
        return _wrap({"decimal": {"precision": 5, "scale": value}})
    if field == "size":
        return _wrap(
            {
                "fixed_size_list": {
                    "size": value,
                    "element": {"type": "int32", "nullable": False},
                }
            }
        )
    if field == "byte_width":
        return _wrap({"fixed_size_binary": {"byte_width": value}})
    if field == "bit_width":
        return _wrap({"time": {"unit": "s", "bit_width": value}})
    if field == "field_id":
        return {
            "fields": [
                {"name": "value", "type": "int64", "nullable": False, "field_id": value}
            ]
        }
    raise AssertionError(f"unexpected integer-metadata field {field!r}")


# ---------------------------------------------------------------------------
# Recursive parse + Arrow round trip (brief Step 1)
# ---------------------------------------------------------------------------


def test_parse_recursive_schema_and_round_trip_arrow() -> None:
    raw = {
        "fields": [
            {"name": "id", "type": "int64", "nullable": False},
            {
                "name": "lines",
                "type": {
                    "list": {
                        "element": {
                            "type": {
                                "struct": {
                                    "fields": [
                                        {
                                            "name": "amount",
                                            "type": {
                                                "decimal": {
                                                    "precision": 18,
                                                    "scale": 2,
                                                }
                                            },
                                            "nullable": False,
                                        }
                                    ]
                                }
                            },
                            "nullable": False,
                        }
                    }
                },
                "nullable": True,
            },
        ]
    }

    schema = parse_schema_document(raw)

    assert schema.fields[0] == FieldSchema("id", ScalarType("int64"), False)
    assert schema.fields[1].type == ListType(
        FieldSchema(
            "element",
            StructType(
                (
                    FieldSchema(
                        "amount",
                        DecimalType(18, 2),
                        False,
                    ),
                )
            ),
            False,
        )
    )
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema
    assert table_schema_to_arrow(schema).field("id") == pa.field(
        "id", pa.int64(), nullable=False
    )


# ---------------------------------------------------------------------------
# Validation, comparison, fingerprint (brief Step 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "path", "message"),
    [
        ({}, "schema.fields", "fields is required"),
        ({"fields": []}, "schema.fields", "fields must be non-empty"),
        (
            {
                "fields": [
                    {"name": "id", "type": "int64", "nullable": False},
                    {"name": "id", "type": "int64", "nullable": False},
                ]
            },
            "schema.fields[1].name",
            "field name 'id' is duplicated",
        ),
        (
            {
                "fields": [
                    {
                        "name": "amount",
                        "type": {"decimal": {"precision": 2, "scale": 3}},
                        "nullable": False,
                    }
                ]
            },
            "schema.fields[0].type.decimal.scale",
            "scale must be between 0 and precision",
        ),
    ],
)
def test_invalid_schema_is_one_sorted_domain_error(
    raw: object, path: str, message: str
) -> None:
    with pytest.raises(SchemaDefinitionError) as caught:
        parse_schema_document(raw)
    assert [(issue.path, issue.message) for issue in caught.value.issues] == [
        (path, message)
    ]


def test_compare_schema_allows_stricter_observed_nullability_only() -> None:
    declared = TableSchema((FieldSchema("id", ScalarType("int64"), True),))
    stricter = TableSchema((FieldSchema("id", ScalarType("int64"), False),))
    unsafe = TableSchema((FieldSchema("id", ScalarType("int64"), True),))

    assert compare_schemas(declared, stricter) == ()
    assert compare_schemas(stricter, unsafe)[0].code == "nullable_field"
    assert schema_fingerprint(declared) == schema_fingerprint(declared)
    assert schema_fingerprint(declared) != schema_fingerprint(stricter)


@pytest.mark.parametrize("logical_type", SCALAR_CASES + RECURSIVE_CASES)
def test_every_supported_type_round_trips(logical_type: object) -> None:
    schema = parse_schema_document(
        {"fields": [{"name": "value", "type": logical_type, "nullable": False}]}
    )
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


@pytest.mark.parametrize("field", ("precision", "scale", "size", "field_id"))
def test_boolean_is_not_accepted_as_integer_metadata(field: str) -> None:
    raw = invalid_integer_metadata_schema(field, True)
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(raw)


# ---------------------------------------------------------------------------
# Pre-flight resolution types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("logical_type", PREFLIGHT_ROUND_TRIP_CASES)
def test_preflight_type_round_trips_through_arrow(logical_type: object) -> None:
    schema = parse_schema_document(_wrap(logical_type))
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


def test_fixed_size_binary_parses_and_round_trips() -> None:
    schema = parse_schema_document(_wrap({"fixed_size_binary": {"byte_width": 16}}))
    assert schema.fields[0].type == FixedSizeBinaryType(16)
    assert table_schema_to_arrow(schema).field("value").type == pa.binary(16)
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


@pytest.mark.parametrize(
    ("unit", "bit_width"),
    [("s", 32), ("ms", 32), ("us", 64), ("ns", 64)],
)
def test_time_valid_unit_width_pairings_round_trip(unit: str, bit_width: int) -> None:
    schema = parse_schema_document(
        _wrap({"time": {"unit": unit, "bit_width": bit_width}})
    )
    assert schema.fields[0].type == TimeType(unit, bit_width)
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


@pytest.mark.parametrize(
    ("unit", "bit_width"),
    [("s", 64), ("ms", 64), ("us", 32), ("ns", 32)],
)
def test_time_invalid_unit_width_pairings_are_rejected(
    unit: str, bit_width: int
) -> None:
    with pytest.raises(SchemaDefinitionError) as caught:
        parse_schema_document(_wrap({"time": {"unit": unit, "bit_width": bit_width}}))
    assert caught.value.issues[0].path == "schema.fields[0].type.time.unit"
    assert "bit_width" in caught.value.issues[0].message


def test_duration_parses_and_round_trips() -> None:
    schema = parse_schema_document(_wrap({"duration": {"unit": "us"}}))
    assert schema.fields[0].type == DurationType("us")
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


@pytest.mark.parametrize("variant", ("year_month", "day_time", "month_day_nano"))
def test_interval_variants_are_accepted_by_the_document_model(variant: str) -> None:
    schema = parse_schema_document(_wrap({"interval": {"variant": variant}}))
    assert schema.fields[0].type == IntervalType(variant)


def test_month_day_nano_interval_round_trips_through_arrow() -> None:
    schema = parse_schema_document(_wrap({"interval": {"variant": "month_day_nano"}}))
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


@pytest.mark.parametrize("variant", ("year_month", "day_time"))
def test_unsupported_interval_variants_fail_arrow_conversion(variant: str) -> None:
    schema = parse_schema_document(_wrap({"interval": {"variant": variant}}))
    with pytest.raises(SchemaDefinitionError):
        table_schema_to_arrow(schema)


@pytest.mark.parametrize("field", ("byte_width", "bit_width"))
def test_boolean_is_not_accepted_for_preflight_integer_metadata(field: str) -> None:
    raw = invalid_integer_metadata_schema(field, True)
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(raw)


# ---------------------------------------------------------------------------
# Validation aggregation, sorting, and exception hygiene
# ---------------------------------------------------------------------------


def test_validate_schema_document_returns_sorted_tuple_for_empty_input() -> None:
    issues = validate_schema_document({})
    assert isinstance(issues, tuple)
    assert all(isinstance(issue, SchemaIssue) for issue in issues)
    assert issues == tuple(
        sorted(issues, key=lambda issue: (issue.path, issue.message))
    )


def test_validate_schema_document_aggregates_and_sorts_multiple_issues() -> None:
    raw = {
        "fields": [
            {"name": "a", "type": {"decimal": {"precision": 2, "scale": 3}}},
            {"type": "int64", "nullable": False},
            {"name": "c", "type": "not_a_scalar", "nullable": False},
        ]
    }
    issues = validate_schema_document(raw)
    assert issues == tuple(
        sorted(issues, key=lambda issue: (issue.path, issue.message))
    )
    assert len(issues) >= 3


@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        "not a mapping",
        [],
        {"fields": "x"},
        {"fields": [123]},
        {"fields": [{"type": "int64", "nullable": False}]},
        {"fields": [{"name": "a", "nullable": False}]},
        {"fields": [{"name": "a", "type": "int64"}]},
        {"fields": [{"name": 1, "type": "int64", "nullable": False}]},
        {"fields": [{"name": "a", "type": 7, "nullable": False}]},
    ],
)
def test_malformed_documents_never_leak_exceptions(raw: object) -> None:
    issues = validate_schema_document(raw)
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(raw)
    assert isinstance(issues, tuple)


def test_duplicate_field_detection_in_struct() -> None:
    raw = _wrap(
        {
            "struct": {
                "fields": [
                    {"name": "x", "type": "int64", "nullable": False},
                    {"name": "x", "type": "int64", "nullable": False},
                ]
            }
        }
    )
    with pytest.raises(SchemaDefinitionError) as caught:
        parse_schema_document(raw)
    paths = [issue.path for issue in caught.value.issues]
    assert "schema.fields[0].type.struct.fields[1].name" in paths


def test_map_key_must_be_non_nullable() -> None:
    raw = _wrap(
        {
            "map": {
                "key": {"type": "utf8", "nullable": True},
                "value": {"type": "int64", "nullable": True},
            }
        }
    )
    with pytest.raises(SchemaDefinitionError) as caught:
        parse_schema_document(raw)
    assert caught.value.issues[0].path == "schema.fields[0].type.map.key.nullable"


def test_unknown_type_discriminator_is_rejected() -> None:
    with pytest.raises(SchemaDefinitionError) as caught:
        parse_schema_document(_wrap({"widget": {}}))
    assert caught.value.issues[0].path == "schema.fields[0].type"


def test_dictionary_index_must_be_signed_integer() -> None:
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(
            _wrap({"dictionary": {"index": "uint32", "value": "utf8"}})
        )


def test_decimal_precision_must_be_in_range() -> None:
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(_wrap({"decimal": {"precision": 0, "scale": 0}}))
    with pytest.raises(SchemaDefinitionError):
        parse_schema_document(_wrap({"decimal": {"precision": 77, "scale": 0}}))


def test_decimal256_round_trips_above_decimal128_precision() -> None:
    schema = parse_schema_document(_wrap({"decimal": {"precision": 39, "scale": 4}}))
    arrow_type = table_schema_to_arrow(schema).field("value").type
    assert arrow_type == pa.decimal256(39, 4)
    assert table_schema_from_arrow(table_schema_to_arrow(schema)) == schema


# ---------------------------------------------------------------------------
# Arrow conversion rejection of unsupported types
# ---------------------------------------------------------------------------


def test_table_schema_from_arrow_rejects_extension_type() -> None:
    class MyExtension(pa.ExtensionType):
        def __init__(self) -> None:
            super().__init__(pa.null(), "test.my_extension")

        def __arrow_ext_serialize__(self) -> bytes:
            return b""

    extension = MyExtension()
    arrow_schema = pa.schema([pa.field("value", extension, nullable=False)])
    with pytest.raises(SchemaDefinitionError):
        table_schema_from_arrow(arrow_schema)


def test_table_schema_from_arrow_rejects_union_type() -> None:
    arrow_schema = pa.schema(
        [
            pa.field(
                "value",
                pa.sparse_union([pa.field("a", pa.int64()), pa.field("b", pa.utf8())]),
                nullable=False,
            )
        ]
    )
    with pytest.raises(SchemaDefinitionError):
        table_schema_from_arrow(arrow_schema)


def test_table_schema_from_arrow_rejects_run_end_encoded_type() -> None:
    arrow_schema = pa.schema(
        [pa.field("value", pa.run_end_encoded(pa.int32(), pa.int64()), nullable=False)]
    )
    with pytest.raises(SchemaDefinitionError):
        table_schema_from_arrow(arrow_schema)


def test_table_schema_to_arrow_preserves_metadata_and_field_id() -> None:
    schema = parse_schema_document(
        {
            "fields": [
                {
                    "name": "value",
                    "type": "int64",
                    "nullable": False,
                    "field_id": 7,
                    "metadata": {"b": "2", "a": "1"},
                }
            ]
        }
    )
    arrow_field = table_schema_to_arrow(schema).field("value")
    assert arrow_field.metadata == {
        b"selayer.field_id": b"7",
        b"a": b"1",
        b"b": b"2",
    }
    round_tripped = table_schema_from_arrow(table_schema_to_arrow(schema))
    assert round_tripped == schema
    assert round_tripped.fields[0].field_id == "7"


# ---------------------------------------------------------------------------
# Immutability and public model contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        ScalarType("int64"),
        DecimalType(18, 2),
        TimestampType("us", "UTC"),
        TimeType("s", 32),
        DurationType("us"),
        IntervalType("month_day_nano"),
        FixedSizeBinaryType(8),
        ListType(FieldSchema("element", ScalarType("int64"), False)),
        LargeListType(FieldSchema("element", ScalarType("utf8"), True)),
        FixedSizeListType(FieldSchema("element", ScalarType("int32"), False), 3),
        StructType((FieldSchema("x", ScalarType("int64"), False),)),
        MapType(
            FieldSchema("key", ScalarType("utf8"), False),
            FieldSchema("value", ScalarType("int64"), True),
        ),
        DictionaryType(ScalarType("int32"), ScalarType("utf8"), False),
        FieldSchema("id", ScalarType("int64"), False),
        TableSchema((FieldSchema("id", ScalarType("int64"), False),)),
        SchemaIssue("code", "path", "message"),
        SchemaMismatch("code", "path", "message"),
    ],
)
def test_models_are_frozen_and_slotted(model: Any) -> None:
    import dataclasses

    assert hasattr(model, "__slots__")
    # CPython 3.13 raises TypeError when assigning an attribute that is not a
    # declared slot on a frozen+slotted dataclass, so assign an existing field
    # to exercise the frozen ``__setattr__`` (FrozenInstanceError, a subclass of
    # AttributeError).
    first_field = dataclasses.fields(model)[0].name
    with pytest.raises(AttributeError):
        setattr(model, first_field, "mutated")


def test_field_metadata_is_immutable_mapping_proxy() -> None:
    schema = parse_schema_document(
        {
            "fields": [
                {
                    "name": "value",
                    "type": "int64",
                    "nullable": False,
                    "metadata": {"a": "1"},
                }
            ]
        }
    )
    metadata = schema.fields[0].metadata
    assert isinstance(metadata, MappingProxyType)
    with pytest.raises(TypeError):
        metadata["b"] = "2"  # type: ignore[index]


def test_table_schema_field_lookup_raises_key_error_for_missing() -> None:
    schema = TableSchema((FieldSchema("id", ScalarType("int64"), False),))
    assert schema.field("id").name == "id"
    with pytest.raises(KeyError):
        schema.field("missing")


def test_scalar_names_are_closed() -> None:
    assert SCALAR_NAMES == frozenset(SCALAR_CASES)


# ---------------------------------------------------------------------------
# Comparison rules
# ---------------------------------------------------------------------------


def _table(*fields: FieldSchema) -> TableSchema:
    return TableSchema(tuple(fields))


def test_compare_extra_observed_field_is_reported() -> None:
    declared = _table(FieldSchema("id", ScalarType("int64"), False))
    observed = _table(
        FieldSchema("id", ScalarType("int64"), False),
        FieldSchema("extra", ScalarType("utf8"), False),
    )
    mismatches = compare_schemas(declared, observed)
    assert mismatches[0].code == "extra_observed_field"
    assert mismatches == tuple(
        sorted(mismatches, key=lambda mismatch: (mismatch.path, mismatch.message))
    )


def test_compare_missing_observed_field_is_reported() -> None:
    declared = _table(
        FieldSchema("id", ScalarType("int64"), False),
        FieldSchema("name", ScalarType("utf8"), False),
    )
    observed = _table(FieldSchema("id", ScalarType("int64"), False))
    mismatches = compare_schemas(declared, observed)
    assert mismatches[0].code == "missing_observed_field"


def test_compare_field_name_and_type_mismatches() -> None:
    declared = _table(FieldSchema("id", ScalarType("int64"), False))
    observed = _table(FieldSchema("code", ScalarType("utf8"), False))
    mismatches = compare_schemas(declared, observed)
    codes = {m.code for m in mismatches}
    assert codes == {"field_name", "field_type"}


def test_compare_declared_field_id_must_match_observed() -> None:
    declared = parse_schema_document(
        {"fields": [{"name": "id", "type": "int64", "nullable": False, "field_id": 1}]}
    )
    observed = parse_schema_document(
        {"fields": [{"name": "id", "type": "int64", "nullable": False, "field_id": 2}]}
    )
    mismatches = compare_schemas(declared, observed)
    assert mismatches[0].code == "field_id"


def test_compare_matching_schemas_return_empty_tuple() -> None:
    declared = _table(FieldSchema("id", ScalarType("int64"), False))
    observed = _table(FieldSchema("id", ScalarType("int64"), False))
    assert compare_schemas(declared, observed) == ()


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_order_sensitive() -> None:
    first = _table(
        FieldSchema("a", ScalarType("int64"), False),
        FieldSchema("b", ScalarType("utf8"), False),
    )
    swapped = _table(
        FieldSchema("b", ScalarType("utf8"), False),
        FieldSchema("a", ScalarType("int64"), False),
    )
    assert schema_fingerprint(first) == schema_fingerprint(first)
    assert schema_fingerprint(first) != schema_fingerprint(swapped)
    assert len(schema_fingerprint(first)) == 64


def test_fingerprint_is_independent_of_metadata_insertion_order() -> None:
    schema_a = parse_schema_document(
        {
            "fields": [
                {
                    "name": "v",
                    "type": "int64",
                    "nullable": False,
                    "metadata": {"a": "1", "b": "2"},
                }
            ]
        }
    )
    schema_b = parse_schema_document(
        {
            "fields": [
                {
                    "name": "v",
                    "type": "int64",
                    "nullable": False,
                    "metadata": {"b": "2", "a": "1"},
                }
            ]
        }
    )
    assert schema_fingerprint(schema_a) == schema_fingerprint(schema_b)


def test_logical_type_union_alias_is_exported() -> None:
    assert LogicalType is not None
