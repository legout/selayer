"""PyArrow Dataset adapter for parquet, csv, and programmatic sources.

This adapter satisfies the private
:class:`~selayer.sources.base.SourceAdapter` protocol for the ``parquet``,
``csv``, and ``pyarrow`` connector kinds.  It builds a
:class:`pyarrow.dataset.Dataset` (for file-based and programmatic dataset
sources) or binds a :class:`pyarrow.RecordBatchReader` (for query-scoped
programmatic providers) against the *declared* :class:`~selayer.sources.schema.TableSchema`.

Design pillars:

* **No eager Polars materialization.**  Sources are registered with DuckDB as
  PyArrow Datasets (or readers), so projection and filter pushdown happen
  inside DuckDB's Arrow scanner.  ``polars.read_parquet`` / ``polars.read_csv``
  are never called.
* **Declared schema is authoritative.**  The dataset is built with the declared
  Arrow schema so an observed schema that drifts from the declaration is caught
  by :func:`~selayer.sources.schema.compare_schemas` before registration.
* **Providers are the reloadable unit.**  Programmatic ``pyarrow`` sources
  resolve a zero-argument provider factory and invoke it on every prepare and
  (for query-scoped readers) on every query binding, so the underlying relation
  can be re-opened.
* **Query-scoped readers.**  A provider that yields a
  :class:`pyarrow.RecordBatchReader` produces a query-scoped handle: the reader
  is single-pass, so it is recreated and re-registered once per query.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.dataset as padataset
import pyarrow.fs as pafs

try:
    import boto3
except ImportError:  # pragma: no cover - exercised only without the s3 extra
    boto3 = None  # type: ignore[assignment]

from selayer.sources.base import (
    QueryBinding,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import (
    CsvConfig,
    ParquetConfig,
    PyArrowConfig,
    SourceConnector,
)
from selayer.sources.profiles import (
    ArrowObject,
    ArrowProviderResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, table_schema_to_arrow

__all__ = ["ArrowDatasetAdapter"]


def _reader_cleanup(connection: object, stable_name: str) -> Callable[[], None]:
    """Return an idempotent cleanup that deregisters a query-scoped view."""

    def _cleanup() -> None:
        with suppress(Exception):
            # DuckDB's unregister removes the replacement-scan binding; any
            # failure is suppressed because cleanup must never mask the query
            # result or raise out of a context-manager exit.
            connection.unregister(stable_name)  # type: ignore[attr-defined]

    return _cleanup


class ArrowDatasetAdapter:
    """Adapter for parquet/csv files and programmatic PyArrow sources.

    A single instance serves the ``parquet``, ``csv``, and ``pyarrow``
    connector kinds.  File-based connectors build a
    :class:`pyarrow.dataset.Dataset` from ``location`` using the declared
    schema; the ``pyarrow`` connector resolves a provider factory from
    :class:`~selayer.sources.profiles.ArrowProviderResolver` and invokes it.
    """

    __slots__ = ()

    # -- prepare -----------------------------------------------------------

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        connector = source.connector
        arrow_schema = table_schema_to_arrow(source.schema)
        kind = _connector_kind(connector)
        if kind == "pyarrow":
            assert isinstance(connector, PyArrowConfig)
            provider = arrow_providers.resolve(connector.handle, source_id=source.name)
            resource = provider()
            return _handle_for_arrow_object(source, resource, provider)
        if kind == "parquet":
            assert isinstance(connector, ParquetConfig)
            # Build *without* a schema override so the physical Parquet schema
            # is observed by ``inspect_schema`` and any drift (type, extra
            # field, nullability) is caught by ``compare_schemas`` before the
            # source is registered.  DuckDB still gets ARROW_SCAN pushdown over
            # the physical dataset (local or S3-backed alike).
            filesystem, path = _resolve_s3_location(connector, profiles, source.name)
            resource = _parquet_dataset(path, filesystem)
        elif kind == "csv":
            assert isinstance(connector, CsvConfig)
            filesystem, path = _resolve_s3_location(connector, profiles, source.name)
            resource = _csv_dataset(connector, arrow_schema, filesystem, path)
        else:  # pragma: no cover - registry only dispatches these kinds
            raise TypeError(f"ArrowDatasetAdapter does not serve connector {kind!r}")
        return SourceHandle(
            source_id=source.name,
            connector=kind,
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=False,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        arrow_schema = _resource_schema(resource)
        # The resource carries the *observed* Arrow schema: the physical
        # Parquet schema, the reconciled CSV schema (physical types with
        # declared nullability), or the provider object's schema.  Converting
        # it back to the logical model lets the registry compare it against
        # the declaration and reject any drift before registration.
        from selayer.sources.schema import table_schema_from_arrow

        return table_schema_from_arrow(arrow_schema)

    # -- registration ------------------------------------------------------

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        connection.register(stable_name, handle.resource)  # type: ignore[attr-defined]

    # -- query binding -----------------------------------------------------

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        # Persistent datasets (parquet/csv and provider-backed datasets/scanners/
        # tables) are registered once and benefit from automatic DuckDB Arrow
        # pushdown; they need no per-query binding.  Only query-scoped readers
        # are recreated per query, and that path is driven by the registry which
        # re-invokes the stored provider directly.
        if not handle.query_scoped or handle.cleanup is None:
            return None
        return QueryBinding(
            source_id=handle.source_id,
            stable_name=handle.source_id,
            cleanup=handle.cleanup,
        )

    # -- close -------------------------------------------------------------

    def close(self, handle: SourceHandle) -> None:
        resource = handle.resource
        closer = getattr(resource, "close", None)
        if closer is not None:
            with suppress(Exception):
                closer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connector_kind(connector: SourceConnector) -> str:
    if isinstance(connector, ParquetConfig):
        return "parquet"
    if isinstance(connector, CsvConfig):
        return "csv"
    if isinstance(connector, PyArrowConfig):
        return "pyarrow"
    return type(connector).__name__


def _csv_dataset(
    connector: CsvConfig,
    declared_schema: pa.Schema,
    filesystem: pafs.FileSystem | None,
    location: str,
) -> padataset.Dataset:
    parse_options = pacsv.ParseOptions(
        delimiter=connector.delimiter,
        quote_char=connector.quote_char,
        escape_char=connector.escape_char,
    )
    # Probe the *physical* CSV schema without a declared type override so the
    # file's own inferred types are observed.  CSV type inference marks every
    # column ``nullable=True`` regardless of the data, so the probe alone would
    # produce false mismatches against declared non-nullable fields.  For a
    # header-less file there is no header row to read names from, so the
    # declared field names are used positionally.
    if not connector.has_header:
        read_options = pacsv.ReadOptions(
            column_names=list(declared_schema.names), autogenerate_column_names=False
        )
    else:
        read_options = pacsv.ReadOptions(autogenerate_column_names=False)
    probe_format = padataset.CsvFileFormat(
        parse_options=parse_options, read_options=read_options
    )
    physical_schema = _csv_probe_schema(location, probe_format, filesystem)
    # Reconcile by position: keep the physical inferred types (so type drift
    # and any extra columns remain observable) but adopt the declared
    # nullability for declared positions so inference's ``nullable=True`` does
    # not create a false mismatch.  Columns beyond the declaration keep their
    # physical nullability so an extra field still surfaces as drift.
    reconciled_schema = _reconcile_csv_schema(physical_schema, declared_schema)
    final_format = padataset.CsvFileFormat(
        parse_options=parse_options, read_options=read_options
    )
    if filesystem is None:
        return padataset.dataset(
            location, schema=reconciled_schema, format=final_format
        )
    return padataset.dataset(
        location, schema=reconciled_schema, format=final_format, filesystem=filesystem
    )


def _csv_probe_schema(
    location: str,
    probe_format: padataset.CsvFileFormat,
    filesystem: pafs.FileSystem | None,
) -> pa.Schema:
    """Probe the physical CSV schema at ``location`` through ``filesystem``."""

    if filesystem is None:
        return padataset.dataset(location, format=probe_format).schema
    return padataset.dataset(
        location, format=probe_format, filesystem=filesystem
    ).schema


def _reconcile_csv_schema(
    physical_schema: pa.Schema, declared_schema: pa.Schema
) -> pa.Schema:
    """Adopt declared nullability by position while keeping physical types.

    The physical types (and any extra fields beyond the declaration) are
    preserved so genuine type drift and extra columns stay observable for
    :func:`~selayer.sources.schema.compare_schemas`; only the nullability of
    the declared positions is taken from the declaration, neutralizing CSV
    inference's unconditional ``nullable=True``.
    """

    fields: list[pa.Field] = []
    for index, physical_field in enumerate(physical_schema):
        if index < len(declared_schema):
            declared_field = declared_schema.field(index)
            fields.append(
                pa.field(
                    physical_field.name,
                    physical_field.type,
                    nullable=declared_field.nullable,
                )
            )
        else:
            fields.append(physical_field)
    return pa.schema(fields)


def _handle_for_arrow_object(
    source: ParsedSource,
    resource: ArrowObject,
    provider: Callable[[], ArrowObject],
) -> SourceHandle:
    """Build a handle for a provider-resolved Arrow object.

    Datasets, scanners, and tables are persistent: their provider is retained
    on the handle (as ``resource``-adjacent state via the cleanup slot is not
    appropriate) so the registry can re-invoke it on reload.  Record batch
    readers are single-pass and therefore query-scoped.
    """

    if isinstance(resource, pa.RecordBatchReader):
        return SourceHandle(
            source_id=source.name,
            connector="pyarrow",
            resource=resource,
            schema=source.schema,
            snapshot=None,
            query_scoped=True,
            # The provider is stashed on the handle so the registry can recreate
            # the reader per query without re-resolving the resolver.  It is
            # repr-hidden by ``SourceHandle``.
            cleanup=_reader_recreator(provider),
        )
    return SourceHandle(
        source_id=source.name,
        connector="pyarrow",
        resource=resource,
        schema=source.schema,
        snapshot=None,
        query_scoped=False,
        # Stash the provider so reload can re-invoke it.  Using the cleanup slot
        # is the only repr-hidden mutable carrier on a frozen handle.
        cleanup=_persisted_provider(provider),
    )


def _persisted_provider(
    provider: Callable[[], ArrowObject],
) -> Callable[[], None]:
    """Wrap a provider so it survives on a frozen handle's cleanup slot.

    The cleanup slot is the only ``repr=False`` carrier on
    :class:`~selayer.sources.base.SourceHandle`; the registry reads it back via
    :func:`_read_provider`.  Returning a no-op keeps the persisted provider out
    of any accidental close path.
    """

    def _noop() -> None:
        return None

    _noop.provider = provider  # type: ignore[attr-defined]
    return _noop


def _read_provider(handle: SourceHandle) -> Callable[[], ArrowObject] | None:
    """Return the provider stashed on a handle, if any."""

    cleanup = handle.cleanup
    if cleanup is None:
        return None
    return getattr(cleanup, "provider", None)


def _reader_recreator(
    provider: Callable[[], ArrowObject],
) -> Callable[[], None]:
    """Stash a reader provider for query-scoped recreation on the handle."""

    def _noop() -> None:
        return None

    _noop.provider = provider  # type: ignore[attr-defined]
    return _noop


def _resource_schema(resource: ArrowObject) -> pa.Schema:
    if isinstance(resource, padataset.Dataset):
        return resource.schema
    if isinstance(resource, padataset.Scanner):
        return resource.dataset_schema
    if isinstance(resource, pa.Table):
        return resource.schema
    if isinstance(resource, pa.RecordBatchReader):
        return resource.schema
    raise TypeError(f"unsupported Arrow resource: {type(resource).__name__}")


# ---------------------------------------------------------------------------
# S3 transport
# ---------------------------------------------------------------------------

# Known profile keys for the S3 transport.  Unknown keys are rejected so a
# typo (e.g. ``acces_key``) cannot silently fall through to the default
# credential chain and route an explicit secret to the wrong path.  Only key
# *names* are configuration metadata; secret values are never enumerated in
# bulk by :meth:`RuntimeProfile.keys`.
_S3_PROFILE_KEYS: frozenset[str] = frozenset(
    {
        "access_key",
        "secret_key",
        "session_token",
        "region",
        "endpoint_override",
        "scheme",
        "profile_name",
        "role_arn",
        "external_id",
        "session_name",
    }
)

_VALID_S3_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Every S3 profile option is a string.  Requiring an *exact* builtin ``str``
# (``type(value) is str``, not ``isinstance``) for each present value *before*
# any membership, hash, or ``urlparse`` operation means a hostile ``str``
# subclass — whose ``__str__``/``__hash__``/``__eq__`` could raise or leak
# secret text into a propagated exception — is rejected by type first, so none
# of its dunders is ever invoked.
_S3_STRING_OPTIONS: frozenset[str] = frozenset(
    {
        "access_key",
        "secret_key",
        "session_token",
        "region",
        "endpoint_override",
        "scheme",
        "profile_name",
        "role_arn",
        "external_id",
        "session_name",
    }
)


def s3_filesystem(profile: RuntimeProfile) -> pafs.S3FileSystem:
    """Build a :class:`pyarrow.fs.S3FileSystem` from a runtime profile.

    Three credential paths are supported, resolved in priority order:

    * **role session** — when ``role_arn`` is present, STS assumes the role
      (via boto3) and returns temporary credentials.
    * **explicit credentials** — when ``access_key`` and ``secret_key`` are
      present, they are passed frozen to PyArrow (boto3 is not consulted).
    * **default chain** — otherwise boto3's standard chain resolves
      credentials; ``profile_name`` selects a named AWS profile.

    The profile mapping is never forwarded to Arrow wholesale: only frozen,
    individual values are passed.  Unknown keys, an endpoint carrying URI
    userinfo, or an unsupported scheme are rejected with a *constant* message
    raised outside any ``except`` scope, so no secret value can surface in any
    error surface — ``error.args``, the formatted traceback, ``__cause__``, or
    ``__context__``.
    """

    _validate_s3_profile(profile)
    keys = set(profile.keys())

    scheme = _s3_profile_value(profile, "scheme", "https")
    endpoint_override = _s3_profile_value(profile, "endpoint_override", None)
    region = _s3_profile_value(profile, "region", None)

    if "role_arn" in keys:
        access_key, secret_key, session_token = _s3_role_session_credentials(profile)
    elif "access_key" in keys and "secret_key" in keys:
        access_key = _s3_profile_value(profile, "access_key", None)
        secret_key = _s3_profile_value(profile, "secret_key", None)
        session_token = _s3_profile_value(profile, "session_token", None)
    else:
        access_key, secret_key, session_token = _s3_default_chain_credentials(profile)

    # The driver call receives the access key, secret key, and session token; a
    # failure there may echo them in the driver exception's message/repr.  The
    # call is wrapped so a constant ``ValueError`` is raised *outside* the
    # ``except`` scope (keeping ``__cause__``/``__context__`` ``None``),
    # discarding the driver exception entirely so no sentinel surfaces.
    filesystem: pafs.S3FileSystem | None = None
    try:
        filesystem = pafs.S3FileSystem(
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            region=region,
            endpoint_override=endpoint_override,
            scheme=scheme,
        )
    except Exception:  # noqa: BLE001 - sanitize any failure that may echo credentials
        filesystem = None
    if filesystem is None:
        raise ValueError("S3 filesystem could not be created")
    return filesystem


def _s3_profile_value(
    profile: RuntimeProfile, name: str, default: str | None
) -> str | None:
    """Return a validated S3 string option, or ``default`` when absent.

    Call only after :func:`_validate_s3_profile`: every present value is
    guaranteed to be an exact builtin ``str``.  The ``type(value) is str`` test
    narrows the ``object`` returned by :meth:`RuntimeProfile.value` to ``str``
    so downstream PyArrow/boto3/``urlparse`` consumers get a concrete type
    without further casting.
    """

    if name in profile:
        value: object = profile.value(name)
        if type(value) is str:
            return value
        return default
    return default


def _validate_s3_profile(profile: RuntimeProfile) -> None:
    """Reject unknown keys, non-``str`` values, bad schemes, and userinfo endpoints.

    Every rejection raises a :class:`ValueError` with a *constant* message and
    outside any ``except`` scope, so no profile value can surface in any error
    surface (``error.args``, the formatted traceback, ``__cause__``,
    ``__context__``).

    Each present value is required to be an *exact* builtin ``str``
    (``type(value) is str``) *before* any membership, hash, or ``urlparse``
    operation: a hostile ``str`` subclass whose ``__str__``/``__hash__``/
    ``__eq__`` raises or leaks secret text would otherwise surface that text in
    a propagated exception.
    """

    unknown = set(profile.keys()) - _S3_PROFILE_KEYS
    if unknown:
        raise ValueError("unsupported key in S3 profile")
    for name in _S3_STRING_OPTIONS:
        if name in profile and type(profile.value(name)) is not str:
            raise ValueError("invalid S3 profile value")
    scheme = _s3_profile_value(profile, "scheme", None)
    if scheme is not None and scheme not in _VALID_S3_SCHEMES:
        raise ValueError("invalid S3 scheme")
    endpoint = _s3_profile_value(profile, "endpoint_override", None)
    if endpoint is not None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in _VALID_S3_SCHEMES:
            raise ValueError("invalid S3 endpoint")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("S3 endpoint must not contain credentials")


def _s3_role_session_credentials(
    profile: RuntimeProfile,
) -> tuple[object, object, object]:
    """Assume an IAM role via STS; return ``(access_key, secret_key, token)``."""

    if boto3 is None:
        raise ValueError("boto3 is required for S3 role-session credentials")
    client_kwargs: dict[str, object] = {}
    region = _s3_profile_value(profile, "region", None)
    if region is not None:
        client_kwargs["region_name"] = region
    client = boto3.client("sts", **client_kwargs)
    assume_kwargs: dict[str, object] = {"RoleArn": profile.value("role_arn")}
    external_id = _s3_profile_value(profile, "external_id", None)
    if external_id is not None:
        assume_kwargs["ExternalId"] = external_id
    session_name = _s3_profile_value(profile, "session_name", None)
    if session_name is not None:
        assume_kwargs["RoleSessionName"] = session_name
    response = client.assume_role(**assume_kwargs)
    credentials = response["Credentials"]
    return (
        credentials["AccessKeyId"],
        credentials["SecretAccessKey"],
        credentials["SessionToken"],
    )


def _s3_default_chain_credentials(
    profile: RuntimeProfile,
) -> tuple[object, object, object]:
    """Resolve credentials via boto3's default chain; ``(key, secret, token)``."""

    if boto3 is None:
        raise ValueError("boto3 is required for S3 default-chain credentials")
    session_kwargs: dict[str, object] = {}
    profile_name = _s3_profile_value(profile, "profile_name", None)
    if profile_name is not None:
        session_kwargs["profile_name"] = profile_name
    region = _s3_profile_value(profile, "region", None)
    if region is not None:
        session_kwargs["region_name"] = region
    session = boto3.Session(**session_kwargs)
    credentials = session.get_credentials()
    if credentials is None:
        raise ValueError("no S3 credentials resolved from the default chain")
    frozen = credentials.get_frozen_credentials()
    return frozen.access_key, frozen.secret_key, frozen.token


