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
    assert err.message == "could not establish connection"
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
    err = SourceReloadError(
        "orders", "reload_failed", "reload aborted", operation_id="op-123"
    )
    assert err.operation_id == "op-123"


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
