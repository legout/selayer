# Data Source Adapters, Schema Contracts, and Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace eager Polars source loading with schema-validated Arrow/native adapters for files, lakehouses, databases, S3, and programmatic Arrow, including atomic source reloads.

**Architecture:** A private `selayer.sources` module resolves complete catalog schemas, runtime profiles, connector adapters, and active registrations behind `SourceRegistry`. Persistent PyArrow Datasets serve Parquet, CSV, Delta, and programmatic Arrow sources; PyIceberg owns Iceberg snapshots and query-scoped readers; DuckDB native attachments serve SQLite, DuckDB files, and PostgreSQL. `QueryEngine` remains the small public orchestration interface and delegates all source lifecycle work to the registry.

**Tech Stack:** Python 3.13+, DuckDB 1.3.2+, PyArrow 20+, PyYAML, Polars result frames, delta-rs/deltalake 1.6.2+, PyIceberg 0.11.1+, psycopg 3.3.4+, boto3, pytest, testcontainers, Ruff, Pyright, uv.

## Global Constraints

- Catalog schema version remains exactly built-in integer `1`; rewrite the unreleased source contract directly and add no compatibility loader.
- Every source declares a non-empty grain and exactly one complete inline `schema` or `schema_ref`.
- Schemas use the recursive Arrow-compatible model from `docs/superpowers/specs/2026-07-28-data-source-adapters-design.md`.
- The resolved catalog is executable authority; runtime inference verifies but never replaces declarations.
- Keep the planner connection-free and keep connector handles, credentials, and observed schemas out of `QueryPlan`.
- Preserve `QueryEngine.query(metrics, dimensions=None, filters=None) -> polars.DataFrame` and `QueryEngine.plan(...) -> QueryPlan`.
- Add only `reload_source`, `reload_all`, and `source_status` to the public engine lifecycle interface.
- Use Python `deltalake` and `pyiceberg`; do not use DuckDB Delta or Iceberg extensions.
- Preserve DuckDB projection/filter pushdown into registered PyArrow Datasets.
- S3 is a profiled transport for file/lakehouse connectors, not a source type.
- Credentials and authenticated locations never appear in catalogs, OKF, reprs, plans, logs, schema fingerprints, exception causes/contexts, or error text.
- Reload is explicit. `reload_source` preserves the old source on failure; `reload_all` is all-or-nothing.
- Do not add writes, polling/watchers, arbitrary SQL sources, public adapter plugins, persistent runtime caches, OKF-controlled execution, or additional execution engines.
- Every task follows strict RED/GREEN TDD, commits only its scoped files, and passes task-focused Ruff, formatting, and Pyright checks before review.

---

## File Structure

Create these focused modules:

```text
src/selayer/sources/
├── __init__.py          # Internal source-module exports
├── schema.py            # Recursive logical schema, Arrow conversion, comparison
├── config.py            # Closed connector configuration union
├── catalog.py           # Source YAML/schema_ref parsing before active cutover
├── profiles.py          # Opaque named runtime-profile resolution
├── errors.py            # Sanitized source lifecycle errors
├── base.py              # Adapter/handle/binding contracts and status types
├── registry.py          # Registration ownership, locks, reload, rollback, cleanup
└── adapters/
    ├── __init__.py
    ├── arrow.py          # Parquet, CSV, programmatic Arrow, S3 filesystem support
    ├── delta.py          # delta-rs snapshot → PyArrow Dataset
    ├── iceberg.py        # PyIceberg metadata + query-scoped scans/readers
    └── database.py       # SQLite, DuckDB-file, PostgreSQL native scans
```

Create connector-specific tests under `tests/sources/`. Keep semantic planning and DuckDB SQL compilation in their existing modules.

---

### Task 1: Add the recursive Arrow-compatible schema model

**Files:**

- Create: `src/selayer/sources/__init__.py`
- Create: `src/selayer/sources/schema.py`
- Create: `tests/sources/__init__.py`
- Create: `tests/sources/test_schema.py`

**Interfaces:**

- Produces `ScalarType`, `DecimalType`, `TimestampType`, `ListType`, `StructType`, `MapType`, `DictionaryType`, `FieldSchema`, `TableSchema`, and `LogicalType`.
- Produces `SchemaIssue(code: str, path: str, message: str)`, `SchemaMismatch(code: str, path: str, message: str)`, and `SchemaDefinitionError(issues: tuple[SchemaIssue, ...])`.
- Produces `validate_schema_document(raw: object, path: str = "schema") -> tuple[SchemaIssue, ...]` and `parse_schema_document(raw: object) -> TableSchema`.
- Produces `table_schema_from_arrow(schema: pyarrow.Schema) -> TableSchema` and `table_schema_to_arrow(schema: TableSchema) -> pyarrow.Schema`.
- Produces `compare_schemas(declared: TableSchema, observed: TableSchema) -> tuple[SchemaMismatch, ...]` and `schema_fingerprint(schema: TableSchema) -> str`.
- Later tasks consume these exact names; no connector logic belongs here.

- [ ] **Step 1: Write failing scalar, nested, and round-trip tests**

```python
from __future__ import annotations

import pyarrow as pa

from selayer.sources.schema import (
    DecimalType,
    FieldSchema,
    ListType,
    ScalarType,
    StructType,
    TableSchema,
    parse_schema_document,
    table_schema_from_arrow,
    table_schema_to_arrow,
)


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
```

- [ ] **Step 2: Write failing validation, compatibility, and fingerprint tests**

```python
import pytest

from selayer.sources.schema import (
    FieldSchema,
    ScalarType,
    SchemaDefinitionError,
    TableSchema,
    compare_schemas,
    parse_schema_document,
    schema_fingerprint,
)


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
                    {"name": "amount", "type": {"decimal": {"precision": 2, "scale": 3}}, "nullable": False}
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
```

Use these explicit case tables in the same test file:

```python
SCALAR_CASES = (
    "null", "boolean", "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64", "float16", "float32",
    "float64", "utf8", "large_utf8", "binary", "large_binary",
    "date32", "date64",
)
RECURSIVE_CASES = (
    {"decimal": {"precision": 18, "scale": 2}},
    {"timestamp": {"unit": "us", "timezone": "UTC"}},
    {"list": {"element": {"type": "int64", "nullable": False}}},
    {"large_list": {"element": {"type": "utf8", "nullable": True}}},
    {"fixed_size_list": {"size": 3, "element": {"type": "int32", "nullable": False}}},
    {"struct": {"fields": [{"name": "x", "type": "int64", "nullable": False}]}},
    {"map": {"key": {"type": "utf8", "nullable": False}, "value": {"type": "int64", "nullable": True}}},
    {"dictionary": {"index": "int32", "value": "utf8", "ordered": False}},
)

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
```

`invalid_integer_metadata_schema` is a local test helper with one explicit mapping branch for each listed field.

- [ ] **Step 3: Run the schema tests and verify RED**

Run:

```bash
uv run pytest -q tests/sources/test_schema.py
```

Expected: collection fails because `selayer.sources.schema` does not exist.

- [ ] **Step 4: Implement immutable schema types and deterministic validation**

Implement frozen, slotted dataclasses. Use tuples and `MappingProxyType` for nested collections. The scalar-name set is closed:

```python
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


@dataclass(frozen=True, slots=True)
class FieldSchema:
    name: str
    type: LogicalType
    nullable: bool
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class TableSchema:
    fields: tuple[FieldSchema, ...]

    def field(self, name: str) -> FieldSchema:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(name)
```

