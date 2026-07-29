"""Strict parsing of closed connector declarations and schema references.

This module validates a raw ``data_sources`` mapping into deterministic
:class:`SourceDeclarationIssue` aggregates, and parses it into immutable
:class:`ParsedSource` objects.  Parsing occurs only after validation reports
zero issues.

Design pillars:

* **Closed connector union** — every source resolves to exactly one frozen
  ``SourceConnector`` config; arbitrary option mappings are never retained.
* **Strict field validation** — each connector type has a fixed allowed-field
  set; unknown or secret fields are rejected at their own path.
* **Secure schema_ref resolution** — references are resolved with
  ``Path.resolve()`` and contained within the catalog root *before* any file
  is opened; symlinks and ``..`` traversal cannot escape.
* **Deterministic aggregate errors** — all independent issues are collected
  and sorted by ``(path, message)`` so error output is reproducible.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeGuard

import yaml

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
    _sanitize_location,
)
from selayer.sources.schema import (
    TableSchema,
    parse_schema_document,
    validate_schema_document,
)

__all__ = [
    "ParsedSource",
    "SourceDeclarationError",
    "SourceDeclarationIssue",
    "parse_source_declarations",
    "validate_source_declarations",
]


# ---------------------------------------------------------------------------
# Issue, error, and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceDeclarationIssue:
    code: str
    path: str
    message: str


class SourceDeclarationError(ValueError):
    """Raised when source declarations cannot be resolved into parsed sources."""

    def __init__(self, issues: tuple[SourceDeclarationIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        )


@dataclass(frozen=True, slots=True)
class ParsedSource:
    name: str
    connector: SourceConnector
    schema: TableSchema
    grain: tuple[str, ...]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_SQL_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

_REMOTE_SCHEMES = frozenset(
    {
        "s3",
        "s3a",
        "s3n",
        "gs",
        "gcs",
        "abfs",
        "abfss",
        "wasb",
        "wasbs",
        "hdfs",
        "http",
        "https",
        "ftp",
        "ftps",
    }
)

_SECRET_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "private_key",
        "credential",
        "credentials",
        "username",
        "user",
    }
)

# Allowed keys per connector type (includes the common keys type/schema/
# schema_ref/grain plus connector-specific keys).
_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "parquet": frozenset(
        {"type", "location", "credential_profile", "schema", "schema_ref", "grain"}
    ),
    "csv": frozenset(
        {
            "type",
            "location",
            "credential_profile",
            "delimiter",
            "quote_char",
            "escape_char",
            "has_header",
            "schema",
            "schema_ref",
            "grain",
        }
    ),
    "delta": frozenset(
        {"type", "location", "credential_profile", "schema", "schema_ref", "grain"}
    ),
    "iceberg": frozenset(
        {
            "type",
            "catalog_profile",
            "namespace",
            "table",
            "schema",
            "schema_ref",
            "grain",
        }
    ),
    "sqlite": frozenset(
        {"type", "location", "relation", "schema", "schema_ref", "grain"}
    ),
    "duckdb": frozenset(
        {"type", "location", "relation", "read_only", "schema", "schema_ref", "grain"}
    ),
    "postgres": frozenset(
        {"type", "connection_profile", "relation", "schema", "schema_ref", "grain"}
    ),
    "pyarrow": frozenset({"type", "handle", "schema", "schema_ref", "grain"}),
}

# Required keys per connector type (connector-specific only; schema/schema_ref
# and grain are validated universally).
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "parquet": frozenset({"location"}),
    "csv": frozenset({"location"}),
    "delta": frozenset({"location"}),
    "iceberg": frozenset({"catalog_profile", "namespace", "table"}),
    "sqlite": frozenset({"location", "relation"}),
    "duckdb": frozenset({"location", "relation"}),
    "postgres": frozenset({"connection_profile", "relation"}),
    "pyarrow": frozenset({"handle"}),
}


# ---------------------------------------------------------------------------
# Type guards and helpers
# ---------------------------------------------------------------------------


def _is_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _is_valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _is_remote_location(location: str) -> bool:
    scheme_sep = location.find("://")
    if scheme_sep <= 0:
        return False
    scheme = location[:scheme_sep].lower()
    return scheme in _REMOTE_SCHEMES


def _sorted_unknown_keys(
    raw: Mapping[object, object], allowed: frozenset[str]
) -> list[str]:
    """Return unknown mapping keys deterministically ordered by ``str``.

    Mirrors the heterogeneous-key-safe ordering in
    :mod:`selayer.sources.schema`: keys are projected through ``str()``
    before ordering and de-duplication so heterogeneous key sets never
    raise ``TypeError`` out of validation.
    """

    seen: set[str] = set()
    unknown: list[str] = []
    for key in raw:
        key_text = str(key)
        if key_text in allowed:
            continue
        if key_text not in seen:
            seen.add(key_text)
            unknown.append(key_text)
    unknown.sort()
    return unknown


# ---------------------------------------------------------------------------
# Duplicate-key-safe YAML loading
# ---------------------------------------------------------------------------


def _compose_and_construct(text: str) -> tuple[yaml.Node | None, object]:
    """Compose the YAML node tree and construct Python objects from it.

    Returning the node tree lets us detect duplicate mapping keys (which
    PyYAML silently collapses) while still using ``SafeLoader`` semantics.
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


