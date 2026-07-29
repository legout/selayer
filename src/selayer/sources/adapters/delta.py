"""Delta Lake table adapter using ``deltalake.DeltaTable``.

This adapter satisfies the private
:class:`~selayer.sources.base.SourceAdapter` protocol for the ``delta``
connector kind.  It builds a :class:`pyarrow.dataset.Dataset` from a fresh
:class:`deltalake.DeltaTable` on every prepare/reload, then registers the
Dataset with DuckDB so projection and filter pushdown happen inside DuckDB's
Arrow scanner.  No DuckDB Delta extension is used.

Design pillars:

* **Fresh candidate per prepare/reload.**  A new ``DeltaTable`` is created on
  every call so candidate preparation never mutates the published generation.
  ``update_incremental()`` is never called on the active table.
* **Safe integer version snapshot.**  ``DeltaTable.version()`` (an integer) is
  converted to ``str`` and stored as the handle snapshot — never the table URI
  or any opaque handle.
* **Declared schema is authoritative.**  The Dataset is built from the Delta
  log's physical schema so an observed schema that drifts from the declaration
  is caught by :func:`~selayer.sources.schema.compare_schemas` before
  registration.
* **S3 profile transport.**  When ``credential_profile`` is set the named
  profile is resolved to a :class:`pyarrow.fs.FileSystem` (via the shared
  :func:`~selayer.sources.adapters.arrow.s3_filesystem` helper) and passed to
  ``to_pyarrow_dataset(filesystem=...)``.
* **Resource cleanup.**  Both the ``DeltaTable`` and the Dataset are retained
  in an internal resource record (``repr=False``); ``close`` releases both so
  the underlying Rust object store and PyArrow file handles can be garbage
  collected on reload and engine close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow.dataset as padataset
import pyarrow.fs as pafs

from selayer.sources.adapters.arrow import (
    _s3_profile_value,
    _s3_role_session_credentials,
    _validate_s3_profile,
    s3_filesystem,
)
from selayer.sources.base import (
    QueryBinding,
    SourceHandle,
    SourceScanRequirement,
)
from selayer.sources.catalog import ParsedSource
from selayer.sources.config import DeltaConfig
from selayer.sources.errors import SourceDependencyError
from selayer.sources.profiles import (
    ArrowProviderResolver,
    RuntimeProfile,
    RuntimeProfileResolver,
)
from selayer.sources.schema import TableSchema, table_schema_from_arrow

# Conditional import: ``deltalake`` is an optional extra.  The import
# exception is never retained — ``_DeltaTable`` is ``None`` when the package
# is absent, and ``prepare`` converts the absence to a sanitized
# :class:`SourceDependencyError` without retaining any import exception.
try:
    from deltalake import DeltaTable as _DeltaTable
except ImportError:
    _DeltaTable = None  # type: ignore[assignment, misc]

__all__ = ["DeltaAdapter"]


@dataclass(slots=True)
class _DeltaResource:
    """Internal record holding a fresh DeltaTable and its PyArrow Dataset.

    Both fields are ``repr=False`` so no resource object can surface in
    diagnostics.  The registry registers only the Dataset; the DeltaTable is
    retained so :meth:`DeltaAdapter.close` can release it.
    """

    table: object = field(repr=False)
    dataset: padataset.Dataset = field(repr=False)


class DeltaAdapter:
    """Delta Lake table adapter using ``deltalake.DeltaTable``.

    A fresh ``DeltaTable`` is created on every prepare/reload, then
    ``to_pyarrow_dataset(filesystem=...)`` builds a PyArrow Dataset for DuckDB
    registration.  ``table.version()`` is the safe snapshot string.  No DuckDB
    Delta extension is used — DuckDB scans the PyArrow Dataset.
    """

    __slots__ = ()

    # -- prepare -----------------------------------------------------------

    def prepare(
        self,
        source: ParsedSource,
        profiles: RuntimeProfileResolver,
        arrow_providers: ArrowProviderResolver,
    ) -> SourceHandle:
        if _DeltaTable is None:
            # Raised outside any ``except`` scope so ``__cause__`` and
            # ``__context__`` remain ``None``.  No location or profile value
            # is interpolated: ``SourceDependencyError`` discards the
            # caller-supplied message and stores only the constant generic
            # text for ``"missing_delta_dependency"``.
            raise SourceDependencyError(
                source.name,
                "missing_delta_dependency",
                "deltalake is required for delta sources",
            )
        connector = source.connector
        assert isinstance(connector, DeltaConfig)

        credential_profile = connector.credential_profile
        location = connector.location

        if credential_profile is not None:
            profile = profiles.resolve(credential_profile, source_id=source.name)
            storage_options = _delta_storage_options(profile)
            base_filesystem = s3_filesystem(profile)
            # Root the filesystem at the table's bucket/prefix.  The wrapped
            # S3FileSystem owns the ``s3://`` scheme/host, so the root is a
            # clean ``bucket/prefix`` path and the relative file paths stored
            # in the Delta log resolve against it.  A local path is returned
            # unchanged (trailing slash stripped) so local behavior is
            # preserved exactly.
            root = _s3_root_path(location)
            filesystem = pafs.SubTreeFileSystem(root, base_filesystem)
            # The DeltaTable reads the transaction log via its own storage
            # backend; ``storage_options`` carry the resolved credentials.
            # For a local path the options are harmlessly ignored.
            table = _DeltaTable(location, storage_options=storage_options)
        else:
            filesystem = None
            table = _DeltaTable(location)

        # Build the Dataset from the Delta log's physical schema (no declared
        # override) so ``inspect_schema`` observes the physical types and any
        # drift is caught by ``compare_schemas`` before registration.
        dataset = table.to_pyarrow_dataset(filesystem=filesystem)
        snapshot = str(table.version())

        return SourceHandle(
            source_id=source.name,
            connector="delta",
            resource=_DeltaResource(table=table, dataset=dataset),
            schema=source.schema,
            snapshot=snapshot,
            query_scoped=False,
        )

    # -- schema inspection -------------------------------------------------

    def inspect_schema(self, handle: SourceHandle) -> TableSchema:
        resource = handle.resource
        assert isinstance(resource, _DeltaResource)
        arrow_schema = resource.dataset.schema
        # Converting the physical Arrow schema to the logical model lets the
        # registry compare it against the declaration and reject any drift.
        return table_schema_from_arrow(arrow_schema)

    # -- registration ------------------------------------------------------

    def register(
        self, connection: object, stable_name: str, handle: SourceHandle
    ) -> None:
        resource = handle.resource
        assert isinstance(resource, _DeltaResource)
        connection.register(stable_name, resource.dataset)  # type: ignore[attr-defined]

    # -- query binding -----------------------------------------------------

    def bind_query(
        self, handle: SourceHandle, requirement: SourceScanRequirement
    ) -> QueryBinding | None:
        # Persistent Delta Datasets are registered once and benefit from
        # automatic DuckDB Arrow pushdown; they need no per-query binding.
        return None

    # -- close -------------------------------------------------------------

    def close(self, handle: SourceHandle) -> None:
        resource = handle.resource
        if isinstance(resource, _DeltaResource):
            # Release both references so the underlying Rust DeltaTable and
            # the PyArrow Dataset file handles can be garbage collected.
            # ``DeltaTable`` has no explicit ``close()``; dropping the Python
            # reference drops the Rust object store.
            resource.table = None
            resource.dataset = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_root_path(location: str) -> str:
    """Return the SubTreeFileSystem root for a Delta ``location``.

    The ``s3://`` scheme is stripped (the wrapped :class:`S3FileSystem` owns
    the scheme/host) and any trailing slash is removed, so the root is a clean
    ``bucket/prefix`` path that the relative file paths stored in the Delta
    log resolve against.  Local paths are returned with a trailing slash
    stripped but otherwise unchanged, so local behavior is preserved exactly.
    """

    path = location.removeprefix("s3://")
    path = path.removesuffix("/")
    return path


def _delta_storage_options(profile: RuntimeProfile) -> dict[str, str]:
    """Resolve ``deltalake``/AWS storage options for a Delta S3 profile.

    Mirrors the credential precedence of :func:`~selayer.sources.adapters.arrow.s3_filesystem`
    so the DeltaTable transaction-log reader uses the same identity as the
    dataset filesystem:

    * **role session** — when ``role_arn`` is present, STS assumes the role
      (via boto3) and the temporary credentials become
      ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``.
      If boto3 is unavailable a constant ``ValueError`` is raised outside any
      ``except`` scope.
    * **explicit credentials** — when ``access_key`` and ``secret_key`` are
      present they are forwarded directly (plus an optional ``session_token``).
    * **named/default profile** — ``AWS_PROFILE`` is set so Delta's Rust
      ``object_store`` resolves the profile itself rather than receiving
      long-lived credentials.

    Region and endpoint are always propagated when present
    (``AWS_REGION`` / ``AWS_ENDPOINT_URL``).  Every value is read through an
    exact-builtin-``str`` guard, and every failure raises a *constant* message
    outside any ``except`` scope so no credential can leak via ``error.args``,
    the traceback, ``__cause__``, or ``__context__``.
    """

    _validate_s3_profile(profile)
    keys = set(profile.keys())
    opts: dict[str, str] = {}

    region = _s3_profile_value(profile, "region", None)
    endpoint = _s3_profile_value(profile, "endpoint_override", None)
    if region is not None:
        opts["AWS_REGION"] = region
    if endpoint is not None:
        opts["AWS_ENDPOINT_URL"] = endpoint

    if "role_arn" in keys:
        access_key, secret_key, session_token = _s3_role_session_credentials(profile)
        _add_str_storage_opt(opts, "AWS_ACCESS_KEY_ID", access_key)
        _add_str_storage_opt(opts, "AWS_SECRET_ACCESS_KEY", secret_key)
        _add_str_storage_opt(opts, "AWS_SESSION_TOKEN", session_token)
    elif "access_key" in keys and "secret_key" in keys:
        _add_str_storage_opt(
            opts, "AWS_ACCESS_KEY_ID", _s3_profile_value(profile, "access_key", None)
        )
        _add_str_storage_opt(
            opts,
            "AWS_SECRET_ACCESS_KEY",
            _s3_profile_value(profile, "secret_key", None),
        )
        _add_str_storage_opt(
            opts,
            "AWS_SESSION_TOKEN",
            _s3_profile_value(profile, "session_token", None),
        )
    else:
        # Named/default profile: Delta's Rust ``object_store`` consumes
        # ``AWS_PROFILE`` directly (alongside the propagated region/endpoint)
        # rather than receiving resolved long-lived credentials.
        profile_name = _s3_profile_value(profile, "profile_name", None)
        if profile_name is not None:
            opts["AWS_PROFILE"] = profile_name
    return opts


def _add_str_storage_opt(opts: dict[str, str], env_key: str, value: object) -> None:
    """Add ``env_key`` only when ``value`` is an exact builtin ``str``.

    A hostile ``str`` subclass whose dunders could leak a secret is rejected
    by the exact-type guard (``type(value) is str``); ``None`` and non-string
    values are skipped so Delta falls back to its own chain for those.
    """

    if type(value) is str:
        opts[env_key] = value