Parse recursive mappings with exact allowed keys per node and aggregate `SchemaIssue(path, message)` values sorted by `(path, message)`. Never leak `KeyError`, `TypeError`, `yaml.YAMLError`, or PyArrow conversion errors for malformed documents.

Use canonical JSON with sorted metadata keys and compact separators for SHA-256 fingerprints. Preserve field order in the canonical payload.

- [ ] **Step 5: Implement Arrow conversion and schema comparison**

Map every logical node explicitly to/from `pyarrow.DataType`. Reject unsupported extension/run-end encoded/union types with `SchemaDefinitionError` rather than approximating them.

Comparison rules are exact after normalization except that observed `nullable=False` satisfies declared `nullable=True`. Compare ordered names, types, nullability, and declared `field_id` metadata. Return sorted `SchemaMismatch(code, path, message)` values.

- [ ] **Step 6: Run focused and active tests**

Run:

```bash
uv run pytest -q tests/sources/test_schema.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
```

Expected: all commands exit zero; existing tests remain green.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/selayer/sources tests/sources
git commit -m "feat(sources): add recursive table schemas"
```

---

### Task 2: Parse closed connector declarations and schema references

**Files:**

- Create: `src/selayer/sources/config.py`
- Create: `src/selayer/sources/catalog.py`
- Create: `tests/sources/test_source_catalog.py`

**Interfaces:**

- Consumes Task 1 schema interfaces.
- Produces frozen `ParquetConfig`, `CsvConfig`, `DeltaConfig`, `IcebergConfig`, `SqliteConfig`, `DuckDbConfig`, `PostgresConfig`, `PyArrowConfig`, and `SourceConnector`.
- Produces `SourceDeclarationIssue(code: str, path: str, message: str)` and `SourceDeclarationError(issues: tuple[SourceDeclarationIssue, ...])`.
- Produces `ParsedSource(name: str, connector: SourceConnector, schema: TableSchema, grain: tuple[str, ...])`.
- Produces `validate_source_declarations(raw: object, catalog_path: Path) -> tuple[SourceDeclarationIssue, ...]` and `parse_source_declarations(raw: object, catalog_path: Path) -> Mapping[str, ParsedSource]`.
- Active `SemanticLayer.load()` is not changed until Task 4; existing tests remain green.

- [ ] **Step 1: Write failing connector-shape tests**

```python
from pathlib import Path

import pytest

from selayer.sources.catalog import SourceDeclarationError, parse_source_declarations
from selayer.sources.config import ParquetConfig, PostgresConfig


INLINE_SCHEMA = {
    "fields": [{"name": "id", "type": "int64", "nullable": False}]
}


def test_parse_file_and_database_connector_union(tmp_path: Path) -> None:
    raw = {
        "orders": {
            "type": "parquet",
            "location": "s3://warehouse/orders/",
            "credential_profile": "analytics_s3",
            "schema": INLINE_SCHEMA,
            "grain": ["id"],
        },
        "customers": {
            "type": "postgres",
            "connection_profile": "warehouse_ro",
            "relation": "analytics.customers",
            "schema": INLINE_SCHEMA,
            "grain": ["id"],
        },
    }

    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")

    assert sources["orders"].connector == ParquetConfig(
        "s3://warehouse/orders/", "analytics_s3"
    )
    assert sources["customers"].connector == PostgresConfig(
        "warehouse_ro", "analytics.customers"
    )


@pytest.mark.parametrize(
    ("source", "issue_path"),
    [
        ({"type": "unknown", "schema": INLINE_SCHEMA, "grain": ["id"]}, "data_sources.x.type"),
        ({"type": "parquet", "location": "x", "grain": ["id"]}, "data_sources.x.schema"),
        (
            {
                "type": "parquet",
                "location": "x",
                "schema": INLINE_SCHEMA,
                "schema_ref": "schema.yaml",
                "grain": ["id"],
            },
            "data_sources.x.schema",
        ),
        (
            {
                "type": "postgres",
                "connection_profile": "db",
                "relation": "public.x",
                "schema": INLINE_SCHEMA,
                "grain": ["id"],
                "password": "secret",
            },
            "data_sources.x.password",
        ),
    ],
)
def test_connector_union_rejects_unknown_or_secret_fields(
    tmp_path: Path, source: dict[str, object], issue_path: str
) -> None:
    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert issue_path in {issue.path for issue in caught.value.issues}
```

Use one explicit declaration matrix:

```python
@pytest.mark.parametrize(
    ("kind", "required", "forbidden"),
    [
        ("parquet", {"location"}, {"relation", "connection_profile", "handle"}),
        ("csv", {"location"}, {"relation", "connection_profile", "handle"}),
        ("delta", {"location"}, {"relation", "connection_profile", "handle"}),
        ("iceberg", {"catalog_profile", "namespace", "table"}, {"location", "relation", "handle"}),
        ("sqlite", {"location", "relation"}, {"credential_profile", "handle"}),
        ("duckdb", {"location", "relation"}, {"credential_profile", "handle"}),
        ("postgres", {"connection_profile", "relation"}, {"location", "handle"}),
        ("pyarrow", {"handle"}, {"location", "relation", "credential_profile"}),
    ],
)
def test_connector_required_and_forbidden_fields(
    tmp_path: Path, kind: str, required: set[str], forbidden: set[str]
) -> None:
    valid = valid_source_mapping(kind, INLINE_SCHEMA)
    for field in required:
        invalid = {key: value for key, value in valid.items() if key != field}
        with pytest.raises(SourceDeclarationError) as caught:
            parse_source_declarations({"x": invalid}, tmp_path / "catalog.yaml")
        assert f"data_sources.x.{field}" in {issue.path for issue in caught.value.issues}
    for field in forbidden:
        invalid = {**valid, field: "forbidden"}
        with pytest.raises(SourceDeclarationError):
            parse_source_declarations({"x": invalid}, tmp_path / "catalog.yaml")
```

`valid_source_mapping` contains one complete literal mapping per connector. Add separate parameterized assertions for invalid identifiers, non-built-in strings, invalid relation segments, remote locations without profiles, empty/non-string grains, and invalid CSV delimiter/quote/escape/header types.

- [ ] **Step 2: Write failing schema-reference containment tests**

```python
import yaml


def test_schema_ref_resolves_relative_to_catalog(tmp_path: Path) -> None:
    schema_path = tmp_path / "schemas" / "orders.yaml"
    schema_path.parent.mkdir()
    schema_path.write_text(yaml.safe_dump(INLINE_SCHEMA), encoding="utf-8")
    catalog_path = tmp_path / "catalog.yaml"

    sources = parse_source_declarations(
        {
            "orders": {
                "type": "parquet",
                "location": "orders/",
                "schema_ref": "schemas/orders.yaml",
                "grain": ["id"],
            }
        },
        catalog_path,
    )

    assert sources["orders"].schema.field("id").nullable is False


def test_schema_ref_cannot_escape_catalog_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text(yaml.safe_dump(INLINE_SCHEMA), encoding="utf-8")

    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations(
            {
                "orders": {
                    "type": "parquet",
                    "location": "orders/",
                    "schema_ref": "../outside.yaml",
                    "grain": ["id"],
                }
            },
            tmp_path / "catalog.yaml",
        )

    assert caught.value.issues[0].path == "data_sources.orders.schema_ref"