def _collect_duplicate_keys(
    node: yaml.Node, path: str, issues: list[SourceDeclarationIssue]
) -> None:
    """Walk a YAML node tree collecting duplicate mapping-key issues."""

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
                    issues.append(
                        SourceDeclarationIssue(
                            "schema_ref_duplicate_key",
                            path,
                            f"duplicate key {key!r} in schema reference",
                        )
                    )
                else:
                    seen.add(key)
            _collect_duplicate_keys(value_node, child_path, issues)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _collect_duplicate_keys(item, f"{path}[{index}]", issues)


# ---------------------------------------------------------------------------
# Schema-reference resolution
# ---------------------------------------------------------------------------


def _resolve_schema_ref(
    schema_ref: str,
    catalog_path: Path,
    base_path: str,
    issues: list[SourceDeclarationIssue],
) -> None:
    """Resolve, load, and validate a schema reference file.

    All filesystem access occurs only after the containment check passes.
    Issues are appended to *issues* and prefixed with *base_path*.  Supplied
    references are sanitized before being echoed in messages so embedded URI
    userinfo can never leak into error text.
    """

    # Sanitize once; used in every message that echoes the supplied reference.
    safe_ref = _sanitize_location(schema_ref)
    catalog_root = catalog_path.parent.resolve()
    candidate = catalog_path.parent / schema_ref
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_escape",
                base_path,
                "schema_ref path cannot be resolved safely",
            )
        )
        return
    if not resolved.is_relative_to(catalog_root):
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_escape",
                base_path,
                "schema_ref must resolve within the catalog root directory",
            )
        )
        return
    if not resolved.is_file():
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_missing",
                base_path,
                f"schema_ref file {safe_ref!r} does not exist",
            )
        )
        return
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_missing",
                base_path,
                f"schema_ref file {safe_ref!r} cannot be read",
            )
        )
        return
    try:
        node, data = _compose_and_construct(text)
    except yaml.YAMLError:
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_yaml",
                base_path,
                f"schema_ref file {safe_ref!r} is not valid YAML",
            )
        )
        return
    dup_issues: list[SourceDeclarationIssue] = []
    if node is not None:
        _collect_duplicate_keys(node, base_path, dup_issues)
    if dup_issues:
        issues.extend(dup_issues)
        return
    if not isinstance(data, Mapping):
        issues.append(
            SourceDeclarationIssue(
                "schema_ref_mapping",
                base_path,
                "schema_ref root must be a mapping",
            )
        )
        return
    for issue in validate_schema_document(data, path=base_path):
        issues.append(SourceDeclarationIssue(issue.code, issue.path, issue.message))


# ---------------------------------------------------------------------------
# Per-source validation
# ---------------------------------------------------------------------------


