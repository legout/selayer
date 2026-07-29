"""Tests for the immutable source adapter lifecycle contracts."""

from __future__ import annotations

import types
import uuid
from collections import UserDict
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum

import pytest

from selayer.sources.base import (
    QueryBinding,
    ReloadResult,
    SourceAdapter,
    SourceFilter,
    SourceHandle,
    SourceHealth,
    SourceScanRequirement,
    SourceStatus,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import ParquetConfig
from selayer.sources.errors import (
    SourceConnectionError,
    SourceDependencyError,
    SourceError,
    SourceProfileError,
    SourceReloadError,
    SourceSchemaError,
)
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)
from selayer.sources.schema import (
    FieldSchema,
    ScalarType,
    TableSchema,
    schema_fingerprint,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoResource:
    value: int


def _schema() -> TableSchema:
    return TableSchema((FieldSchema("id", ScalarType("int64"), False),))


def _parsed_source() -> ParsedSource:
    return ParsedSource(
        name="orders",
        connector=ParquetConfig("data/orders"),
        schema=_schema(),
        grain=("id",),
    )


# ---------------------------------------------------------------------------
# Brief: handle and status are immutable and safe
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sanitized source errors
# ---------------------------------------------------------------------------


def test_source_error_carries_uuidv4_operation_id_and_safe_fields() -> None:
    err = SourceConnectionError(
        "orders", "connect_failed", "could not establish connection"
    )
    assert err.source_id == "orders"
    assert err.code == "connect_failed"
    # The caller-supplied message is discarded; only the constant generic
    # message for the code is retained.
    assert err.message == "the source connection could not be established"
    assert "could not establish connection" not in err.message
    parsed = uuid.UUID(err.operation_id)
    assert parsed.version == 4
    assert str(parsed) == err.operation_id


def test_source_error_raised_clean_has_no_cause_or_context() -> None:
    err = SourceConnectionError("orders", "connect_failed", "connection failed")
    with pytest.raises(SourceConnectionError) as caught:
        raise err
    assert caught.value is err
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_source_error_does_not_retain_driver_exception() -> None:
    # A driver exception is observed and swallowed inside its own except
    # scope; the sanitized error is constructed and raised *outside* that
    # scope so no cause/context is retained.
    try:
        try:
            raise RuntimeError("driver secret: password=hunter2")
        except RuntimeError:
            pass
    finally:
        pass
    with pytest.raises(SourceConnectionError) as caught:
        raise SourceConnectionError("orders", "connect_failed", "connection failed")
    text = repr(caught.value)
    assert "hunter2" not in text
    assert "password" not in text
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_source_error_hierarchy_subclasses_base() -> None:
    assert issubclass(SourceDependencyError, SourceError)
    assert issubclass(SourceProfileError, SourceDependencyError)
    assert issubclass(SourceConnectionError, SourceError)
    assert issubclass(SourceSchemaError, SourceError)
    assert issubclass(SourceReloadError, SourceError)


def test_source_error_repr_is_sanitized_and_constant() -> None:
    err = SourceSchemaError(
        "orders", "schema_mismatch", "observed schema differs from declared"
    )
    text = repr(err)
    assert "orders" in text
    assert "schema_mismatch" in text
    assert "Traceback" not in text


def test_source_error_explicit_operation_id_is_honored() -> None:
    valid = "123e4567-e89b-42d3-a456-426614174000"
    err = SourceReloadError(
        "orders", "reload_failed", "reload aborted", operation_id=valid
    )
    assert err.operation_id == valid


# ---------------------------------------------------------------------------
# Hostile regression: SourceError must never expose arbitrary caller/driver
# text in message, source_id, code, or operation_id.
# ---------------------------------------------------------------------------


def test_source_error_ignores_arbitrary_driver_message() -> None:
    secret = "password=hunter2;host=internal.db.example"
    err = SourceConnectionError("orders", "connect_failed", secret)
    text = repr(err)
    assert "hunter2" not in text
    assert "password" not in text
    assert "internal.db.example" not in text
    assert secret not in str(err)
    # Stored message is the constant for the code, never the supplied detail.
    assert err.message == "the source connection could not be established"


def test_source_error_sanitizes_hostile_source_id() -> None:
    err = SourceSchemaError(
        "orders; DROP TABLE users--", "schema_mismatch", "leaked detail"
    )
    assert err.source_id == "<source>"
    text = repr(err)
    assert "DROP TABLE" not in text
    assert "users" not in text


def test_source_error_rejects_arbitrary_code() -> None:
    err = SourceReloadError("orders", "DROP TABLE users", "detail")
    assert err.code == "unknown"
    text = repr(err)
    assert "DROP TABLE" not in text
    assert "users" not in text


def test_source_error_invalid_operation_id_replaced_with_uuidv4() -> None:
    err = SourceConnectionError(
        "orders", "connect_failed", "detail", operation_id="not-a-uuid"
    )
    parsed = uuid.UUID(err.operation_id)
    assert parsed.version == 4
    assert err.operation_id != "not-a-uuid"


def test_source_error_non_v4_operation_id_replaced_with_uuidv4() -> None:
    # A valid UUID that is NOT v4 must also be replaced.
    v1 = "123e4567-e89b-12d3-a456-426614174000"
    err = SourceConnectionError("orders", "connect_failed", "detail", operation_id=v1)
    parsed = uuid.UUID(err.operation_id)
    assert parsed.version == 4
    assert err.operation_id != v1


def test_source_error_arbitrary_args_never_leak_in_repr_or_str() -> None:
    err = SourceReloadError(
        "src'; --",
        "Evil Code!",
        "driver secret token=abcdef DROP TABLE x",
        operation_id="../../etc/passwd",
    )
    for text in (repr(err), str(err)):
        assert "abcdef" not in text
        assert "DROP TABLE" not in text
        assert "passwd" not in text
        assert "Evil Code" not in text
        assert "token=" not in text


def test_source_error_unknown_code_is_coerced_to_unknown() -> None:
    # Only known codes are retained; anything else is coerced to ``"unknown"``
    # so an arbitrary caller/driver code can never surface.
    err = SourceConnectionError("orders", "some_future_code", "detail")
    assert err.code == "unknown"
    assert err.message == "a source lifecycle error occurred"
    assert "some_future_code" not in repr(err)


# ---------------------------------------------------------------------------
# SourceHandle
# ---------------------------------------------------------------------------


def test_source_handle_excludes_sensitive_fields_from_repr() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(42),
        schema=_schema(),
        snapshot="file-set:1",
        query_scoped=False,
        cleanup=lambda: None,
    )
    text = repr(handle)
    assert "DemoResource" not in text
    assert "TableSchema" not in text
    assert "lambda" not in text
    assert "<function" not in text
    assert "orders" in text
    assert "parquet" in text