```

Add this explicit reference-failure matrix, with fixture helpers that create each filesystem condition:

```python
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", "schema_ref_missing"),
        ("malformed_yaml", "schema_ref_yaml"),
        ("non_mapping", "schema_ref_mapping"),
        ("symlink_escape", "schema_ref_escape"),
        ("duplicate_key", "schema_ref_duplicate_key"),
    ],
)
def test_schema_ref_failures_are_deterministic(
    tmp_path: Path, case: str, code: str
) -> None:
    catalog_path, source = schema_ref_failure_fixture(tmp_path, case)
    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations({"x": source}, catalog_path)
    assert caught.value.issues[0].code == code
    assert caught.value.issues == tuple(
        sorted(caught.value.issues, key=lambda issue: (issue.path, issue.message))
    )
```

A nested malformed schema assertion must prove its issue path is prefixed with `data_sources.x.schema_ref`.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run pytest -q tests/sources/test_source_catalog.py
```

Expected: collection fails because `selayer.sources.catalog` and connector config classes do not exist.

- [ ] **Step 4: Implement the closed connector dataclasses**

Use frozen, slotted dataclasses and a closed union. Do not retain arbitrary option mappings.

```python
@dataclass(frozen=True, slots=True)
class ParquetConfig:
    location: str
    credential_profile: str | None = None


@dataclass(frozen=True, slots=True)
class CsvConfig:
    location: str
    credential_profile: str | None = None
    delimiter: str = ","
    quote_char: str = '"'
    escape_char: str | None = None
    has_header: bool = True


@dataclass(frozen=True, slots=True)
class DeltaConfig:
    location: str
    credential_profile: str | None = None


@dataclass(frozen=True, slots=True)
class IcebergConfig:
    catalog_profile: str
    namespace: tuple[str, ...]
    table: str


@dataclass(frozen=True, slots=True)
class SqliteConfig:
    location: str
    relation: str


@dataclass(frozen=True, slots=True)
class DuckDbConfig:
    location: str
    relation: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    connection_profile: str
    relation: str


@dataclass(frozen=True, slots=True)
class PyArrowConfig:
    handle: str
```

The Python union is `SourceConnector`. A `kind` property or exhaustive helper returns the exact YAML discriminator without repeated `isinstance` switches outside `sources.config`.

- [ ] **Step 5: Implement strict parsing and secure schema_ref resolution**

Resolve references with `Path.resolve()` and `Path.is_relative_to(catalog_root)`. Open files only after containment succeeds. Reuse the catalog's duplicate-key-safe YAML loader rather than plain `yaml.safe_load` where possible; if reuse would create a cycle, move the duplicate-key loader into a private shared YAML module in this task.

Aggregate source and schema issues into one `SourceDeclarationError` sorted by `(path, message)`. Parsing occurs only after validation reports zero issues.

- [ ] **Step 6: Verify and commit Task 2**

```bash
uv run pytest -q tests/sources/test_source_catalog.py tests/sources/test_schema.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
git add src/selayer/sources tests/sources
git commit -m "feat(sources): parse connector declarations"
```

Expected: all commands exit zero.

---

### Task 3: Add runtime profiles, sanitized errors, and adapter contracts

**Files:**

- Create: `src/selayer/sources/profiles.py`
- Create: `src/selayer/sources/errors.py`
- Create: `src/selayer/sources/base.py`
- Create: `tests/sources/test_profiles.py`
- Create: `tests/sources/test_adapter_contract.py`

**Interfaces:**

- Consumes `ParsedSource`, `TableSchema`, and connector configs.
- Produces `RuntimeProfile`, `RuntimeProfileResolver`, `MappingProfileResolver`, and `ArrowProviderResolver`.
- Produces `SourceError`, `SourceDependencyError`, `SourceProfileError`, `SourceConnectionError`, `SourceSchemaError`, and `SourceReloadError`.
- Produces `SourceHandle`, `SourceStatus`, `ReloadResult`, `SourceScanRequirement`, `QueryBinding`, and private `SourceAdapter` protocol.
- Task 4 consumes these exact contracts.

- [ ] **Step 1: Write failing profile secrecy and defensive-copy tests**

```python
from selayer.sources.errors import SourceProfileError
from selayer.sources.profiles import MappingProfileResolver, RuntimeProfile


def test_runtime_profile_never_reprs_secret_values() -> None:
    values = {"access_key": "AKIA_SECRET", "secret_key": "SUPER_SECRET"}
    profile = RuntimeProfile("analytics_s3", values)
    values["secret_key"] = "changed"

    assert "AKIA_SECRET" not in repr(profile)
    assert "SUPER_SECRET" not in repr(profile)
    assert profile.value("secret_key") == "SUPER_SECRET"


def test_missing_profile_has_safe_domain_error() -> None:
    resolver = MappingProfileResolver({})

    with pytest.raises(SourceProfileError) as caught:
        resolver.resolve("missing", source_id="orders")

    assert caught.value.code == "missing_profile"
    assert caught.value.source_id == "orders"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
```

- [ ] **Step 2: Write failing adapter-contract tests**

```python
from dataclasses import dataclass

from selayer.sources.base import ReloadResult, SourceHandle, SourceStatus


@dataclass(frozen=True, slots=True)
class DemoResource:
    value: int


def test_handle_and_status_are_immutable_and_safe() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(1),
        schema=TableSchema((FieldSchema("id", ScalarType("int64"), False),)),
        snapshot="file-set:1",
        query_scoped=False,
    )
    status = SourceStatus.from_handle(handle, generation=3)
    result = ReloadResult("orders", 2, 3, status.schema_fingerprint, "file-set:1")

    assert status.generation == 3
    assert result.old_generation == 2
    assert "DemoResource" not in repr(status)
    assert "DemoResource" not in repr(result)
```