def _validate_source(
    name: object,
    value: object,
    catalog_path: Path,
    issues: list[SourceDeclarationIssue],
) -> None:
    key_text = str(name)
    base = f"data_sources.{key_text}"

    if not _is_valid_identifier(name):
        issues.append(
            SourceDeclarationIssue(
                "source_name_invalid",
                base,
                f"data source name must match [a-z][a-z0-9_]*, got {key_text!r}",
            )
        )

    if not isinstance(value, Mapping):
        issues.append(
            SourceDeclarationIssue(
                "source_not_mapping",
                base,
                "data source declaration must be a mapping",
            )
        )
        return

    # --- type discriminator ---
    type_raw = value.get("type")
    type_path = f"{base}.type"
    if type_raw is None:
        issues.append(
            SourceDeclarationIssue("type_missing", type_path, "type is required")
        )
        return
    if not _is_str(type_raw):
        issues.append(
            SourceDeclarationIssue("unknown_type", type_path, "type must be a string")
        )
        return
    if type_raw not in _ALLOWED_FIELDS:
        issues.append(
            SourceDeclarationIssue(
                "unknown_type",
                type_path,
                f"unknown connector type {type_raw!r}",
            )
        )
        return

    kind = type_raw
    allowed = _ALLOWED_FIELDS[kind]

    # --- unknown and secret fields ---
    for field_name in _sorted_unknown_keys(value, allowed):
        field_path = f"{base}.{field_name}"
        if field_name in _SECRET_FIELDS:
            issues.append(
                SourceDeclarationIssue(
                    "secret_field",
                    field_path,
                    f"secret field {field_name!r} is not permitted in "
                    "source declarations",
                )
            )
        else:
            issues.append(
                SourceDeclarationIssue(
                    "unknown_field",
                    field_path,
                    f"unknown field {field_name!r} for connector type {kind!r}",
                )
            )

    # --- missing required fields ---
    for field_name in sorted(_REQUIRED_FIELDS[kind]):
        if field_name not in value:
            issues.append(
                SourceDeclarationIssue(
                    "field_missing",
                    f"{base}.{field_name}",
                    f"{field_name} is required for connector type {kind!r}",
                )
            )

    # --- schema / schema_ref exclusivity ---
    has_schema = "schema" in value
    has_schema_ref = "schema_ref" in value
    schema_path = f"{base}.schema"
    if not has_schema and not has_schema_ref:
        issues.append(
            SourceDeclarationIssue(
                "schema_missing",
                schema_path,
                "exactly one of schema or schema_ref is required",
            )
        )
    elif has_schema and has_schema_ref:
        issues.append(
            SourceDeclarationIssue(
                "schema_conflict",
                schema_path,
                "schema and schema_ref are mutually exclusive",
            )
        )

    # --- grain ---
    grain = value.get("grain")
    grain_path = f"{base}.grain"
    if grain is None:
        issues.append(
            SourceDeclarationIssue("field_missing", grain_path, "grain is required")
        )
    elif not isinstance(grain, list):
        issues.append(
            SourceDeclarationIssue(
                "grain_invalid", grain_path, "grain must be a list of column names"
            )
        )
    elif not grain:
        issues.append(
            SourceDeclarationIssue(
                "grain_invalid", grain_path, "grain must be non-empty"
            )
        )
    elif not all(isinstance(entry, str) and entry for entry in grain):
        issues.append(
            SourceDeclarationIssue(
                "grain_invalid",
                grain_path,
                "grain entries must be non-empty strings",
            )
        )

    # --- connector-specific field values ---
    _validate_connector_fields(value, kind, base, issues)

    # --- schema content ---
    if has_schema and not has_schema_ref:
        inline = value["schema"]
        for issue in validate_schema_document(inline, path=schema_path):
            issues.append(SourceDeclarationIssue(issue.code, issue.path, issue.message))
    elif has_schema_ref and not has_schema:
        ref = value["schema_ref"]
        ref_path = f"{base}.schema_ref"
        if _is_str(ref) and ref:
            _resolve_schema_ref(ref, catalog_path, ref_path, issues)
        else:
            issues.append(
                SourceDeclarationIssue(
                    "schema_ref_invalid",
                    ref_path,
                    "schema_ref must be a non-empty string",
                )
            )