def test_source_handle_is_immutable() -> None:
    handle = SourceHandle(
        source_id="o",
        connector="parquet",
        resource=DemoResource(1),
        schema=_schema(),
    )
    with pytest.raises(AttributeError):
        handle.__setattr__("source_id", "other")


# ---------------------------------------------------------------------------
# SourceStatus
# ---------------------------------------------------------------------------


def test_status_from_handle_computes_fingerprint_and_defaults_health() -> None:
    schema = _schema()
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(1),
        schema=schema,
        snapshot="file-set:1",
    )
    status = SourceStatus.from_handle(handle, generation=3)
    assert status.source_id == "orders"
    assert status.connector == "parquet"
    assert status.generation == 3
    assert status.schema_fingerprint == schema_fingerprint(schema)
    assert status.snapshot == "file-set:1"
    assert status.health is SourceHealth.READY


def test_status_repr_has_no_resource_or_schema() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(7),
        schema=_schema(),
    )
    status = SourceStatus.from_handle(handle, generation=1)
    text = repr(status)
    assert "DemoResource" not in text
    assert "TableSchema" not in text
    assert "orders" in text


def test_status_is_immutable() -> None:
    status = SourceStatus.from_handle(
        SourceHandle(
            source_id="o",
            connector="parquet",
            resource=DemoResource(1),
            schema=_schema(),
        ),
        generation=1,
    )
    with pytest.raises(AttributeError):
        status.__setattr__("generation", 9)


# ---------------------------------------------------------------------------
# ReloadResult
# ---------------------------------------------------------------------------


def test_reload_result_is_immutable_and_safe() -> None:
    result = ReloadResult("orders", 2, 3, schema_fingerprint(_schema()), "file-set:1")
    assert result.old_generation == 2
    assert result.new_generation == 3
    assert result.source_id == "orders"
    text = repr(result)
    assert "DemoResource" not in text
    assert "TableSchema" not in text
    assert "orders" in text


# ---------------------------------------------------------------------------
# SourceScanRequirement
# ---------------------------------------------------------------------------


def test_scan_requirement_carries_structured_filters_no_raw_sql() -> None:
    req = SourceScanRequirement(
        columns=("id", "amount"),
        filters=(
            SourceFilter(column="id", operator="eq", value=5),
            SourceFilter(column="amount", operator="gt", value=0),
        ),
    )
    assert req.columns == ("id", "amount")
    assert len(req.filters) == 2
    text = repr(req)
    assert "SELECT" not in text.upper()
    assert "WHERE" not in text.upper()
    assert "FROM" not in text.upper()


def test_scan_requirement_coerces_iterables_to_tuples() -> None:
    req = SourceScanRequirement(columns=["a", "b"], filters=[])  # type: ignore[arg-type]
    assert req.columns == ("a", "b")
    assert req.filters == ()


def test_scan_requirement_accepts_column_generator() -> None:
    # A single-pass generator of columns must be materialized exactly once so
    # validation and coercion share the same items (previously the validation
    # loop exhausted the generator and the later ``tuple(...)`` stored ()).
    columns = (column for column in ("id", "amount"))
    req = SourceScanRequirement(columns=columns, filters=())  # type: ignore[arg-type]
    assert req.columns == ("id", "amount")


def test_scan_requirement_accepts_filter_generator() -> None:
    # A single-pass generator of filters must be materialized exactly once so
    # validation and coercion share the same items (previously the validation
    # loop exhausted the generator and the later ``tuple(...)`` stored ()).
    filters = (SourceFilter(column="id", operator="eq", value=1) for _ in [None])
    req = SourceScanRequirement(columns=("id",), filters=filters)  # type: ignore[arg-type]
    assert len(req.filters) == 1
    assert req.filters[0].column == "id"


