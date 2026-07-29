"""Tests for closed connector declarations and schema references."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from selayer.sources.catalog import (
    ParsedSource,
    SourceDeclarationError,
    SourceDeclarationIssue,
    parse_source_declarations,
    validate_source_declarations,
)
from selayer.sources.config import (
    CsvConfig,
    DeltaConfig,
    DuckDbConfig,
    IcebergConfig,
    ParquetConfig,
    PostgresConfig,
    PyArrowConfig,
    SourceConnector,
    SqliteConfig,
    connector_kind,
)
from selayer.sources.schema import ScalarType, TableSchema

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

INLINE_SCHEMA: dict[str, Any] = {
    "fields": [{"name": "id", "type": "int64", "nullable": False}]
}

KNOWN_KINDS = (
    "parquet",
    "csv",
    "delta",
    "iceberg",
    "sqlite",
    "duckdb",
    "postgres",
    "pyarrow",
)

CONNECTOR_TYPES = {
    "parquet": ParquetConfig,
    "csv": CsvConfig,
    "delta": DeltaConfig,
    "iceberg": IcebergConfig,
    "sqlite": SqliteConfig,
    "duckdb": DuckDbConfig,
    "postgres": PostgresConfig,
    "pyarrow": PyArrowConfig,
}


def valid_source_mapping(
    kind: str, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return one complete valid source mapping for the given connector kind."""

    common: dict[str, Any] = {
        "schema": schema if schema is not None else INLINE_SCHEMA,
        "grain": ["id"],
    }
    if kind == "parquet":
        return {"type": "parquet", "location": "data/orders/", **common}
    if kind == "csv":
        return {"type": "csv", "location": "data/orders.csv", **common}
    if kind == "delta":
        return {"type": "delta", "location": "data/orders_delta/", **common}
    if kind == "iceberg":
        return {
            "type": "iceberg",
            "catalog_profile": "warehouse",
            "namespace": ["analytics"],
            "table": "orders",
            **common,
        }
    if kind == "sqlite":
        return {
            "type": "sqlite",
            "location": "data/warehouse.db",
            "relation": "main.orders",
            **common,
        }
    if kind == "duckdb":
        return {
            "type": "duckdb",
            "location": "data/warehouse.duckdb",
            "relation": "orders",
            **common,
        }
    if kind == "postgres":
        return {
            "type": "postgres",
            "connection_profile": "warehouse_ro",
            "relation": "analytics.customers",
            **common,
        }
    if kind == "pyarrow":
        return {"type": "pyarrow", "handle": "orders_table", **common}
    raise AssertionError(f"unexpected connector kind {kind!r}")