Define a fake adapter in the test that satisfies every protocol method and prove Pyright accepts it without casts.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py
```

Expected: collection fails because the profile, error, and base modules do not exist.

- [ ] **Step 4: Implement opaque profiles and provider resolution**

`RuntimeProfile` defensively copies values into `MappingProxyType`, uses `repr=False`, and never exposes the entire mapping. `value(name)` returns one value for internal adapter use. `MappingProfileResolver` copies its profile map.

Define `RuntimeProfileResolver` as a protocol with:

```text
resolve(name: str, *, source_id: str) -> RuntimeProfile
```

Define `ArrowProviderResolver` with:

```text
resolve(handle: str, *, source_id: str) -> Callable[[], ArrowObject]
```

`ArrowObject` is the supported union of PyArrow Dataset, Scanner, Table, and RecordBatchReader. Providers, not one-time objects, are the reloadable unit.

- [ ] **Step 5: Implement sanitized source errors**

Each source error stores `operation_id`, `source_id`, `code`, and a constant/sanitized `message`. Generate UUIDv4 operation IDs at the source lifecycle boundary. Do not retain driver exceptions. Raise constructed errors outside active `except` scopes so `__cause__` and `__context__` remain `None`.

- [ ] **Step 6: Implement adapter and metadata contracts**

`SourceHandle.resource` and cleanup callbacks use `repr=False`. `SourceStatus` and `ReloadResult` contain only IDs, connector kind, generation, fingerprint, safe snapshot/version, and health state.

The internal `SourceAdapter` protocol has exact methods:

```text
prepare(source, profiles, arrow_providers) -> SourceHandle
inspect_schema(handle) -> TableSchema
register(connection, stable_name, handle) -> None
bind_query(handle, requirement) -> QueryBinding | None
close(handle) -> None
```

`QueryBinding` is a context-managed registration cleanup record. `SourceScanRequirement` contains ordered physical columns and source-local planned filters; it contains no raw SQL.

- [ ] **Step 7: Verify and commit Task 3**

```bash
uv run pytest -q tests/sources/test_profiles.py tests/sources/test_adapter_contract.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
git add src/selayer/sources tests/sources
git commit -m "feat(sources): define adapter lifecycle contracts"
```

Expected: all commands exit zero.

---

### Task 4: Build SourceRegistry and atomically cut over QueryEngine/catalogs

**Files:**

- Create: `src/selayer/sources/registry.py`
- Create: `src/selayer/sources/adapters/__init__.py`
- Create: `src/selayer/sources/adapters/arrow.py`
- Modify: `src/selayer/model.py:33-40,139-226`
- Modify: `src/selayer/catalog.py:202-243,587-738`
- Modify: `src/selayer/query.py:36-114`
- Modify: `src/selayer/errors.py`
- Modify: `src/selayer/__init__.py`
- Modify: `src/selayer/okf/generation.py:52-163`
- Modify: `tests/conftest.py:9-71`
- Modify: `ecommerce_semantic_layer.yaml`
- Modify: `examples/e_commerce/selayer1.py`
- Modify: all catalog fixtures under `tests/`
- Create: `tests/sources/test_registry.py`
- Create: `tests/sources/test_arrow_adapter.py`
- Modify: `tests/test_catalog.py`
- Modify: `tests/test_query.py`
- Modify: `tests/integration/test_ecommerce.py`
- Modify: `tests/okf/test_generation.py`

**Interfaces:**

- Consumes Tasks 1-3.
- Replaces active `DataSource(name, type, path, grain)` with `DataSource(name, connector, schema, grain)`; no compatibility properties.
- Produces `SourceRegistry.create(layer, connection, profiles, arrow_providers, *, adapters=None) -> SourceRegistry`; `adapters` is private test injection and defaults to the closed built-in mapping.
- Produces `reload_source(source_id: str) -> ReloadResult`, `reload_all() -> tuple[ReloadResult, ...]`, `status(source_id: str) -> SourceStatus`, `bind(plan: QueryPlan) -> ContextManager[None]`, `execute_lock() -> ContextManager[None]`, and idempotent `close() -> None`.
- Adds `QueryEngine.reload_source(source_id: str) -> ReloadResult`, `reload_all() -> tuple[ReloadResult, ...]`, and `source_status(source_id: str) -> SourceStatus` while preserving query/plan signatures.
- Activates Parquet, CSV, and programmatic PyArrow adapters.

- [ ] **Step 1: Write failing atomic registry tests with fake adapters**

```python
from __future__ import annotations

import pyarrow as pa
import pyarrow.dataset as ds
import pytest

from selayer.sources.errors import SourceReloadError
from selayer.sources.registry import SourceRegistry


def test_reload_source_publishes_new_generation_without_engine_rebuild(
    registry_fixture,
) -> None:
    registry, connection, provider = registry_fixture
    assert connection.sql('select sum("value") from "events"').fetchone() == (1,)

    provider.next_dataset = ds.dataset(pa.table({"id": [1], "value": [9]}))
    result = registry.reload_source("events")

    assert result.old_generation == 1
    assert result.new_generation == 2
    assert connection.sql('select sum("value") from "events"').fetchone() == (9,)


def test_failed_reload_keeps_old_registration_queryable(registry_fixture) -> None:
    registry, connection, provider = registry_fixture
    provider.failure = RuntimeError("s3://user:secret@example/private")

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_source("events")

    assert connection.sql('select sum("value") from "events"').fetchone() == (1,)
    assert registry.status("events").generation == 1
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
```

The registry test file must include these independent assertions:

```python
def test_initial_failure_closes_every_prepared_handle(registry_builder) -> None:
    result = registry_builder(fail_prepare="b")
    assert result.error.code == "source_initialization_failed"
    assert result.closed_handles == ("a", "b")
    assert result.connection_closed


def test_reload_all_mid_swap_restores_old_handles(registry_fixture) -> None:
    registry, connection, provider = registry_fixture
    provider.fail_register_after = 1
    before = registry_statuses(registry)
    with pytest.raises(SourceReloadError):
        registry.reload_all()
    assert registry_statuses(registry) == before
    assert registered_values(connection) == {"events": 1}


def test_reload_order_and_cleanup_are_deterministic(registry_fixture) -> None:
    registry, _, provider = registry_fixture
    registry.reload_all()
    assert provider.register_order == tuple(sorted(provider.source_ids))
    assert provider.closed_old_handles == tuple(sorted(provider.source_ids))


def test_close_is_idempotent_and_status_fails_after_close(registry_fixture) -> None:
    registry, _, _ = registry_fixture
    registry.close()
    registry.close()
    with pytest.raises(SourceConnectionError):
        registry.status("events")
```

A two-thread barrier test holds a query binding while reload waits, then proves the generation changes only after the binding exits.

- [ ] **Step 2: Write failing Arrow registration and pushdown tests**

```python
import duckdb
import pyarrow as pa
import pyarrow.dataset as ds

from selayer.sources.adapters.arrow import ArrowDatasetAdapter


def test_arrow_dataset_registration_preserves_projection_and_filter_pushdown(
    parquet_source, profiles, providers
) -> None:
    adapter = ArrowDatasetAdapter()
    handle = adapter.prepare(parquet_source, profiles, providers)
    connection = duckdb.connect(":memory:")
    adapter.register(connection, "orders", handle)

    explain = "\n".join(
        row[1]
        for row in connection.execute(
            'explain select "id" from "orders" where "amount" > 10'
        ).fetchall()
    )

    assert "ARROW_SCAN" in explain
    assert "id" in explain
    assert "amount" in explain
    assert connection.execute(
        'select "id" from "orders" where "amount" > 10'
    ).fetchall() == [(2,)]
```

Add named tests `test_csv_uses_declared_arrow_schema`, `test_pyarrow_provider_is_invoked_again_on_reload`, `test_record_batch_reader_is_bound_once_per_query`, `test_registry_retains_dataset_until_close`, and `test_schema_mismatch_prevents_register`. Patch `polars.read_parquet` and `polars.read_csv` to raise in `test_query_engine_never_uses_eager_polars_source_reads`; a successful query proves neither function is called.

- [ ] **Step 3: Write failing QueryEngine lifecycle tests**

```python

def test_query_engine_delegates_source_reload(valid_layer, arrow_providers) -> None:
    with QueryEngine(valid_layer, arrow_providers=arrow_providers) as engine:
        before = engine.source_status("order_items")
        result = engine.reload_source("order_items")
        after = engine.source_status("order_items")

    assert result.old_generation == before.generation
    assert result.new_generation == after.generation
    assert after.generation == before.generation + 1


def test_reload_all_is_exposed_as_immutable_results(
    valid_layer, arrow_providers
) -> None:
    with QueryEngine(valid_layer, arrow_providers=arrow_providers) as engine:
        results = engine.reload_all()
    assert tuple(item.source_id for item in results) == tuple(
        sorted(valid_layer.data_sources)
    )