def test_scan_requirement_generator_validation_runs_before_storage() -> None:
    # A generator yielding a hostile column must be rejected (and the materialized
    # tuple discarded) rather than silently stored as an empty tuple.
    columns = (column for column in ("id; DROP TABLE users--", "amount"))
    with pytest.raises(ValueError):
        SourceScanRequirement(columns=columns, filters=())  # type: ignore[arg-type]


def test_scan_requirement_is_immutable() -> None:
    req = SourceScanRequirement(columns=("id",), filters=())
    with pytest.raises(AttributeError):
        req.__setattr__("columns", "other")


# ---------------------------------------------------------------------------
# QueryBinding
# ---------------------------------------------------------------------------


def test_query_binding_runs_cleanup_on_context_exit() -> None:
    calls: list[str] = []

    def cleanup() -> None:
        calls.append("cleaned")

    binding = QueryBinding(source_id="orders", stable_name="orders_v1", cleanup=cleanup)
    with binding as ctx:
        assert ctx is binding
    assert calls == ["cleaned"]


def test_query_binding_cleanup_is_idempotent() -> None:
    count = [0]

    def cleanup() -> None:
        count[0] += 1

    binding = QueryBinding(source_id="orders", stable_name="orders_v1", cleanup=cleanup)
    with binding:
        pass
    # A second, manual exit must not re-invoke the underlying cleanup.
    binding.__exit__(None, None, None)
    assert count[0] == 1


def test_query_binding_repr_excludes_cleanup_and_does_not_invoke_it() -> None:
    invoked: list[str] = []

    def cleanup() -> None:
        invoked.append("called")

    binding = QueryBinding(source_id="orders", stable_name="orders_v1", cleanup=cleanup)
    text = repr(binding)
    assert "orders" in text
    assert "cleanup" not in text
    assert "<function" not in text
    assert invoked == []


# ---------------------------------------------------------------------------
# Hostile regression: value-object reprs must not leak arbitrary SQL,
# credential-bearing URI strings, handles, resources, schemas, or cleanup
# callbacks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HostileResource:
    """A resource whose own repr leaks a secret — must never surface."""

    secret: str

    def __repr__(self) -> str:
        return f"_HostileResource(secret={self.secret!r})"


def test_source_filter_value_credential_uri_is_redacted() -> None:
    flt = SourceFilter(
        column="id",
        operator="eq",
        value="postgres://user:secret@internal.host:5432/db",
    )
    text = repr(flt)
    # The credential userinfo (user:secret) is redacted; the host/path may
    # remain (matching the existing config redactor contract).
    assert "secret" not in text
    assert "user:" not in text
    assert "user:secret" not in text


def test_source_filter_value_sql_fragment_is_redacted() -> None:
    flt = SourceFilter(column="id", operator="eq", value="'; DROP TABLE users; --")
    text = repr(flt)
    assert "DROP TABLE" not in text
    assert "users" not in text


def test_source_filter_value_keyeq_secret_is_redacted() -> None:
    flt = SourceFilter(column="id", operator="eq", value="password=hunter2")
    text = repr(flt)
    assert "hunter2" not in text
    assert "password" not in text


def test_source_filter_value_tuple_elements_are_redacted() -> None:
    flt = SourceFilter(
        column="id",
        operator="in",
        value=("postgres://u:p@h/db", "'; DROP TABLE x"),
    )
    text = repr(flt)
    assert "p@h" not in text
    assert "DROP TABLE" not in text


def test_source_filter_rejects_hostile_column() -> None:
    # A SQL fragment is not a string identifier; it is rejected at construction
    # so no arbitrary SQL can ever be stored as a column (and later interpolated).
    with pytest.raises(ValueError):
        SourceFilter(column="id; DROP TABLE users--", operator="eq", value=1)


def test_source_filter_rejects_non_string_column() -> None:
    with pytest.raises(ValueError):
        SourceFilter(column=123, operator="eq", value=1)  # type: ignore[arg-type]


def test_source_filter_token_shaped_column_is_redacted() -> None:
    # TOKENONLYSECRET is a syntactically valid identifier, so it is accepted at
    # construction; the repr conservatively redacts it so a token-shaped secret
    # column can never surface in diagnostics.
    flt = SourceFilter(column="TOKENONLYSECRET", operator="eq", value=1)
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "<redacted>" in text


def test_source_filter_scalar_values_render_normally() -> None:
    flt = SourceFilter(column="amount", operator="gt", value=0)
    assert "amount" in repr(flt)
    assert "gt" in repr(flt)
    assert "0" in repr(flt)


def test_scan_requirement_rejects_hostile_column() -> None:
    # Every column must be a string identifier; a SQL fragment is rejected at
    # construction rather than merely redacted in the repr.
    with pytest.raises(ValueError):
        SourceScanRequirement(
            columns=("id; DROP TABLE users--", "amount"),
            filters=(),
        )


def test_scan_requirement_token_shaped_column_is_redacted() -> None:
    req = SourceScanRequirement(columns=("TOKENONLYSECRET", "amount"), filters=())
    text = repr(req)
    assert "TOKENONLYSECRET" not in text
    assert "amount" in text
    assert "<redacted>" in text


def test_scan_requirement_rejects_raw_string_filter() -> None:
    # A raw SQL string is not a SourceFilter; it is rejected with a clean
    # TypeError so arbitrary SQL can never be stored as a planned filter.
    with pytest.raises(TypeError):
        SourceScanRequirement(
            columns=("id",),
            filters=("SELECT password FROM users",),  # type: ignore[list-item]
        )