def schema_ref_failure_fixture(
    tmp_path: Path, case: str
) -> tuple[Path, dict[str, Any]]:
    """Create the filesystem condition for a schema_ref failure case."""

    catalog_path = tmp_path / "catalog.yaml"
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    schema_path = schemas_dir / "orders.yaml"
    source: dict[str, Any] = {
        "type": "parquet",
        "location": "orders/",
        "schema_ref": "schemas/orders.yaml",
        "grain": ["id"],
    }

    if case == "missing":
        pass  # deliberately absent
    elif case == "malformed_yaml":
        schema_path.write_text("fields: [\n", encoding="utf-8")
    elif case == "non_mapping":
        schema_path.write_text(str(yaml.safe_dump([1, 2])), encoding="utf-8")
    elif case == "symlink_escape":
        outside = tmp_path.parent / "outside_escape.yaml"
        outside.write_text(str(yaml.safe_dump(INLINE_SCHEMA)), encoding="utf-8")
        schema_path.symlink_to(outside)
    elif case == "duplicate_key":
        schema_path.write_text(
            "fields:\n"
            "  - name: id\n"
            "    type: int64\n"
            "    nullable: false\n"
            "fields:\n"
            "  - name: id\n"
            "    type: int64\n"
            "    nullable: false\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError(f"unexpected case {case!r}")

    return catalog_path, source


# ---------------------------------------------------------------------------
# Step 1 — connector shape tests
# ---------------------------------------------------------------------------


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


def test_parsed_source_carries_name_schema_and_grain(tmp_path: Path) -> None:
    raw = {
        "orders": {
            "type": "parquet",
            "location": "data/orders/",
            "schema": INLINE_SCHEMA,
            "grain": ["id", "tenant_id"],
        }
    }
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    parsed = sources["orders"]
    assert isinstance(parsed, ParsedSource)
    assert parsed.name == "orders"
    assert parsed.grain == ("id", "tenant_id")
    assert isinstance(parsed.grain, tuple)
    assert isinstance(parsed.schema, TableSchema)
    assert parsed.schema.field("id").type == ScalarType("int64")


@pytest.mark.parametrize(
    ("source", "issue_path"),
    [
        (
            {"type": "unknown", "schema": INLINE_SCHEMA, "grain": ["id"]},
            "data_sources.x.type",
        ),
        (
            {"type": "parquet", "location": "x", "grain": ["id"]},
            "data_sources.x.schema",
        ),
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


@pytest.mark.parametrize(
    ("kind", "required", "forbidden"),
    [
        ("parquet", {"location"}, {"relation", "connection_profile", "handle"}),
        ("csv", {"location"}, {"relation", "connection_profile", "handle"}),
        ("delta", {"location"}, {"relation", "connection_profile", "handle"}),
        (
            "iceberg",
            {"catalog_profile", "namespace", "table"},
            {"location", "relation", "handle"},
        ),
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
        assert f"data_sources.x.{field}" in {
            issue.path for issue in caught.value.issues
        }
    for field in forbidden:
        invalid = {**valid, field: "forbidden"}
        with pytest.raises(SourceDeclarationError):
            parse_source_declarations({"x": invalid}, tmp_path / "catalog.yaml")


@pytest.mark.parametrize("kind", KNOWN_KINDS)
def test_every_connector_kind_parses(tmp_path: Path, kind: str) -> None:
    """Every known kind round-trips through parse with a correct connector."""

    raw = {"x": valid_source_mapping(kind)}
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    expected_type = CONNECTOR_TYPES[kind]
    assert isinstance(sources["x"].connector, expected_type)
    assert connector_kind(sources["x"].connector) == kind


@pytest.mark.parametrize(
    "name",
    ["1bad", "UPPER", "with-dash", "with space", ""],
)
def test_rejects_invalid_source_identifier(tmp_path: Path, name: str) -> None:
    issues = validate_source_declarations(
        {name: valid_source_mapping("parquet")}, tmp_path / "catalog.yaml"
    )
    assert f"data_sources.{name}" in {issue.path for issue in issues}


@pytest.mark.parametrize("bad_type", ["unknown", "", 123, None])
def test_rejects_non_builtin_type(tmp_path: Path, bad_type: object) -> None:
    source = {**valid_source_mapping("parquet"), "type": bad_type}
    issues = validate_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert "data_sources.x.type" in {issue.path for issue in issues}


@pytest.mark.parametrize(
    ("kind", "relation"),
    [
        ("sqlite", ""),
        ("sqlite", "a.b.c"),
        ("sqlite", ".a"),
        ("sqlite", "a."),
        ("sqlite", "a-b"),
        ("duckdb", ""),
        ("duckdb", "a.b.c"),
        ("postgres", ""),
        ("postgres", "a.b.c.d"),
        ("postgres", ".a"),
    ],
)
def test_rejects_invalid_relation_segments(
    tmp_path: Path, kind: str, relation: str
) -> None:
    source = {**valid_source_mapping(kind), "relation": relation}
    issues = validate_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert "data_sources.x.relation" in {issue.path for issue in issues}


@pytest.mark.parametrize("kind", ["parquet", "csv", "delta"])
@pytest.mark.parametrize(
    "location",
    ["s3://bucket/path/", "gs://bucket/data/", "abfss://acct/container/path/"],
)
def test_rejects_remote_location_without_credential_profile(
    tmp_path: Path, kind: str, location: str
) -> None:
    source = {**valid_source_mapping(kind), "location": location}
    issues = validate_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert "data_sources.x.location" in {issue.path for issue in issues}


def test_remote_location_with_credential_profile_is_accepted(
    tmp_path: Path,
) -> None:
    source = {
        "type": "parquet",
        "location": "s3://bucket/path/",
        "credential_profile": "analytics_s3",
        "schema": INLINE_SCHEMA,
        "grain": ["id"],
    }
    sources = parse_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert sources["x"].connector == ParquetConfig("s3://bucket/path/", "analytics_s3")


@pytest.mark.parametrize(
    "grain",
    [[], [""], ["id", ""], [1], ["id", 1], "id", None, {"id"}, [True]],
)
def test_rejects_empty_or_non_string_grain(tmp_path: Path, grain: object) -> None:
    source = {**valid_source_mapping("parquet"), "grain": grain}
    issues = validate_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert "data_sources.x.grain" in {issue.path for issue in issues}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delimiter", "ab"),
        ("delimiter", ""),
        ("delimiter", 1),
        ("quote_char", "ab"),
        ("quote_char", ""),
        ("quote_char", 1),
        ("escape_char", "ab"),
        ("escape_char", 1),
        ("has_header", "yes"),
        ("has_header", 1),
    ],
)
def test_csv_rejects_invalid_option_types(
    tmp_path: Path, field: str, value: object
) -> None:
    source = {**valid_source_mapping("csv"), field: value}
    issues = validate_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    assert f"data_sources.x.{field}" in {issue.path for issue in issues}


def test_csv_parses_with_defaults_and_overrides(tmp_path: Path) -> None:
    raw = {
        "x": {
            "type": "csv",
            "location": "data/orders.csv",
            "credential_profile": "analytics_s3",
            "delimiter": ";",
            "quote_char": "'",
            "escape_char": "\\",
            "has_header": False,
            "schema": INLINE_SCHEMA,
            "grain": ["id"],
        }
    }
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    assert sources["x"].connector == CsvConfig(
        "data/orders.csv",
        "analytics_s3",
        ";",
        "'",
        "\\",
        False,
    )


def test_csv_parses_with_defaults_only(tmp_path: Path) -> None:
    raw = {"x": valid_source_mapping("csv")}
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    assert sources["x"].connector == CsvConfig(
        "data/orders.csv", None, ",", '"', None, True
    )


def test_duckdb_parses_with_read_only_override(tmp_path: Path) -> None:
    raw = {
        "x": {
            **valid_source_mapping("duckdb"),
            "read_only": False,
        }
    }
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    assert sources["x"].connector == DuckDbConfig(
        "data/warehouse.duckdb", "orders", False
    )


def test_iceberg_parses_multi_segment_namespace(tmp_path: Path) -> None:
    raw = {
        "x": {
            **valid_source_mapping("iceberg"),
            "namespace": ["analytics", "staging"],
        }
    }
    sources = parse_source_declarations(raw, tmp_path / "catalog.yaml")
    assert sources["x"].connector == IcebergConfig(
        "warehouse", ("analytics", "staging"), "orders"
    )


# ---------------------------------------------------------------------------
# Step 2 — schema-reference containment tests
# ---------------------------------------------------------------------------


def test_schema_ref_resolves_relative_to_catalog(tmp_path: Path) -> None:
    schema_path = tmp_path / "schemas" / "orders.yaml"
    schema_path.parent.mkdir()
    schema_path.write_text(str(yaml.safe_dump(INLINE_SCHEMA)), encoding="utf-8")
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
    outside.write_text(str(yaml.safe_dump(INLINE_SCHEMA)), encoding="utf-8")

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


def test_schema_ref_nested_schema_error_is_prefixed(tmp_path: Path) -> None:
    schema_path = tmp_path / "schemas" / "orders.yaml"
    schema_path.parent.mkdir()
    schema_path.write_text(
        str(
            yaml.safe_dump(
                {
                    "fields": [
                        {
                            "name": "id",
                            "type": "not_a_real_type",
                            "nullable": False,
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )

    issues = validate_source_declarations(
        {
            "x": {
                "type": "parquet",
                "location": "orders/",
                "schema_ref": "schemas/orders.yaml",
                "grain": ["id"],
            }
        },
        tmp_path / "catalog.yaml",
    )

    assert issues
    for issue in issues:
        assert issue.path.startswith("data_sources.x.schema_ref")


# ---------------------------------------------------------------------------
# Aggregate-error determinism and public-model contract
# ---------------------------------------------------------------------------


def test_aggregate_issues_are_sorted_by_path_then_message(tmp_path: Path) -> None:
    raw = {
        "zeta": {"type": "unknown", "schema": INLINE_SCHEMA, "grain": ["id"]},
        "alpha": {
            "type": "parquet",
            "location": "x",
            "grain": [],  # missing schema + empty grain → two issues for alpha
        },
    }
    issues = validate_source_declarations(raw, tmp_path / "catalog.yaml")
    assert issues == tuple(
        sorted(issues, key=lambda issue: (issue.path, issue.message))
    )
    assert len(issues) >= 3


def test_validate_returns_empty_tuple_for_valid_catalog(tmp_path: Path) -> None:
    raw = {
        "orders": valid_source_mapping("parquet"),
        "customers": valid_source_mapping("postgres"),
    }
    issues = validate_source_declarations(raw, tmp_path / "catalog.yaml")
    assert issues == ()


def test_validate_rejects_non_mapping_root(tmp_path: Path) -> None:
    issues = validate_source_declarations([], tmp_path / "catalog.yaml")
    assert len(issues) == 1
    assert issues[0].path == "data_sources"


def test_parse_raises_on_any_issue(tmp_path: Path) -> None:
    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations({"x": {"type": "unknown"}}, tmp_path / "catalog.yaml")
    assert caught.value.issues
    assert isinstance(caught.value.issues[0], SourceDeclarationIssue)


@pytest.mark.parametrize("kind", KNOWN_KINDS)
def test_connector_kind_is_exhaustive(kind: str) -> None:
    expected_type = CONNECTOR_TYPES[kind]
    # Build a minimal instance per kind using positional args matching the
    # dataclass constructors defined in config.py.
    if kind == "parquet":
        instance: SourceConnector = ParquetConfig("loc")
    elif kind == "csv":
        instance = CsvConfig("loc")
    elif kind == "delta":
        instance = DeltaConfig("loc")
    elif kind == "iceberg":
        instance = IcebergConfig("prof", ("ns",), "tbl")
    elif kind == "sqlite":
        instance = SqliteConfig("loc", "rel")
    elif kind == "duckdb":
        instance = DuckDbConfig("loc", "rel")
    elif kind == "postgres":
        instance = PostgresConfig("prof", "rel")
    else:
        instance = PyArrowConfig("handle")
    assert connector_kind(instance) == kind
    assert isinstance(instance, expected_type)


# ---------------------------------------------------------------------------
# Immutability and frozen/slotted contract
# ---------------------------------------------------------------------------


def test_connector_configs_are_frozen_and_slotted() -> None:
    # CPython 3.13 raises FrozenInstanceError(AttributeError) for existing
    # fields and TypeError for non-existent attributes on frozen+slotted.
    config = ParquetConfig("loc", "prof")
    with pytest.raises(AttributeError):
        config.location = "other"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        config.extra = "value"  # type: ignore[attr-defined]


def test_source_declaration_issue_is_frozen_and_slotted() -> None:
    issue = SourceDeclarationIssue("code", "path", "message")
    with pytest.raises(AttributeError):
        issue.code = "other"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        issue.extra = "value"  # type: ignore[attr-defined]


def test_parsed_source_is_frozen_and_slotted() -> None:
    parsed = ParsedSource(
        "orders",
        ParquetConfig("loc"),
        TableSchema(()),
        ("id",),
    )
    with pytest.raises(AttributeError):
        parsed.name = "other"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        parsed.extra = "value"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Regression — credentials never leak through reprs or schema_ref errors
# ---------------------------------------------------------------------------
#
# Binding requirement: credentials and authenticated locations never appear
# in catalogs, reprs, or exception/error text.  These tests guard against
# default dataclass reprs echoing a ``location`` whose URI carries embedded
# userinfo (``scheme://user:password@host``) and against ``_resolve_schema_ref``
# error messages echoing a supplied reference verbatim.

# A location whose URI authority carries embedded credentials.
AUTHENTICATED_LOCATION = "s3://AKIATOPSECRET:hunter2@warehouse/orders/"
# Markers that must never survive into any rendered diagnostic.
SECRET_MARKERS = ("AKIATOPSECRET", "hunter2")

# A schema reference whose value carries embedded userinfo.
SECRET_SCHEMA_REF = "https://token:hunter2@host/orders.yaml"


@pytest.mark.parametrize(
    "connector",
    [
        ParquetConfig(AUTHENTICATED_LOCATION),
        ParquetConfig(AUTHENTICATED_LOCATION, "analytics_s3"),
        CsvConfig(AUTHENTICATED_LOCATION),
        CsvConfig(AUTHENTICATED_LOCATION, "analytics_s3"),
        DeltaConfig(AUTHENTICATED_LOCATION),
        DeltaConfig(AUTHENTICATED_LOCATION, "analytics_s3"),
        SqliteConfig(AUTHENTICATED_LOCATION, "main.orders"),
        DuckDbConfig(AUTHENTICATED_LOCATION, "orders"),
        DuckDbConfig(AUTHENTICATED_LOCATION, "orders", False),
    ],
)
def test_location_connector_reprs_redact_authenticated_userinfo(
    connector: SourceConnector,
) -> None:
    rendered = repr(connector)
    for marker in SECRET_MARKERS:
        assert marker not in rendered
    # Non-secret diagnostics (scheme + host) remain useful.
    assert "s3://warehouse/orders/" in rendered


@pytest.mark.parametrize(
    "connector",
    [
        # These identifier-only connectors carry free-form string fields
        # (table, namespace, handle, relation) that are validated only as
        # non-empty strings, so they could embed URI userinfo if validation
        # rules ever relax.  Prove every field is redacted regardless.
        IcebergConfig(
            "warehouse",
            ("s3://AKIATOPSECRET:hunter2@ns/analytics",),
            "s3://AKIATOPSECRET:hunter2@host/orders",
        ),
        PostgresConfig("warehouse_ro", "s3://AKIATOPSECRET:hunter2@host/customers"),
        PyArrowConfig("s3://AKIATOPSECRET:hunter2@handle/orders"),
    ],
)
def test_non_location_connector_reprs_redact_credential_bearing_fields(
    connector: SourceConnector,
) -> None:
    rendered = repr(connector)
    for marker in SECRET_MARKERS:
        assert marker not in rendered, rendered
    # The ``userinfo@`` authority is fully stripped from every string field,
    # so no residual ``@`` survives in the repr.
    assert "@" not in rendered
    # Scheme/host diagnostics remain useful (not over-redacted).
    assert "s3://" in rendered


def test_parsed_source_repr_redacts_authenticated_location() -> None:
    parsed = ParsedSource(
        "orders",
        ParquetConfig(AUTHENTICATED_LOCATION, "analytics_s3"),
        TableSchema(()),
        ("id",),
    )
    rendered = repr(parsed)
    for marker in SECRET_MARKERS:
        assert marker not in rendered
    assert "s3://warehouse/orders/" in rendered


def test_parsed_source_repr_redacts_grain_credentials() -> None:
    # ``grain`` is a tuple of non-empty strings that could carry embedded URI
    # userinfo; prove the ParsedSource repr sanitizes it (and not just the
    # delegated connector repr).
    parsed = ParsedSource(
        "orders",
        ParquetConfig("s3://warehouse/orders/", "analytics_s3"),
        TableSchema(()),
        ("s3://AKIATOPSECRET:hunter2@grain/id",),
    )
    rendered = repr(parsed)
    for marker in SECRET_MARKERS:
        assert marker not in rendered, rendered
    assert "@" not in rendered
    # The sanitized grain value remains a useful diagnostic.
    assert "grain/id" in rendered


def test_schema_ref_failure_messages_redact_userinfo(tmp_path: Path) -> None:
    # A schema_ref whose value embeds credentials; it resolves to a path
    # that is not a file, producing schema_ref_missing.
    source: dict[str, Any] = {
        "type": "parquet",
        "location": "orders/",
        "schema_ref": SECRET_SCHEMA_REF,
        "grain": ["id"],
    }
    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    error = caught.value
    messages = [issue.message for issue in error.issues]

    # Every representation of the error must redact the embedded secret.
    for rendered in (str(error), repr(error), repr(error.args), *messages):
        for marker in SECRET_MARKERS:
            assert marker not in rendered, rendered

    # The sanitized reference (host, no userinfo) remains a useful diagnostic.
    assert any("host/orders.yaml" in message for message in messages)


def test_schema_ref_yaml_error_message_redacts_userinfo(tmp_path: Path) -> None:
    # Create the referenced file with invalid YAML so the schema_ref_yaml
    # branch echoes the (sanitized) reference.
    ref_path = tmp_path / SECRET_SCHEMA_REF
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text("fields: [\n", encoding="utf-8")
    source: dict[str, Any] = {
        "type": "parquet",
        "location": "orders/",
        "schema_ref": SECRET_SCHEMA_REF,
        "grain": ["id"],
    }
    with pytest.raises(SourceDeclarationError) as caught:
        parse_source_declarations({"x": source}, tmp_path / "catalog.yaml")
    error = caught.value
    messages = [issue.message for issue in error.issues]

    for rendered in (str(error), repr(error), repr(error.args), *messages):
        for marker in SECRET_MARKERS:
            assert marker not in rendered, rendered
    assert any("host/orders.yaml" in message for message in messages)
