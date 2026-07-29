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

from selayer.sources.adapters.arrow import s3_filesystem
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


# Mapping from S3 profile keys to the AWS storage-option names that
# ``deltalake`` (via the Rust ``object_store`` crate) consumes.  Only key
# *names* are configuration metadata; secret values are never enumerated.
_STORAGE_OPTION_MAP: dict[str, str] = {
    "access_key": "AWS_ACCESS_KEY_ID",
    "secret_key": "AWS_SECRET_ACCESS_KEY",
    "session_token": "AWS_SESSION_TOKEN",
    "region": "AWS_REGION",
    "endpoint_override": "AWS_ENDPOINT_URL",
}


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
            filesystem = s3_filesystem(profile)
            storage_options = _delta_storage_options(profile)
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


def _delta_storage_options(profile: RuntimeProfile) -> dict[str, str]:
    """Map S3 profile keys to ``deltalake``/AWS storage-option names.

    Only keys present in the profile are mapped; absent keys are omitted so
    ``deltalake`` falls back to its own credential chain for those.  Secret
    values are never exposed in bulk — only individual present values are read.
    """

    opts: dict[str, str] = {}
    for profile_key, env_key in _STORAGE_OPTION_MAP.items():
        if profile_key in profile:
            value = profile.value(profile_key)
            # Only an exact builtin ``str`` is accepted so a hostile subclass
            # cannot leak its ``__repr__`` through the storage options.
            if type(value) is str:
                opts[env_key] = value
    return opts
