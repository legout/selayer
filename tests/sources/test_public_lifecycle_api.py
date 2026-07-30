"""Curated public source-lifecycle API at the package root.

The package root exposes the stable source lifecycle types and error classes
end users need (schema models, status, reload results, and sanitized errors).
It deliberately does *not* export adapter internals, the registry, raw handles,
profile values, or schema-parser internals — those are private to the sources
package.
"""

from __future__ import annotations

import selayer

REQUIRED_EXPORTS = {
    "TableSchema",
    "FieldSchema",
    "SourceStatus",
    "ReloadResult",
    "SourceError",
    "SourceConnectionError",
    "SourceDependencyError",
    "SourceProfileError",
    "SourceSchemaError",
    "SourceReloadError",
}

FORBIDDEN_EXPORTS = {
    "SourceRegistry",
    "ArrowDatasetAdapter",
    "DeltaAdapter",
    "IcebergAdapter",
    "SqliteAdapter",
    "DuckDbAdapter",
    "PostgresAdapter",
    "SourceHandle",
    "QueryBinding",
    "SourceAdapter",
    "SourceScanRequirement",
    "SourceFilter",
    "RuntimeProfile",
    "RuntimeProfileResolver",
    "MappingProfileResolver",
    "parse_schema_document",
    "validate_schema_document",
    "table_schema_to_arrow",
    "table_schema_from_arrow",
    "compare_schemas",
}


def test_source_lifecycle_types_are_exported_from_root() -> None:
    for name in REQUIRED_EXPORTS:
        assert hasattr(selayer, name), f"missing public export: {name}"
        assert name in selayer.__all__, f"{name} not in selayer.__all__"


def test_adapter_and_registry_internals_are_not_exported() -> None:
    for name in FORBIDDEN_EXPORTS:
        assert name not in selayer.__all__, f"{name} must not be exported"


def test_exported_schema_and_status_types_are_stable_shapes() -> None:
    from selayer.sources.schema import FieldSchema, ScalarType, TableSchema

    assert selayer.TableSchema is TableSchema
    assert selayer.FieldSchema is FieldSchema

    schema = selayer.TableSchema(
        (selayer.FieldSchema("id", ScalarType("int64"), False),)
    )
    assert schema.field("id").name == "id"

    from selayer.sources.base import ReloadResult, SourceStatus

    assert selayer.SourceStatus is SourceStatus
    assert selayer.ReloadResult is ReloadResult


def test_exported_error_classes_are_sanitized_source_errors() -> None:
    from selayer.sources.errors import (
        SourceConnectionError,
        SourceDependencyError,
        SourceError,
        SourceProfileError,
        SourceReloadError,
        SourceSchemaError,
    )

    assert selayer.SourceError is SourceError
    assert selayer.SourceConnectionError is SourceConnectionError
    assert selayer.SourceDependencyError is SourceDependencyError
    assert selayer.SourceProfileError is SourceProfileError
    assert selayer.SourceSchemaError is SourceSchemaError
    assert selayer.SourceReloadError is SourceReloadError

    # Constructed errors carry only safe identifiers.
    error = selayer.SourceConnectionError("orders", "connect_failed", "ignored")
    assert error.source_id == "orders"
    assert error.code == "connect_failed"