```

- [ ] **Step 4: Run focused tests and verify RED**

```bash
uv run pytest -q tests/sources/test_registry.py tests/sources/test_arrow_adapter.py tests/test_query.py
```

Expected: failures because `SourceRegistry`, Arrow adapters, new `DataSource`, and engine lifecycle methods are absent.

- [ ] **Step 5: Implement SourceRegistry with candidate-first atomic swaps**

Use one `threading.RLock` for DuckDB execution and registration mutation. Build and validate candidates before acquiring it. Store registrations in a private mapping of source ID to `(adapter, handle, generation)`.

For `reload_source`, call `connection.register(stable_name, candidate.resource)` only after schema verification. DuckDB replacement registration is the commit point. If adapter registration fails, explicitly re-register the previous handle before releasing the lock and raise `SourceReloadError` outside the caught exception scope.

For `reload_all`, prepare every candidate first, then swap in sorted source-ID order. On failure, restore each already-swapped old handle in reverse order. Publish generation changes only after every swap succeeds.

`bind(plan)` is a context manager. Persistent sources need no temporary binding; query-scoped adapters return cleanup records removed in `finally`.

- [ ] **Step 6: Implement Arrow adapters without eager Polars materialization**

Parquet and CSV use `pyarrow.dataset.dataset` with the declared Arrow schema. Programmatic providers are invoked during initial prepare and every reload. Dataset and Scanner handles are persistent; RecordBatchReader handles are query-scoped and recreated by providers.

The adapter registry is a closed internal mapping keyed by connector kind. Do not expose a plugin-registration method.

- [ ] **Step 7: Perform the atomic catalog/model/QueryEngine cutover**

Replace `DataSource` with:

```python
@dataclass(frozen=True, slots=True)
class DataSource:
    name: str
    connector: SourceConnector
    schema: TableSchema
    grain: tuple[str, ...]
```

Wire `catalog.load()` to Task 2 parsing and convert source/schema issues into sorted `CatalogIssue` values. Validate grain, dimension columns, relationship join columns, and every source-field reference against the declared schema. Compare dimension/fact semantic data types through an explicit compatibility table; do not cast.

Replace `QueryEngine._load_source` and direct `conn.register` calls with `SourceRegistry.create`. Its constructor becomes:

```text
QueryEngine(
    semantic_layer: SemanticLayer,
    *,
    profiles: RuntimeProfileResolver | None = None,
    arrow_providers: ArrowProviderResolver | None = None,
)
```

Keep `conn` private to registry-aware execution. `query()` obtains `plan`, enters `registry.bind(plan)`, compiles, and executes under the registry lock. Preserve existing parameterized-query sanitization.

- [ ] **Step 8: Migrate every active catalog and OKF source rendering in the same commit**

Update shared fixtures, e-commerce YAML, examples, integration tests, catalog tests, model tests, query tests, and OKF generation tests to the rewritten v1 shape. Use reusable schema files under `examples/e_commerce/schemas/` for the example and inline schemas for focused unit fixtures.

Update OKF source `Catalog Definition` rendering to include connector category, ordered field summary, schema fingerprint, and grain without locations or profile names. This prevents an intermediate broken main branch after `DataSource.path` disappears.

Delete every expectation for old `path` attributes and eager Polars loading. Add explicit tests that old `type/path/grain` catalogs fail with missing-schema/location issues rather than loading compatibly.

- [ ] **Step 9: Run the full atomic-cutover verification**

```bash
uv run pytest -q tests/sources tests/test_catalog.py tests/test_model.py tests/test_query.py tests/planning tests/compilation tests/okf/test_generation.py tests/integration/test_ecommerce.py
uv run pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run pyright src tests examples
uv run python examples/e_commerce/selayer1.py
```

Expected: all commands exit zero; no active catalog uses the old source shape; the example executes without eager Polars source reads.

- [ ] **Step 10: Commit Task 4**

```bash
git add src/selayer tests examples ecommerce_semantic_layer.yaml
git commit -m "refactor(sources): activate registry-backed loading"
```

---

### Task 5: Add named S3 transport profiles and MinIO coverage

**Files:**

- Modify: `src/selayer/sources/adapters/arrow.py`
- Modify: `src/selayer/sources/profiles.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/sources/test_s3.py`
- Modify: `tests/sources/test_arrow_adapter.py`

**Interfaces:**

- Consumes Parquet/CSV/Delta location configs and `RuntimeProfile`.
- Produces internal `s3_filesystem(profile) -> pyarrow.fs.S3FileSystem`.
- Adds optional `s3` extra with `boto3>=1.40,<2`.
- Does not add an `s3` source type.

- [ ] **Step 1: Write failing profile-to-filesystem secrecy tests**

```python
from selayer.sources.adapters.arrow import s3_filesystem
from selayer.sources.profiles import RuntimeProfile