def test_source_handle_repr_redacts_credential_snapshot() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
        snapshot="s3://AKIAKEY:s3cr3t@bucket.internal/path",
        cleanup=lambda: None,
    )
    text = repr(handle)
    # Resource object, schema, and cleanup never surface.
    assert "topsecret" not in text
    assert "_HostileResource" not in text
    assert "TableSchema" not in text
    assert "<function" not in text
    # Credential userinfo in the snapshot is redacted.
    assert "s3cr3t" not in text
    assert "AKIAKEY" not in text
    assert "AKIAKEY:s3cr3t" not in text
    assert "orders" in text


def test_source_status_repr_redacts_credential_snapshot() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
        snapshot="https://admin:p4ss@internal.host/snap",
    )
    status = SourceStatus.from_handle(handle, generation=1)
    text = repr(status)
    assert "topsecret" not in text
    assert "_HostileResource" not in text
    assert "TableSchema" not in text
    # Credential userinfo in the snapshot is redacted.
    assert "p4ss" not in text
    assert "admin:p4ss" not in text


def test_reload_result_repr_redacts_hostile_snapshot() -> None:
    result = ReloadResult(
        "orders",
        2,
        3,
        schema_fingerprint(_schema()),
        "'; DROP TABLE users; --",
    )
    text = repr(result)
    assert "DROP TABLE" not in text
    assert "users" not in text
    assert "orders" in text


def test_query_binding_repr_redacts_hostile_stable_name() -> None:
    binding = QueryBinding(
        source_id="orders",
        stable_name="orders; DROP TABLE users--",
        cleanup=lambda: None,
    )
    text = repr(binding)
    assert "DROP TABLE" not in text
    assert "users" not in text
    assert "<function" not in text
    assert "orders" in text


def test_query_binding_repr_does_not_invoke_cleanup() -> None:
    invoked: list[str] = []

    def cleanup() -> None:
        invoked.append("called")

    binding = QueryBinding(source_id="orders", stable_name="orders_v1", cleanup=cleanup)
    repr(binding)
    assert invoked == []


# ---------------------------------------------------------------------------
# Hostile regression: token-shaped secrets.  ``TOKENONLYSECRET`` is a string
# that matches a permissive "safe token" regex yet is a secret.  It must never
# surface in any repr surface; filter values, snapshots, and stable names are
# redacted by default, and arbitrary SQL operators are rejected at
# construction.  SourceError must render token-shaped source ids / codes as
# safe placeholders rather than trusting a permissive regex.
# ---------------------------------------------------------------------------


def test_source_filter_value_token_secret_is_redacted() -> None:
    # A bare token-shaped secret has no URI userinfo, so a userinfo redactor
    # and a token-shaped regex both let it through verbatim — the value must
    # be redacted by default.
    flt = SourceFilter(column="id", operator="eq", value="TOKENONLYSECRET")
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "<redacted>" in text