def _validate_connector_fields(
    value: Mapping[object, object],
    kind: str,
    base: str,
    issues: list[SourceDeclarationIssue],
) -> None:
    """Validate connector-specific field values (type, format, cross-field)."""

    # Location (file-based connectors).
    if kind in ("parquet", "csv", "delta", "sqlite", "duckdb"):
        location = value.get("location")
        if location is not None:
            loc_path = f"{base}.location"
            if not isinstance(location, str):
                issues.append(
                    SourceDeclarationIssue(
                        "location_invalid", loc_path, "location must be a string"
                    )
                )
            elif not location:
                issues.append(
                    SourceDeclarationIssue(
                        "location_invalid",
                        loc_path,
                        "location must be non-empty",
                    )
                )
            elif kind in ("parquet", "csv", "delta") and _is_remote_location(location):
                cred = value.get("credential_profile")
                if not (isinstance(cred, str) and cred):
                    issues.append(
                        SourceDeclarationIssue(
                            "location_requires_profile",
                            loc_path,
                            "remote location requires credential_profile",
                        )
                    )

    # credential_profile (parquet, csv, delta).
    if kind in ("parquet", "csv", "delta"):
        cred = value.get("credential_profile")
        if cred is not None:
            cred_path = f"{base}.credential_profile"
            if not _is_valid_identifier(cred):
                issues.append(
                    SourceDeclarationIssue(
                        "profile_invalid",
                        cred_path,
                        "credential_profile must match [a-z][a-z0-9_]*",
                    )
                )

    # CSV framing options.
    if kind == "csv":
        _validate_single_char(value, "delimiter", base, issues)
        _validate_single_char(value, "quote_char", base, issues)
        escape = value.get("escape_char")
        if escape is not None:
            esc_path = f"{base}.escape_char"
            if not isinstance(escape, str) or len(escape) != 1:
                issues.append(
                    SourceDeclarationIssue(
                        "escape_char_invalid",
                        esc_path,
                        "escape_char must be a single character or null",
                    )
                )
        header = value.get("has_header")
        if header is not None and not isinstance(header, bool):
            issues.append(
                SourceDeclarationIssue(
                    "has_header_invalid",
                    f"{base}.has_header",
                    "has_header must be a boolean",
                )
            )

    # Iceberg fields.
    if kind == "iceberg":
        catalog_profile = value.get("catalog_profile")
        if catalog_profile is not None:
            cp_path = f"{base}.catalog_profile"
            if not _is_valid_identifier(catalog_profile):
                issues.append(
                    SourceDeclarationIssue(
                        "profile_invalid",
                        cp_path,
                        "catalog_profile must match [a-z][a-z0-9_]*",
                    )
                )
        namespace = value.get("namespace")
        if namespace is not None:
            ns_path = f"{base}.namespace"
            if not isinstance(namespace, list):
                issues.append(
                    SourceDeclarationIssue(
                        "namespace_invalid",
                        ns_path,
                        "namespace must be a list of strings",
                    )
                )
            elif not namespace:
                issues.append(
                    SourceDeclarationIssue(
                        "namespace_invalid",
                        ns_path,
                        "namespace must be non-empty",
                    )
                )
            elif not all(isinstance(seg, str) and seg for seg in namespace):
                issues.append(
                    SourceDeclarationIssue(
                        "namespace_invalid",
                        ns_path,
                        "namespace entries must be non-empty strings",
                    )
                )
        table = value.get("table")
        if table is not None:
            tbl_path = f"{base}.table"
            if not _is_str(table) or not table:
                issues.append(
                    SourceDeclarationIssue(
                        "table_invalid",
                        tbl_path,
                        "table must be a non-empty string",
                    )
                )

    # Relation (sqlite, duckdb, postgres).
    if kind in ("sqlite", "duckdb", "postgres"):
        relation = value.get("relation")
        if relation is not None:
            rel_path = f"{base}.relation"
            if not _is_str(relation):
                issues.append(
                    SourceDeclarationIssue(
                        "relation_invalid",
                        rel_path,
                        "relation must be a string",
                    )
                )
            elif not relation:
                issues.append(
                    SourceDeclarationIssue(
                        "relation_invalid",
                        rel_path,
                        "relation must be non-empty",
                    )
                )
            else:
                segments = relation.split(".")
                if len(segments) > 2:
                    issues.append(
                        SourceDeclarationIssue(
                            "relation_invalid",
                            rel_path,
                            "relation must have at most two dot-separated segments",
                        )
                    )
                    segments = []  # stop further segment checks
                for segment in segments:
                    if not segment or not _SQL_IDENTIFIER.fullmatch(segment):
                        issues.append(
                            SourceDeclarationIssue(
                                "relation_invalid",
                                rel_path,
                                f"relation segment {segment!r} is not a valid identifier",
                            )
                        )
                        break

    # read_only (duckdb).
    if kind == "duckdb":
        read_only = value.get("read_only")
        if read_only is not None and not isinstance(read_only, bool):
            issues.append(
                SourceDeclarationIssue(
                    "read_only_invalid",
                    f"{base}.read_only",
                    "read_only must be a boolean",
                )
            )

    # connection_profile (postgres).
    if kind == "postgres":
        conn = value.get("connection_profile")
        if conn is not None:
            conn_path = f"{base}.connection_profile"
            if not _is_valid_identifier(conn):
                issues.append(
                    SourceDeclarationIssue(
                        "profile_invalid",
                        conn_path,
                        "connection_profile must match [a-z][a-z0-9_]*",
                    )
                )

    # handle (pyarrow).
    if kind == "pyarrow":
        handle = value.get("handle")
        if handle is not None:
            handle_path = f"{base}.handle"
            if not _is_str(handle) or not handle:
                issues.append(
                    SourceDeclarationIssue(
                        "handle_invalid",
                        handle_path,
                        "handle must be a non-empty string",
                    )
                )