def _resolve_s3_location(
    connector: ParquetConfig | CsvConfig,
    profiles: RuntimeProfileResolver,
    source_id: str,
) -> tuple[pafs.FileSystem | None, str]:
    """Resolve ``(filesystem, path)`` for a file connector.

    When ``credential_profile`` is set the named profile is resolved to an
    :class:`~pyarrow.fs.S3FileSystem` and the ``s3://`` scheme is stripped per
    PyArrow's filesystem contract (the filesystem owns the scheme/host).  Local
    paths (no ``credential_profile``) are returned unchanged with a ``None``
    filesystem so local pushdown and reload semantics are preserved exactly.
    """

    credential_profile = connector.credential_profile
    location = connector.location
    if credential_profile is None:
        return None, location
    profile = profiles.resolve(credential_profile, source_id=source_id)
    filesystem = s3_filesystem(profile)
    location = location.removeprefix("s3://")
    return filesystem, location


def _parquet_dataset(
    path: str, filesystem: pafs.FileSystem | None
) -> padataset.Dataset:
    """Build a parquet dataset, attaching a filesystem only when one is set.

    Built *without* a schema override so the physical Parquet schema is
    observed by :func:`ArrowDatasetAdapter.inspect_schema` and any drift is
    caught before registration.  DuckDB still gets ARROW_SCAN pushdown over the
    dataset (local or S3-backed alike).
    """

    if filesystem is None:
        return padataset.dataset(path, format="parquet")
    return padataset.dataset(path, format="parquet", filesystem=filesystem)