def test_source_filter_value_token_in_collection_is_redacted() -> None:
    flt = SourceFilter(
        column="id",
        operator="in",
        value=("TOKENONLYSECRET", "PLAINVALUE"),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "PLAINVALUE" not in text


def test_source_filter_dict_value_is_redacted() -> None:
    # A mapping value may carry secrets in keys or values; the whole mapping is
    # redacted to a fixed placeholder so neither can surface in the repr.
    flt = SourceFilter(
        column="id",
        operator="eq",
        value={"token": "TOKENONLYSECRET", "password": "hunter2"},
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "hunter2" not in text
    assert "password" not in text
    assert "token" not in text
    assert "{" not in text
    assert "<redacted>" in text


def test_source_filter_set_value_is_redacted() -> None:
    # A set value may carry secret members; the whole set is redacted so no
    # member can surface in the repr.
    flt = SourceFilter(column="id", operator="in", value={"TOKENONLYSECRET", "x"})
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "{" not in text
    assert "<redacted>" in text


def test_source_filter_numeric_tuple_value_renders_safely() -> None:
    # Numeric literals are safe: a tuple of numeric literals renders without
    # leaking, and a tuple mixing a string secret with a numeric still redacts
    # the string element.
    numeric = SourceFilter(column="id", operator="in", value=(1, 2, 3))
    secret_numeric = SourceFilter(
        column="id", operator="in", value=(1, "TOKENONLYSECRET")
    )
    numeric_text = repr(numeric)
    secret_text = repr(secret_numeric)
    assert "TOKENONLYSECRET" not in secret_text
    assert "<redacted>" in secret_text
    assert "1" in numeric_text


# ---------------------------------------------------------------------------
# Follow-up 4: arbitrary Mapping/Set implementations and unknown object types
# must be redacted wholesale — only safe scalars (int/float/bool/None/enum),
# tuples, and lists are projected.  A custom mapping/set whose own ``__repr__``
# leaks a secret, or an opaque handle object, must never surface via repr.
# ---------------------------------------------------------------------------


class _LeakyMapping(Mapping):
    """A ``collections.abc.Mapping`` whose repr deliberately leaks a secret."""

    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def __getitem__(self, key: str) -> str:
        return self._payload[key]

    def __iter__(self):
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)

    def __repr__(self) -> str:
        return f"_LeakyMapping({self._payload!r})"


class _LeakySet(AbstractSet):
    """A ``collections.abc.Set`` whose repr deliberately leaks a secret."""

    def __init__(self, members: set[str]) -> None:
        self._members = set(members)

    def __contains__(self, item: object) -> bool:
        return item in self._members

    def __iter__(self):
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def __repr__(self) -> str:
        return f"_LeakySet({self._members!r})"


class _LeakyObject:
    """An opaque handle whose repr leaks a secret; must never surface."""

    secret = "TOKENONLYSECRET"

    def __repr__(self) -> str:
        return f"_LeakyObject(secret={self.secret!r})"


def test_source_filter_mappingproxy_value_is_redacted() -> None:
    # ``types.MappingProxyType`` is a Mapping but NOT a ``dict``; the previous
    # ``isinstance(value, (dict, set, frozenset))`` guard let it fall through
    # to ``return value``, leaking the proxied payload via its own repr.
    flt = SourceFilter(
        column="id",
        operator="eq",
        value=types.MappingProxyType({"token": "TOKENONLYSECRET"}),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "mappingproxy" not in text
    assert "<redacted>" in text


def test_source_filter_userdict_value_is_redacted() -> None:
    # ``collections.UserDict`` is a Mapping (subclass of MutableMapping) but
    # not a ``dict``; its repr renders the wrapped dict verbatim.
    flt = SourceFilter(
        column="id",
        operator="eq",
        value=UserDict({"token": "TOKENONLYSECRET", "password": "hunter2"}),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "hunter2" not in text
    assert "password" not in text
    assert "<redacted>" in text


def test_source_filter_custom_mapping_value_is_redacted() -> None:
    # A hand-rolled ``collections.abc.Mapping`` is not a ``dict``; its own
    # (leaky) repr would surface if only concrete dict/set/frozenset were
    # redacted.
    flt = SourceFilter(
        column="id",
        operator="eq",
        value=_LeakyMapping({"token": "TOKENONLYSECRET"}),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyMapping" not in text
    assert "<redacted>" in text


def test_source_filter_custom_set_value_is_redacted() -> None:
    # A hand-rolled ``collections.abc.Set`` is not a ``set``/``frozenset``;
    # its own (leaky) repr would surface otherwise.
    flt = SourceFilter(
        column="id",
        operator="in",
        value=_LeakySet({"TOKENONLYSECRET", "x"}),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakySet" not in text
    assert "<redacted>" in text


def test_source_filter_unknown_object_value_is_redacted() -> None:
    # Any object type that is not a safe scalar/tuple/list is now redacted by
    # default, so an opaque handle's own repr can never leak a secret.
    flt = SourceFilter(column="id", operator="eq", value=_LeakyObject())
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyObject" not in text
    assert "<redacted>" in text


def test_source_filter_scalar_and_tuple_still_render() -> None:
    # Regression guard: safe scalars and ordered collections still project.
    int_value = SourceFilter(column="id", operator="eq", value=42)
    bool_value = SourceFilter(column="active", operator="eq", value=True)
    none_value = SourceFilter(column="id", operator="is_null", value=None)
    tuple_value = SourceFilter(column="id", operator="in", value=(1, 2))
    assert "42" in repr(int_value)
    assert "True" in repr(bool_value)
    assert "None" in repr(none_value)
    assert "1" in repr(tuple_value)
    assert "2" in repr(tuple_value)


def test_source_filter_rejects_arbitrary_sql_operator() -> None:
    # The ``Literal`` operator type is enforced at construction: arbitrary SQL
    # cannot be stored as an operator (and therefore never rendered).  The
    # ``type: ignore`` is static only — ``__post_init__`` rejects it at runtime.
    with pytest.raises(ValueError):
        SourceFilter(
            column="id",
            operator="SELECT * FROM secrets",  # type: ignore[arg-type]
            value=1,
        )


def test_source_handle_snapshot_token_secret_is_redacted() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
        snapshot="TOKENONLYSECRET",
        cleanup=lambda: None,
    )
    text = repr(handle)
    assert "TOKENONLYSECRET" not in text
    assert "topsecret" not in text


def test_source_status_snapshot_token_secret_is_redacted() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
        snapshot="TOKENONLYSECRET",
    )
    status = SourceStatus.from_handle(handle, generation=1)
    text = repr(status)
    assert "TOKENONLYSECRET" not in text
    assert "topsecret" not in text


def test_reload_result_snapshot_token_secret_is_redacted() -> None:
    result = ReloadResult(
        "orders",
        2,
        3,
        schema_fingerprint(_schema()),
        "TOKENONLYSECRET",
    )
    text = repr(result)
    assert "TOKENONLYSECRET" not in text


def test_query_binding_stable_name_token_secret_is_redacted() -> None:
    binding = QueryBinding(
        source_id="orders",
        stable_name="TOKENONLYSECRET",
        cleanup=lambda: None,
    )
    text = repr(binding)
    assert "TOKENONLYSECRET" not in text
    assert "orders" in text


def test_source_error_token_source_id_rendered_as_source() -> None:
    # A token-shaped source id matches a permissive identifier regex yet is a
    # secret; it must render as ``<source>`` and never be retained in repr.
    err = SourceConnectionError("TOKENONLYSECRET", "connect_failed", "detail")
    assert err.source_id == "<source>"
    text = repr(err)
    assert "TOKENONLYSECRET" not in text


def test_source_error_token_code_coerced_to_unknown() -> None:
    err = SourceConnectionError("orders", "TOKENONLYCODE", "detail")
    assert err.code == "unknown"
    assert err.message == "a source lifecycle error occurred"
    text = repr(err)
    assert "TOKENONLYCODE" not in text


# ---------------------------------------------------------------------------
# Follow-up 5: scalar repr bypass.  ``_repr_literal`` previously passed Enum
# members through verbatim and used ``isinstance(value, (int, float))``, which
# also catches subclasses.  An Enum member whose own ``__repr__`` leaks a
# secret, or an ``int``/``float`` subclass with a custom ``__repr__``, thus
# surfaced in diagnostics.  Only *exact* builtin scalars (``int``, ``float``,
# ``bool``) and ``None`` may now pass through; Enum and all subclasses are
# redacted to ``<redacted>``.
# ---------------------------------------------------------------------------


class _LeakyEnum(Enum):
    """An Enum whose repr leaks a secret — must never surface."""

    TOKENONLYSECRET = "supersecret"

    def __repr__(self) -> str:
        return f"_LeakyEnum(TOKENONLYSECRET={self.value!r})"


class _LeakyInt(int):
    """An ``int`` subclass whose repr leaks a secret."""

    def __repr__(self) -> str:
        return "_LeakyInt(TOKENONLYSECRET)"


class _LeakyFloat(float):
    """A ``float`` subclass whose repr leaks a secret."""

    def __repr__(self) -> str:
        return "_LeakyFloat(TOKENONLYSECRET)"


def test_source_filter_enum_member_value_is_redacted() -> None:
    # Previously ``_repr_literal`` returned Enum members unchanged, letting a
    # member whose own ``__repr__`` leaks a secret (``TOKENONLYSECRET``) surface
    # verbatim in diagnostics.
    flt = SourceFilter(column="id", operator="eq", value=_LeakyEnum.TOKENONLYSECRET)
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "supersecret" not in text
    assert "_LeakyEnum" not in text
    assert "<redacted>" in text


def test_source_filter_enum_member_in_tuple_is_redacted() -> None:
    # The element-wise projection of a tuple must also redact an Enum member.
    flt = SourceFilter(
        column="id",
        operator="in",
        value=(1, _LeakyEnum.TOKENONLYSECRET),
    )
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "supersecret" not in text
    assert "<redacted>" in text
    # The safe numeric element still renders.
    assert "1" in text


def test_source_filter_int_subclass_value_is_redacted() -> None:
    # ``isinstance(_LeakyInt(1), int)`` is True, so the previous isinstance
    # guard let the subclass through to its own (leaky) ``__repr__``.
    flt = SourceFilter(column="id", operator="eq", value=_LeakyInt(1))
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyInt" not in text
    assert "<redacted>" in text


def test_source_filter_float_subclass_value_is_redacted() -> None:
    # Likewise ``isinstance(_LeakyFloat(1.0), float)`` is True.
    flt = SourceFilter(column="id", operator="eq", value=_LeakyFloat(1.0))
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyFloat" not in text
    assert "<redacted>" in text


def test_source_filter_exact_scalars_still_render() -> None:
    # Regression guard: tightening to exact-type checks must not regress normal
    # numeric filter reprs.  ``bool`` is included because ``type(True) is bool``
    # (a distinct type from ``int``), so it must be covered explicitly.
    int_value = SourceFilter(column="id", operator="eq", value=42)
    float_value = SourceFilter(column="amount", operator="gt", value=3.14)
    bool_value = SourceFilter(column="active", operator="eq", value=True)
    none_value = SourceFilter(column="id", operator="is_null", value=None)
    assert "42" in repr(int_value)
    assert "3.14" in repr(float_value)
    assert "True" in repr(bool_value)
    assert "None" in repr(none_value)


# ---------------------------------------------------------------------------
# Follow-up 6: hostile ``str`` subclasses.  A ``str`` subclass passes
# ``isinstance(value, str)`` yet can carry a custom ``__repr__`` that leaks a
# secret.  Identifier render helpers (``_repr_source_name``, ``_repr_column``)
# and SourceError storage helpers (``_safe_source_id``, ``_safe_code``) now
# accept only *exact* builtin ``str`` (``type(value) is str``), and
# ``SourceFilter``/``SourceScanRequirement`` columns and operators are rejected
# at construction unless they are exact builtin ``str``.  A hostile subclass
# is therefore never rendered via its own ``__repr__``.
# ---------------------------------------------------------------------------


class _LeakyString(str):
    """A ``str`` subclass whose repr leaks a secret — must never surface."""

    def __repr__(self) -> str:
        return "_LeakyString(TOKENONLYSECRET)"


def test_source_filter_rejects_str_subclass_column() -> None:
    # ``type(column) is str`` rejects a hostile subclass at construction so it
    # can never be stored (and later interpolated by an adapter) or rendered.
    with pytest.raises(ValueError):
        SourceFilter(column=_LeakyString("id"), operator="eq", value=1)


def test_source_filter_rejects_str_subclass_operator() -> None:
    with pytest.raises(ValueError):
        SourceFilter(column="id", operator=_LeakyString("eq"), value=1)  # type: ignore[arg-type]


def test_scan_requirement_rejects_str_subclass_column() -> None:
    with pytest.raises(ValueError):
        SourceScanRequirement(columns=(_LeakyString("id"),), filters=())  # type: ignore[arg-type]


def test_source_filter_value_str_subclass_is_redacted() -> None:
    # The free-form value field redacts every string (including subclasses) to
    # a fixed placeholder, so a hostile subclass's own ``__repr__`` never runs.
    flt = SourceFilter(column="id", operator="eq", value=_LeakyString("secret"))
    text = repr(flt)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text
    assert "<redacted>" in text


def test_source_handle_source_id_str_subclass_redacted() -> None:
    handle = SourceHandle(
        source_id=_LeakyString("orders"),
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
    )
    text = repr(handle)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_handle_connector_str_subclass_redacted() -> None:
    handle = SourceHandle(
        source_id="orders",
        connector=_LeakyString("parquet"),
        resource=_HostileResource("topsecret"),
        schema=_schema(),
    )
    text = repr(handle)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_status_source_id_str_subclass_redacted() -> None:
    status = SourceStatus(
        source_id=_LeakyString("orders"),
        connector="parquet",
        generation=1,
        schema_fingerprint=schema_fingerprint(_schema()),
        snapshot=None,
        health=SourceHealth.READY,
    )
    text = repr(status)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_reload_result_source_id_str_subclass_redacted() -> None:
    result = ReloadResult(
        _LeakyString("orders"),
        2,
        3,
        schema_fingerprint(_schema()),
        None,
    )
    text = repr(result)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_query_binding_source_id_str_subclass_redacted() -> None:
    binding = QueryBinding(
        source_id=_LeakyString("orders"),
        stable_name="orders_v1",
        cleanup=lambda: None,
    )
    text = repr(binding)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_error_str_subclass_source_id_coerced() -> None:
    err = SourceConnectionError(_LeakyString("orders"), "connect_failed", "detail")
    assert type(err.source_id) is str
    assert err.source_id == "<source>"
    text = repr(err)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_error_str_subclass_code_coerced() -> None:
    err = SourceConnectionError("orders", _LeakyString("connect_failed"), "detail")
    assert type(err.code) is str
    assert err.code == "unknown"
    text = repr(err)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_error_str_subclass_operation_id_normalized() -> None:
    # A ``str`` subclass operation_id is parsed as a UUID; only the canonical
    # (plain builtin ``str``) form is stored, so the hostile subclass repr
    # never runs.
    valid = _LeakyString("123e4567-e89b-42d3-a456-426614174000")
    err = SourceConnectionError(
        "orders", "connect_failed", "detail", operation_id=valid
    )
    assert type(err.operation_id) is str
    text = repr(err)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_exact_builtin_string_identifiers_still_render() -> None:
    # Regression guard: tightening to ``type(value) is str`` must not regress
    # normal exact-builtin-string identifier fields.
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=_HostileResource("topsecret"),
        schema=_schema(),
    )
    text = repr(handle)
    assert "orders" in text
    assert "parquet" in text


def test_exact_builtin_string_error_fields_preserved() -> None:
    # Regression guard: normal exact-builtin-string source_id / code are
    # retained unchanged.
    err = SourceConnectionError("orders", "connect_failed", "detail")
    assert err.source_id == "orders"
    assert err.code == "connect_failed"
    assert "orders" in repr(err)
    assert "connect_failed" in repr(err)


# ---------------------------------------------------------------------------
# Follow-up 7: lifecycle repr scalars.  Direct scalar fields
# (``SourceHandle.query_scoped``, ``SourceStatus.generation``,
# ``ReloadResult.old_generation``/``new_generation``) were rendered straight
# through ``_render``'s ``!r``, so a hostile ``int``/``bool`` subclass whose own
# ``__repr__`` leaks a secret surfaced in diagnostics.  These fields now route
# through ``_repr_literal`` (exact-builtin scalar projection).  Closed-set /
# inherently-safe string scalars (``SourceFilter.operator``, validated at
# construction; ``SourceStatus.health`` via ``.value``) route through
# ``_repr_scalar`` — an exact-builtin-str scalar projection — so a hostile
# ``str`` subclass never has its own ``__repr__`` invoked.  Finally,
# ``SourceScanRequirement`` now accepts only *exact* ``SourceFilter`` instances
# so a ``SourceFilter`` subclass with a custom ``__repr__`` can never be stored
# and later rendered when the requirement reprs its filter tuple.
# ---------------------------------------------------------------------------


class _LeakyHealthValue:
    """Object masquerading as a health enum whose ``.value`` leaks a secret."""

    @property
    def value(self) -> object:
        return _LeakyString("ready")


class _LeakySourceFilter(SourceFilter):
    """A ``SourceFilter`` subclass whose repr leaks a secret — must be rejected."""

    def __repr__(self) -> str:
        return "_LeakySourceFilter(TOKENONLYSECRET)"


def test_source_handle_query_scoped_scalar_subclass_redacted() -> None:
    # ``query_scoped`` is rendered directly through ``!r``; a hostile scalar
    # whose own ``__repr__" leaks a secret must be redacted.  ``bool`` is
    # ``@final`` (it cannot be subclassed in well-typed code), so a hostile
    # ``int`` subclass stands in for the non-exact-scalar value the field
    # would accept at runtime absent type enforcement.
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(1),
        schema=_schema(),
        query_scoped=_LeakyInt(0),  # type: ignore[arg-type]
    )
    text = repr(handle)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyInt" not in text
    assert "<redacted>" in text


def test_source_status_generation_int_subclass_redacted() -> None:
    # ``generation`` is an int rendered directly through ``!r``; a hostile
    # ``int`` subclass whose own ``__repr__" leaks a secret must be redacted.
    status = SourceStatus(
        source_id="orders",
        connector="parquet",
        generation=_LeakyInt(3),
        schema_fingerprint=schema_fingerprint(_schema()),
        snapshot=None,
        health=SourceHealth.READY,
    )
    text = repr(status)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyInt" not in text
    assert "<redacted>" in text


def test_reload_result_generation_int_subclass_redacted() -> None:
    # Both generation fields are ints rendered directly through ``!r``; hostile
    # ``int`` subclasses must be redacted.
    result = ReloadResult(
        "orders",
        _LeakyInt(2),
        _LeakyInt(3),
        schema_fingerprint(_schema()),
        None,
    )
    text = repr(result)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyInt" not in text
    assert "<redacted>" in text


def test_source_status_health_value_str_subclass_redacted() -> None:
    # ``health`` renders through ``.value``; a hostile object whose ``.value``
    # returns a ``str`` subclass must not surface that subclass's repr.
    status = SourceStatus(
        source_id="orders",
        connector="parquet",
        generation=1,
        schema_fingerprint=schema_fingerprint(_schema()),
        snapshot=None,
        health=_LeakyHealthValue(),  # type: ignore[arg-type]
    )
    text = repr(status)
    assert "TOKENONLYSECRET" not in text
    assert "_LeakyString" not in text


def test_source_filter_operator_renders_via_safe_helper() -> None:
    # The closed-set operator token is rendered through the exact-value scalar
    # helper, not a direct f-string; a normal validated operator still renders.
    flt = SourceFilter(column="amount", operator="gt", value=0)
    assert "operator='gt'" in repr(flt)


def test_scan_requirement_rejects_source_filter_subclass() -> None:
    # A ``SourceFilter`` subclass with a custom ``__repr__`` must be rejected at
    # construction so its hostile repr can never be invoked when the
    # requirement renders its filter tuple.
    hostile = _LeakySourceFilter(column="id", operator="eq", value=1)
    with pytest.raises(TypeError):
        SourceScanRequirement(columns=("id",), filters=(hostile,))


def test_exact_scalar_lifecycle_fields_still_render() -> None:
    # Regression guard: normal exact-builtin int/bool scalar fields must still
    # render unchanged after routing through the scalar projection helpers.
    handle = SourceHandle(
        source_id="orders",
        connector="parquet",
        resource=DemoResource(1),
        schema=_schema(),
        query_scoped=False,
    )
    status = SourceStatus(
        source_id="orders",
        connector="parquet",
        generation=7,
        schema_fingerprint=schema_fingerprint(_schema()),
        snapshot=None,
        health=SourceHealth.READY,
    )
    result = ReloadResult("orders", 6, 7, schema_fingerprint(_schema()), None)
    assert "query_scoped=False" in repr(handle)
    assert "generation=7" in repr(status)
    assert "old_generation=6" in repr(result)
    assert "new_generation=7" in repr(result)


def test_source_status_normal_health_renders() -> None:
    # Regression guard: a genuine SourceHealth value still renders its string
    # value (not the enum object) after routing ``.value`` through the scalar
    # projection helper.
    status = SourceStatus(
        source_id="orders",
        connector="parquet",
        generation=1,
        schema_fingerprint=schema_fingerprint(_schema()),
        snapshot=None,
        health=SourceHealth.READY,
    )
    assert "health='ready'" in repr(status)


# ---------------------------------------------------------------------------
# SourceAdapter protocol acceptance
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    """A fake adapter satisfying every ``SourceAdapter`` method."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.closed: list[str] = []

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        return SourceHandle(
            source_id=source.name,
            connector="parquet",
            resource=DemoResource(1),
            schema=source.schema,
            snapshot="file-set:1",
            query_scoped=False,
        )

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        return handle.schema

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        self.registered.append(stable_name)

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        return QueryBinding(
            source_id=handle.source_id,
            stable_name="orders",
            cleanup=lambda: None,
        )

    def close(self, handle: SourceHandle) -> None:
        self.closed.append(handle.source_id)


def test_fake_adapter_satisfies_protocol_without_cast() -> None:
    adapter: SourceAdapter = _RecordingAdapter()
    assert isinstance(adapter, SourceAdapter)


def test_fake_adapter_methods_are_usable() -> None:
    adapter = _RecordingAdapter()
    handle = adapter.prepare(
        _parsed_source(),
        profiles=_ProfileResolverStub(),
        arrow_providers=_ArrowResolverStub(),
    )
    assert isinstance(handle, SourceHandle)
    schema = adapter.inspect_schema(handle)
    assert schema == _schema()
    adapter.register(object(), "orders_v1", handle)
    assert adapter.registered == ["orders_v1"]
    binding = adapter.bind_query(
        handle, SourceScanRequirement(columns=("id",), filters=())
    )
    assert binding is not None
    adapter.close(handle)
    assert adapter.closed == ["orders"]


class _ProfileResolverStub:
    def resolve(self, name: str, *, source_id: str) -> RuntimeProfile:
        raise NotImplementedError


class _ArrowResolverStub:
    def resolve(self, handle: str, *, source_id: str) -> Callable[[], ArrowObject]:
        raise NotImplementedError