def test_s3_profile_builds_filesystem_without_repr_leak(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_filesystem(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("selayer.sources.adapters.arrow.pafs.S3FileSystem", fake_filesystem)
    profile = RuntimeProfile(
        "analytics_s3",
        {
            "access_key": "ACCESS_SECRET",
            "secret_key": "SECRET_SECRET",
            "session_token": "TOKEN_SECRET",
            "region": "eu-central-1",
            "endpoint_override": "http://127.0.0.1:9000",
            "scheme": "http",
        },
    )

    s3_filesystem(profile)

    assert captured["access_key"] == "ACCESS_SECRET"
    assert captured["secret_key"] == "SECRET_SECRET"
    assert "SECRET_SECRET" not in repr(profile)
```

Add named tests for `test_boto_default_chain_credentials`, `test_boto_role_session_credentials`, `test_unknown_s3_profile_key_is_rejected`, `test_s3_defaults_to_https`, and `test_invalid_endpoint_is_rejected`. In every failure test, assert access key, secret key, session token, endpoint user-info, `repr(error.args)`, formatted traceback, cause, and context contain no secret sentinel.

- [ ] **Step 2: Write failing MinIO reload integration test**

```python
@pytest.mark.integration
def test_s3_parquet_reload_discovers_new_objects(minio_source_fixture) -> None:
    layer, profiles, upload_second_file = minio_source_fixture
    with QueryEngine(layer, profiles=profiles) as engine:
        assert engine.query(["row_count"])["row_count"].item() == 1
        upload_second_file()
        engine.reload_source("events")
        assert engine.query(["row_count"])["row_count"].item() == 2
```

The fixture starts MinIO with testcontainers, creates a bucket, uploads deterministic Parquet files, and builds a named endpoint profile. Skip only when Docker is unavailable; CI added in Task 9 must run it.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run pytest -q tests/sources/test_s3.py tests/sources/test_arrow_adapter.py
```

Expected: failure because S3 profile handling and dependency extra are absent.

- [ ] **Step 4: Implement S3 transport and optional dependency**

Use boto3 only to resolve named/default-chain credentials and role sessions. Pass frozen credentials to `pyarrow.fs.S3FileSystem`; never pass profile mappings to Arrow wholesale. Strip `s3://` for Dataset filesystem paths according to PyArrow's filesystem contract.

Add:

```toml
[project.optional-dependencies]
s3 = ["boto3>=1.40,<2"]
```

Add `testcontainers[minio]>=4.15,<5` to the dev group.

- [ ] **Step 5: Verify and commit Task 5**

```bash
uv sync --extra s3
uv run --extra s3 pytest -q tests/sources/test_s3.py tests/sources/test_arrow_adapter.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
git add src/selayer/sources tests/sources pyproject.toml uv.lock
git commit -m "feat(sources): add profiled S3 transport"
```

Expected: all commands exit zero; MinIO integration passes when Docker is available.

---

### Task 6: Add the delta-rs adapter and snapshot reload

**Files:**

- Create: `src/selayer/sources/adapters/delta.py`
- Modify: `src/selayer/sources/adapters/__init__.py`
- Modify: `src/selayer/sources/registry.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/sources/test_delta_adapter.py`

**Interfaces:**

- Consumes `DeltaConfig`, profiles, schema verification, and registry contracts.
- Produces `DeltaAdapter` using `deltalake.DeltaTable.to_pyarrow_dataset()`.
- Source status/reload snapshot is the safe Delta version string.
- Adds optional `delta` extra with `deltalake>=1.6.2,<2`.

- [ ] **Step 1: Write failing local Delta snapshot and reload tests**

```python
import pyarrow as pa
from deltalake import write_deltalake


def test_delta_reload_publishes_latest_snapshot(tmp_path, delta_layer_factory) -> None:
    location = tmp_path / "events.delta"
    write_deltalake(location, pa.table({"id": [1], "value": [10]}))
    layer = delta_layer_factory(location)

    with QueryEngine(layer) as engine:
        first = engine.source_status("events")
        assert engine.query(["total_value"])["total_value"].item() == 10

        write_deltalake(
            location,
            pa.table({"id": [2], "value": [20]}),
            mode="append",
        )
        result = engine.reload_source("events")

        assert result.old_generation == first.generation
        assert result.snapshot != first.snapshot
        assert engine.query(["total_value"])["total_value"].item() == 30
```

Add independent tests named `test_delta_registers_pyarrow_dataset`, `test_delta_explain_contains_arrow_scan_projection_and_filter`, `test_delta_schema_mismatch_preserves_old_snapshot`, `test_missing_deltalake_extra_is_source_dependency_error`, `test_delta_s3_profile_builds_filesystem`, `test_delta_status_contains_integer_version_only`, and `test_delta_handles_close_after_reload_and_engine_close`. Each failure test asserts the previous generation remains queryable and no location/profile secret reaches any error surface.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run --extra delta pytest -q tests/sources/test_delta_adapter.py
```

Expected: collection fails because `DeltaAdapter` and the `delta` extra are absent.

- [ ] **Step 3: Implement fresh-candidate Delta handles**

Create a new `DeltaTable` on every prepare/reload, then call `to_pyarrow_dataset(filesystem=...)`. Do not call `update_incremental()` on the active table because candidate preparation must not mutate the published generation.

Verify the Dataset schema before registration. Keep both `DeltaTable` and Dataset in an internal resource record with `repr=False`. Register only the Dataset. Use `table.version()` as the safe snapshot string.

Convert import failure to `SourceDependencyError(code="missing_delta_dependency")` without retaining the import exception.

- [ ] **Step 4: Add dependency extra and registry mapping**

```toml
[project.optional-dependencies]
delta = ["deltalake>=1.6.2,<2"]
```

Register `DeltaAdapter` internally for `DeltaConfig`; no public plugin hook.

- [ ] **Step 5: Verify and commit Task 6**

```bash
uv sync --extra delta
uv run --extra delta pytest -q tests/sources/test_delta_adapter.py tests/sources/test_registry.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
git add src/selayer/sources tests/sources pyproject.toml uv.lock
git commit -m "feat(sources): add delta snapshot adapter"
```

Expected: all commands exit zero.

---

### Task 7: Add PyIceberg query-scoped scans and refresh

**Files:**

- Create: `src/selayer/sources/adapters/iceberg.py`
- Modify: `src/selayer/sources/adapters/__init__.py`
- Modify: `src/selayer/sources/base.py`
- Modify: `src/selayer/sources/registry.py`
- Modify: `src/selayer/query.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/sources/test_iceberg_adapter.py`
- Modify: `tests/sources/test_registry.py`

**Interfaces:**

- Consumes `IcebergConfig`, `QueryPlan`, profile resolution, query binding, and schema verification.
- Produces `IcebergAdapter` with persistent PyIceberg table metadata handles and query-scoped `RecordBatchReader` bindings.
- Produces internal `requirements_for_plan(plan) -> Mapping[str, SourceScanRequirement]`.
- Adds optional `iceberg` extra with `pyiceberg[pyarrow,sql-sqlite]>=0.11.1,<0.12`.

- [ ] **Step 1: Write failing source-requirement extraction tests**

```python
from selayer.sources.registry import requirements_for_plan


def test_requirements_collect_columns_and_local_filters(item_margin_plan) -> None:
    requirements = requirements_for_plan(item_margin_plan)

    assert requirements["order_items"].columns == (
        "order_id",
        "product_id",
        "quantity",
        "total",
    )
    assert requirements["products"].columns == ("id", "category", "cost")
    assert tuple(item.dimension.name for item in requirements["products"].filters) == (
        "product_category",
    )
```

Add table-driven plans that isolate one source of columns at a time: `fact_reference_plan`, `dimension_plan`, `filter_plan`, `join_plan`, and `shared_column_plan`. Assert exact ordered columns for each. A metric-alias-only assertion must prove metric and measure IDs never appear in `SourceScanRequirement.columns`.

- [ ] **Step 2: Write failing PyIceberg projection/filter/refresh tests**

```python

def test_iceberg_binding_uses_fresh_scan_with_projection_and_filter(
    iceberg_table_fixture,
) -> None:
    layer, catalog_profile, append_snapshot, recording_scan = iceberg_table_fixture
    with QueryEngine(layer, profiles=catalog_profile) as engine:
        first = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A"]},
        )
        append_snapshot()
        engine.reload_source("events")
        second = engine.query(
            ["total_value"],
            ["category"],
            {"category": ["A"]},
        )

    assert first["total_value"].sum() == 10
    assert second["total_value"].sum() == 30
    assert recording_scan.selected_fields == ("id", "category", "value")
    assert recording_scan.row_filter == "category IN ('A')"
    assert recording_scan.reader_count == 2
```

Use a local SQL-backed PyIceberg catalog and local warehouse. Add named tests `test_iceberg_scalar_filter_translation`, `test_iceberg_list_filter_translation`, `test_iceberg_range_filter_translation`, `test_unsupported_filter_remains_residual_only`, `test_iceberg_schema_mismatch_preserves_snapshot`, `test_iceberg_status_exposes_snapshot_id_only`, `test_reader_closes_after_success_and_failure`, and `test_iceberg_never_installs_or_loads_duckdb_extension`. Assert both pushed and residual DuckDB results against independent Arrow calculations.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run --extra iceberg pytest -q tests/sources/test_iceberg_adapter.py tests/sources/test_registry.py
```

Expected: failures because Iceberg adapter, requirement extraction, and dependency extra do not exist.

- [ ] **Step 4: Implement stable source requirements and filter translation**

Collect physical fields from planned dimensions, planned filters, fact expression references, and join endpoints. Preserve first-seen order with a companion set.

Translate only source-local scalar equality, list membership, and inclusive range filters into PyIceberg expressions. Unsupported value types or expressions produce no pushdown expression; compiled DuckDB SQL still evaluates every filter with bound parameters.

- [ ] **Step 5: Implement query-scoped PyIceberg binding**

`prepare` loads a PyIceberg table through the named catalog profile and records snapshot metadata. `register` does not publish a reusable reader. `bind_query` creates `table.scan(row_filter=..., selected_fields=...).to_arrow_batch_reader()`, registers it under the stable source name for that locked execution, and returns cleanup that unregisters and closes the reader.

Reload loads or refreshes a separate candidate table handle before publication. Existing readers retain their old snapshot until their query finishes under the registry lock.

- [ ] **Step 6: Add dependency and verify Task 7**

```toml
[project.optional-dependencies]
iceberg = ["pyiceberg[pyarrow,sql-sqlite]>=0.11.1,<0.12"]
```

Run:

```bash
uv sync --extra iceberg
uv run --extra iceberg pytest -q tests/sources/test_iceberg_adapter.py tests/sources/test_registry.py tests/test_query.py
uv run pytest -q
uv run ruff check src/selayer/sources src/selayer/query.py tests/sources tests/test_query.py
uv run ruff format --check src/selayer/sources src/selayer/query.py tests/sources tests/test_query.py
uv run pyright src/selayer/sources src/selayer/query.py tests/sources tests/test_query.py
git add src/selayer/sources src/selayer/query.py tests/sources tests/test_query.py pyproject.toml uv.lock
git commit -m "feat(sources): add iceberg query bindings"
```

Expected: all commands exit zero.

---

### Task 8: Add SQLite, DuckDB-file, and PostgreSQL adapters

**Files:**

- Create: `src/selayer/sources/adapters/database.py`
- Modify: `src/selayer/sources/adapters/__init__.py`
- Modify: `src/selayer/sources/registry.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/sources/test_database_adapters.py`
- Create: `tests/sources/test_postgres_integration.py`

**Interfaces:**

- Consumes SQLite, DuckDB, and PostgreSQL configs plus runtime profiles.
- Produces `SqliteAdapter`, `DuckDbAdapter`, and `PostgresAdapter` using native DuckDB attach/scan behavior.
- Adds optional `postgres` extra with `psycopg[binary]>=3.3.4,<4`.
- Stable source names expose exactly one validated configured relation; no raw SQL catalog field is accepted.

- [ ] **Step 1: Write failing local SQLite and DuckDB-file integration tests**

```python
import duckdb
import sqlite3


def test_sqlite_and_duckdb_sources_query_and_reload(
    tmp_path, database_layer_factory
) -> None:
    sqlite_path = tmp_path / "reference.sqlite"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("create table codes (code text primary key, value integer not null)")
        connection.execute("insert into codes values ('A', 1)")

    duckdb_path = tmp_path / "facts.duckdb"
    with duckdb.connect(str(duckdb_path)) as connection:
        connection.execute("create table facts as select 1::bigint id, 10::bigint value")

    layer = database_layer_factory(sqlite_path, duckdb_path)
    with QueryEngine(layer) as engine:
        assert engine.query(["total_value"])["total_value"].item() == 10
        with duckdb.connect(str(duckdb_path)) as writer:
            writer.execute("insert into facts values (2, 20)")
        engine.reload_source("facts")
        assert engine.query(["total_value"])["total_value"].item() == 30
```

Add named tests `test_relation_segments_are_quoted`, `test_invalid_relation_segment_is_catalog_error`, `test_missing_extension_is_dependency_error`, `test_offline_policy_never_installs_extension`, `test_schema_mismatch_restores_old_database_view`, `test_duckdb_attachment_is_read_only`, `test_attachment_closes_on_engine_close`, and `test_database_errors_hide_location_and_dsn`. Use identifiers containing a reserved word to prove quoting and reject semicolons/comments as invalid segments.

- [ ] **Step 2: Write failing PostgreSQL service integration test**

```python
@pytest.mark.integration
def test_postgres_relation_reads_current_rows_and_reloads_metadata(
    postgres_source_fixture,
) -> None:
    layer, profiles, insert_row = postgres_source_fixture
    with QueryEngine(layer, profiles=profiles) as engine:
        assert engine.query(["total_value"])["total_value"].item() == 10
        insert_row(2, 20)
        assert engine.query(["total_value"])["total_value"].item() == 30
        status = engine.reload_source("facts")
        assert status.new_generation == status.old_generation + 1
```

The fixture uses `testcontainers[postgres]`, constructs a named profile, and never writes the DSN to assertion output. Add connection failure and credential leakage tests.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run --extra postgres pytest -q tests/sources/test_database_adapters.py tests/sources/test_postgres_integration.py
```

Expected: collection or adapter lookup fails because database adapters and the PostgreSQL extra are absent.

- [ ] **Step 4: Implement validated relation identifiers and native scans**

Validate relation names as one or more identifier segments matching `[a-zA-Z_][a-zA-Z0-9_]*`; quote every segment. Catalog authors cannot provide SQL fragments.

Use DuckDB parameter binding for locations/connection strings where supported and generated internal aliases for attachments. Load existing extensions first. Install extensions only when an explicit non-secret runtime profile permits installation; otherwise return `SourceDependencyError(code="extension_unavailable")`.

Build candidate attachments or scans under generated temporary aliases, introspect and verify schemas, then swap the stable semantic view under the registry lock. Clean up old aliases after publication. PostgreSQL profile values are converted to a DuckDB connection string internally and never stored in handle repr/status.

- [ ] **Step 5: Add dependency and verify Task 8**

```toml
[project.optional-dependencies]
postgres = ["psycopg[binary]>=3.3.4,<4"]
```

Add `testcontainers[postgres]>=4.15,<5` to the dev group.

Run:

```bash
uv sync --extra postgres
uv run --extra postgres pytest -q tests/sources/test_database_adapters.py tests/sources/test_postgres_integration.py
uv run pytest -q
uv run ruff check src/selayer/sources tests/sources
uv run ruff format --check src/selayer/sources tests/sources
uv run pyright src/selayer/sources tests/sources
git add src/selayer/sources tests/sources pyproject.toml uv.lock
git commit -m "feat(sources): add native database adapters"
```

Expected: all commands exit zero; PostgreSQL integration passes when Docker is available.

---

### Task 9: Complete cross-connector reload, OKF summaries, packaging, CI, and docs

**Files:**

- Modify: `src/selayer/sources/registry.py`
- Modify: `src/selayer/sources/errors.py`
- Modify: `src/selayer/okf/generation.py`
- Modify: `src/selayer/okf/bundle.py`
- Modify: `src/selayer/okf/validation.py`
- Modify: `src/selayer/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`
- Modify: `.github/copilot-instructions.md`
- Create: `.github/workflows/test.yml`
- Create: `tests/sources/test_reload_matrix.py`
- Modify: `tests/okf/test_generation.py`
- Modify: `tests/okf/test_retrieval.py`
- Modify: `tests/okf/test_validation.py`
- Modify: `tests/okf/test_documentation.py`
- Modify: `tests/integration/test_ecommerce.py`

**Interfaces:**

- Consumes every source adapter and registry lifecycle.
- Finalizes `selayer[delta]`, `[iceberg]`, `[postgres]`, `[s3]`, and `[all]` extras.
- Produces bounded catalog-derived OKF source schema summaries.
- Produces CI coverage that executes Docker-backed PostgreSQL and MinIO tests.
- Curates final public source lifecycle types/errors without exporting adapter internals.

- [ ] **Step 1: Write failing cross-connector reload rollback tests**

```python

def test_reload_all_is_all_or_nothing_across_adapter_modes(
    mixed_registry_fixture,
) -> None:
    registry, connection, generations, fail_database_candidate = mixed_registry_fixture
    before = {
        source_id: connection.sql(f'select count(*) from "{source_id}"').fetchone()
        for source_id in ("arrow_events", "delta_events", "database_events")
    }
    fail_database_candidate()

    with pytest.raises(SourceReloadError) as caught:
        registry.reload_all()

    after = {
        source_id: connection.sql(f'select count(*) from "{source_id}"').fetchone()
        for source_id in ("arrow_events", "delta_events", "database_events")
    }
    assert after == before
    assert {
        source_id: registry.status(source_id).generation
        for source_id in generations
    } == generations
    assert caught.value.code == "reload_all_failed"
```

Add independent tests `test_reload_all_mixed_success_returns_sorted_results`, `test_reload_all_rollback_closes_candidates`, `test_reload_all_waits_for_iceberg_query_binding`, `test_query_waits_for_reload_swap`, `test_close_waits_for_active_reload`, and `test_repeated_reload_has_constant_live_handle_count`. Parameterize source lifecycle failures across every connector and assert secret sentinels are absent from `str(error)`, `error.message`, `repr(error.args)`, formatted traceback, cause, and context.

- [ ] **Step 2: Write failing OKF schema-summary authority tests**

```python

def test_generated_source_context_contains_bounded_schema_summary(
    generated_bundle,
) -> None:
    source = generated_bundle.context_for("source.order_items", max_chars=4_000)
    assert "Schema fingerprint:" in source.content
    assert "Grain: order_id, product_id" in source.content
    assert "quantity: int32 (required)" in source.content
    assert "s3://" not in source.content
    assert "credential_profile" not in source.content


def test_curated_okf_schema_text_cannot_change_catalog_execution(
    generated_bundle, ecommerce_layer
) -> None:
    generated_bundle.replace_curated_text(
        "source.order_items", "quantity is a string and grain is customer_id"
    )
    regenerated = generated_bundle.sync(ecommerce_layer)

    assert ecommerce_layer.data_sources["order_items"].schema.field("quantity").type.name == "int32"
    assert ecommerce_layer.data_sources["order_items"].grain == (
        "order_id",
        "product_id",
    )
    assert "quantity is a string" in regenerated.concept("source.order_items").content
```

Update budget accounting for generated schema text. Validate generated fingerprints and safe source metadata. Preserve curated sections and unknown frontmatter fields.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
uv run --all-extras pytest -q tests/sources/test_reload_matrix.py tests/okf/test_generation.py tests/okf/test_retrieval.py tests/okf/test_validation.py
```

Expected: failures because mixed rollback hardening, OKF schema summaries, and full extras are incomplete.

- [ ] **Step 4: Harden reload and finish OKF generation**

Use one rollback journal of `(source_id, old_adapter, old_handle, old_generation)` entries. Restore in reverse swap order. Do not mutate published generations until all replacements succeed. Close candidates outside the lock after rollback/publication.

Generate schema summaries from `DataSource.schema`, never observed handles. Include only connector category, grain, fingerprint, bounded fields, declared nullability, and explicitly safe snapshot/freshness metadata. Exclude locations, relation connection details, profile names, and connector options.

- [ ] **Step 5: Finalize optional extras and public exports**

The final PEP 621 extras are:

```toml
[project.optional-dependencies]
delta = ["deltalake>=1.6.2,<2"]
iceberg = ["pyiceberg[pyarrow,sql-sqlite]>=0.11.1,<0.12"]
postgres = ["psycopg[binary]>=3.3.4,<4"]
s3 = ["boto3>=1.40,<2"]
all = [
  "boto3>=1.40,<2",
  "deltalake>=1.6.2,<2",
  "psycopg[binary]>=3.3.4,<4",
  "pyiceberg[pyarrow,sql-sqlite]>=0.11.1,<0.12",
]
```

Export `TableSchema`, `FieldSchema`, `SourceStatus`, `ReloadResult`, and the public source error classes from `selayer`. Do not export `SourceRegistry`, adapter classes, raw handles, profile values, or schema parser internals.

- [ ] **Step 6: Add CI and documentation**

Create `.github/workflows/test.yml` on Python 3.13. Install `uv`, run `uv sync --all-extras`, and execute the complete test/lint/type/build matrix. Docker-backed tests use testcontainers; the job must fail rather than skip PostgreSQL and MinIO tests when running in CI. Add a test helper that detects `CI=true` and treats unavailable Docker as a failure.

Update README and Copilot instructions with:

- complete connector matrix;
- inline/schema_ref examples;
- named profiles and secret rules;
- explicit reload examples;
- Arrow/Delta pushdown and Iceberg query-scoped behavior;
- optional extras;
- OKF advisory authority;
- no eager Polars source materialization.

- [ ] **Step 7: Run complete release verification**

```bash
uv sync --all-extras
uv run --all-extras pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run pyright src tests examples
uv run python examples/e_commerce/selayer1.py
rm -rf dist && uv build
uv run python - <<'PY'
from pathlib import Path
import zipfile

wheel = next(Path("dist").glob("*.whl"))
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
required = {
    "selayer/sources/schema.py",
    "selayer/sources/config.py",
    "selayer/sources/registry.py",
    "selayer/sources/adapters/arrow.py",
    "selayer/sources/adapters/delta.py",
    "selayer/sources/adapters/iceberg.py",
    "selayer/sources/adapters/database.py",
}
assert required <= set(names), required - set(names)
assert not any("legacy/" in name or "tests/" in name for name in names)
print(f"verified {wheel}: {len(names)} files")
PY
```

Expected: every command exits zero; all 8 connector types and S3 transport have integration evidence; the wheel contains source modules and no legacy/test content.

- [ ] **Step 8: Run the completion audit**

Map each design acceptance criterion to evidence:

- mandatory authoritative schemas → catalog/schema tests and migrated e-commerce catalog;
- Arrow pushdown → Arrow and Delta explain/integration tests;
- Python-owned lakehouse semantics → delta-rs/PyIceberg tests and dependency scan;
- all connectors → connector integration matrix;
- atomic reload/rollback → registry and mixed-adapter tests;
- schema verification → mismatch tests for every adapter;
- credential secrecy → profile/error/traceback tests;
- OKF advisory-only behavior → generation/sync/conflict tests;
- planner/compiler isolation → import scans;
- packaging → wheel assertion and extras installation.

Any unmapped criterion blocks completion.

- [ ] **Step 9: Commit Task 9**

```bash
git add src/selayer tests README.md .github pyproject.toml uv.lock
git commit -m "feat(sources): complete connector lifecycle"
```

---

## Spec Coverage

| Approved design requirement | Plan evidence |
|---|---|
| Mandatory complete inline/schema_ref schema | Tasks 1, 2, and atomic Task 4 cutover |
| Full recursive Arrow-compatible logical types | Task 1 |
| Closed connector union and rewritten schema version 1 | Tasks 2 and 4 |
| Catalog authority and runtime schema verification | Tasks 1, 2, 4, and every connector task |
| Private adapter seam and SourceRegistry | Tasks 3 and 4 |
| PyArrow Dataset projection/filter pushdown | Tasks 4, 5, and 6 |
| Python delta-rs snapshot ownership | Task 6 |
| Python PyIceberg snapshot/scan ownership | Task 7 |
| SQLite, DuckDB-file, and PostgreSQL native scans | Task 8 |
| S3 as named-profile transport | Task 5 |
| Programmatic Arrow provider reload | Task 4 |
| Atomic reload_source and reload_all rollback | Tasks 4 and 9 |
| Named runtime profiles and secret isolation | Tasks 3, 5, 8, and 9 |
| Optional connector extras | Tasks 5-9 |
| OKF generated advisory schema summaries | Tasks 4 and 9 |
| No eager Polars source materialization | Task 4 regression tests |
| No writes, watchers, arbitrary SQL, plugins, or extra engines | Global constraints plus final scope/import audit |
| Package, CI, examples, and connector integration matrix | Task 9 |

## Final Review Gate

After Task 9:

1. Generate a whole-branch diff from the implementation base.
2. Request independent standards and specification reviews in parallel.
3. Fix every Critical and Important issue and repeat review until both axes are clean.
4. Repeat the complete release verification at the reviewed head.
5. Use `superpowers:finishing-a-development-branch` to offer merge, PR, keep, or discard options.