def _validate_single_char(
    value: Mapping[object, object],
    field: str,
    base: str,
    issues: list[SourceDeclarationIssue],
) -> None:
    raw = value.get(field)
    if raw is None:
        return
    path = f"{base}.{field}"
    if not isinstance(raw, str) or len(raw) != 1:
        issues.append(
            SourceDeclarationIssue(
                f"{field}_invalid",
                path,
                f"{field} must be a single character",
            )
        )


# ---------------------------------------------------------------------------
# Public validation API
# ---------------------------------------------------------------------------


def validate_source_declarations(
    raw: object, catalog_path: Path
) -> tuple[SourceDeclarationIssue, ...]:
    """Validate a raw ``data_sources`` mapping, returning deterministic issues.

    The result is sorted by ``(path, message)`` so output is reproducible
    regardless of source declaration order.
    """

    issues: list[SourceDeclarationIssue] = []
    if not isinstance(raw, Mapping):
        return (
            SourceDeclarationIssue(
                "sources_not_mapping",
                "data_sources",
                "data_sources must be a mapping",
            ),
        )
    for name, value in raw.items():
        _validate_source(name, value, catalog_path, issues)
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.message)))


# ---------------------------------------------------------------------------
# Construction (only runs after validation reports zero issues)
# ---------------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _build_connector(decl: Mapping[object, object], kind: str) -> SourceConnector:
    if kind == "parquet":
        return ParquetConfig(
            str(decl["location"]),
            _opt_str(decl.get("credential_profile")),
        )
    if kind == "csv":
        return CsvConfig(
            str(decl["location"]),
            _opt_str(decl.get("credential_profile")),
            str(decl.get("delimiter", ",")),
            str(decl.get("quote_char", '"')),
            _opt_str(decl.get("escape_char")),
            bool(decl.get("has_header", True)),
        )
    if kind == "delta":
        return DeltaConfig(
            str(decl["location"]),
            _opt_str(decl.get("credential_profile")),
        )
    if kind == "iceberg":
        namespace_raw = decl["namespace"]
        assert isinstance(namespace_raw, list)
        return IcebergConfig(
            str(decl["catalog_profile"]),
            tuple(str(seg) for seg in namespace_raw),
            str(decl["table"]),
        )
    if kind == "sqlite":
        return SqliteConfig(
            str(decl["location"]),
            str(decl["relation"]),
        )
    if kind == "duckdb":
        return DuckDbConfig(
            str(decl["location"]),
            str(decl["relation"]),
            bool(decl.get("read_only", True)),
        )
    if kind == "postgres":
        return PostgresConfig(
            str(decl["connection_profile"]),
            str(decl["relation"]),
        )
    # kind == "pyarrow"
    return PyArrowConfig(str(decl["handle"]))


def _build_schema(decl: Mapping[object, object], catalog_path: Path) -> TableSchema:
    if "schema" in decl:
        return parse_schema_document(decl["schema"])
    # schema_ref (validation guaranteed exactly one is present)
    ref = str(decl["schema_ref"])
    resolved = (catalog_path.parent / ref).resolve()
    text = resolved.read_text(encoding="utf-8")
    _, data = _compose_and_construct(text)
    return parse_schema_document(data)


# ---------------------------------------------------------------------------
# Public parsing API
# ---------------------------------------------------------------------------


def parse_source_declarations(
    raw: object, catalog_path: Path
) -> Mapping[str, ParsedSource]:
    """Parse and validate a raw ``data_sources`` mapping.

    Returns an immutable mapping of :class:`ParsedSource` objects, or raises
    a single :class:`SourceDeclarationError` containing every issue sorted
    by ``(path, message)``.
    """

    issues = validate_source_declarations(raw, catalog_path)
    if issues:
        raise SourceDeclarationError(issues)

    assert isinstance(raw, Mapping)
    sources: dict[str, ParsedSource] = {}
    for name, value in raw.items():
        assert isinstance(value, Mapping)
        kind = str(value["type"])
        connector = _build_connector(value, kind)
        schema = _build_schema(value, catalog_path)
        grain_raw = value["grain"]
        assert isinstance(grain_raw, list)
        grain = tuple(str(entry) for entry in grain_raw)
        sources[str(name)] = ParsedSource(str(name), connector, schema, grain)
    return MappingProxyType(sources)
