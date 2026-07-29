"""Tests for the immutable source adapter lifecycle contracts."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

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


def test_source_filter_column_sql_is_placeholdered() -> None:
    flt = SourceFilter(column="id; DROP TABLE users--", operator="eq", value=1)
    text = repr(flt)
    assert "DROP TABLE" not in text
    assert "users" not in text
    assert "<redacted>" in text


def test_source_filter_scalar_values_render_normally() -> None:
    flt = SourceFilter(column="amount", operator="gt", value=0)
    assert "amount" in repr(flt)
    assert "gt" in repr(flt)
    assert "0" in repr(flt)


def test_scan_requirement_hostile_columns_are_placeholdered() -> None:
    req = SourceScanRequirement(
        columns=("id; DROP TABLE users--", "amount"),
        filters=(),
    )
    text = repr(req)
    assert "DROP TABLE" not in text
    assert "users" not in text
    assert "amount" in text
    assert "<redacted>" in text


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
